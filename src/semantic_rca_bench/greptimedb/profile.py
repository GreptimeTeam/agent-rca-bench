from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

from semantic_rca_bench.contracts import Visibility
from semantic_rca_bench.greptimedb.client import GreptimeClient

TABLE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")
SEARCH_STOP_WORDS = {
    "and",
    "for",
    "from",
    "in",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
}


class TableProfileError(ValueError):
    pass


class TableProfiler:
    def __init__(self, client: GreptimeClient, visibility: Visibility) -> None:
        self.client = client
        self.visibility = visibility

    def describe(
        self,
        table: str,
        *,
        include_samples: bool = True,
        sample_limit: int = 5,
    ) -> dict[str, Any]:
        schema_name, table_name = self._resolve(table)
        sample_limit = max(0, min(sample_limit, 20))
        started = time.monotonic()

        columns_result = self.client.query(
            f"""
            SELECT column_name, data_type, semantic_type, is_nullable, column_comment
            FROM information_schema.columns
            WHERE table_schema = '{schema_name}' AND table_name = '{table_name}'
            ORDER BY ordinal_position
            """,
            max_rows=1_000,
        )
        raw_columns = [_row_dict(columns_result.columns, row) for row in columns_result.rows]
        columns = [_column_profile(column) for column in raw_columns]
        if not columns:
            raise TableProfileError(f"table not found: {schema_name}.{table_name}")
        time_index = next(
            (
                str(column["name"])
                for column in columns
                if str(column.get("semantic_type", "")).upper() == "TIMESTAMP"
            ),
            None,
        )
        primary_keys = [
            str(column["name"])
            for column in columns
            if str(column.get("semantic_type", "")).upper() in {"TAG", "PRIMARY KEY", "PRIMARY_KEY"}
        ]
        profile: dict[str, Any] = {
            "query_id": uuid.uuid4().hex,
            "table": f"{schema_name}.{table_name}",
            "schema": {
                "columns": columns,
                "time_index": time_index,
                "primary_keys": primary_keys,
            },
        }

        if self.visibility is Visibility.RAW:
            profile["semantics"] = {"included": False}
        else:
            semantics = self._semantics(schema_name, table_name)
            profile["semantics"] = semantics

        if include_samples and sample_limit:
            qualified = f"{_quote(schema_name)}.{_quote(table_name)}"
            ordering = f" ORDER BY {_quote(time_index)} DESC" if time_index else ""
            samples = self.client.query(
                f"SELECT * FROM {qualified}{ordering} LIMIT {sample_limit}",
                max_rows=sample_limit,
            )
            profile["samples"] = {
                "included": True,
                "limit": sample_limit,
                "rows": [_row_dict(samples.columns, row) for row in samples.rows],
            }
        else:
            profile["samples"] = {"included": False}

        profile["elapsed_seconds"] = time.monotonic() - started
        return profile

    def search(
        self,
        query: str,
        *,
        signal_type: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        if self.visibility is Visibility.RAW:
            raise TableProfileError("table semantic search is unavailable")
        terms = _search_terms(query)
        if not terms:
            raise TableProfileError("search query must contain at least one word")
        if signal_type not in {None, "metric", "log", "trace", "event"}:
            raise TableProfileError(f"unsupported signal type: {signal_type}")
        limit = max(1, min(limit, 50))
        predicates = [f"table_schema = '{_escape_literal(self.client.database)}'"]
        if signal_type:
            predicates.append(f"signal_type = '{signal_type}'")
        term_predicates = []
        for term in terms:
            escaped = _escape_literal(term)
            term_predicates.append(
                "("
                f"LOWER(table_name) LIKE '%{escaped}%' OR "
                f"LOWER(COALESCE(semantic_options, '')) LIKE '%{escaped}%' OR "
                f"LOWER(COALESCE(entity_declarations, '')) LIKE '%{escaped}%'"
                ")"
            )
        predicates.append(f"({' OR '.join(term_predicates)})")
        result = self.client.query(
            f"""
            SELECT table_name, signal_type, source, source_version, pipeline,
                   metadata_quality, semantic_options, entity_declarations
            FROM information_schema.table_semantics
            WHERE {" AND ".join(predicates)}
            ORDER BY table_name
            LIMIT 1000
            """,
            max_rows=1_000,
        )
        candidates = []
        for row in result.rows:
            item = _row_dict(result.columns, row)
            options = _decode_json(item.get("semantic_options"))
            declarations = _decode_json(item.get("entity_declarations"))
            searchable = " ".join(
                str(value)
                for value in (
                    item.get("table_name"),
                    options,
                    declarations,
                )
                if value not in (None, "")
            )
            matched_terms = _matched_search_terms(terms, searchable)
            if not matched_terms:
                continue
            candidate = {
                "table": item.get("table_name"),
                "signal_type": item.get("signal_type"),
                "source": item.get("source"),
                "source_version": item.get("source_version"),
                "pipeline": item.get("pipeline"),
                "metadata_quality": item.get("metadata_quality"),
                "matched_terms": matched_terms,
            }
            if options not in (None, ""):
                candidate["semantic_options"] = options
            if declarations not in (None, ""):
                candidate["entity_declarations"] = declarations
            candidates.append(candidate)
        candidates.sort(
            key=lambda item: (
                -len(item["matched_terms"]),
                str(item["table"]).lower(),
            )
        )
        return {
            "query_id": result.query_id,
            "query": query,
            "terms": terms,
            "signal_type": signal_type,
            "matched_table_count": len(candidates),
            "matches": candidates[:limit],
            "truncated": result.truncated or len(candidates) > limit,
            "elapsed_seconds": result.elapsed_seconds,
        }

    def _resolve(self, table: str) -> tuple[str, str]:
        if not TABLE_PATTERN.fullmatch(table):
            raise TableProfileError("table must be an unquoted table or schema.table name")
        parts = table.split(".")
        if len(parts) == 1:
            return self.client.database, parts[0]
        if parts[0].lower() != self.client.database.lower():
            raise TableProfileError("cross-database table profiles are unavailable")
        return parts[0], parts[1]

    def _semantics(self, schema_name: str, table_name: str) -> dict[str, Any]:
        result = self.client.query(
            f"""
            SELECT signal_type, source, source_version, pipeline, metadata_quality,
                   semantic_options, entity_declarations
            FROM information_schema.table_semantics
            WHERE table_schema = '{schema_name}' AND table_name = '{table_name}'
            """
        )
        if not result.rows:
            return {
                "included": True,
                "available": True,
                "found": False,
            }
        row = _row_dict(result.columns, result.rows[0])
        row["semantic_options"] = _decode_json(row.get("semantic_options"))
        row["entity_declarations"] = _decode_json(row.get("entity_declarations"))
        return {
            "included": True,
            "available": True,
            "found": True,
            **row,
        }


def _quote(identifier: str | None) -> str:
    if identifier is None:
        raise TableProfileError("missing identifier")
    return f'"{identifier}"'


def _row_dict(columns: list[str], row: list[object]) -> dict[str, object]:
    return dict(zip(columns, row, strict=True))


def _column_profile(column: dict[str, object]) -> dict[str, object]:
    nullable = str(column.get("is_nullable", "")).upper()
    nullable_value = None
    if nullable in {"YES", "Y", "TRUE", "1"}:
        nullable_value = True
    elif nullable in {"NO", "N", "FALSE", "0"}:
        nullable_value = False
    profile = {
        "name": column.get("column_name"),
        "data_type": column.get("data_type"),
        "semantic_type": column.get("semantic_type"),
        "nullable": nullable_value,
    }
    comment = column.get("column_comment")
    if comment:
        profile["comment"] = comment
    return profile


def _decode_json(value: object) -> object:
    if not isinstance(value, str) or not value:
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _search_terms(query: str) -> list[str]:
    normalized = _normalize_slash_abbreviations(query).lower().replace("_", " ")
    terms = (
        term
        for term in re.findall(r"[a-z0-9.:-]+", normalized)
        if len(term) > 1 and term not in SEARCH_STOP_WORDS
    )
    return list(dict.fromkeys(terms))[:10]


def _matched_search_terms(terms: list[str], searchable: str) -> list[str]:
    normalized = _normalize_slash_abbreviations(searchable).lower()
    token_list = re.findall(r"[a-z0-9]+", normalized)
    tokens = set(token_list)
    pairs = set(zip(token_list, token_list[1:], strict=False))
    if ("io", "w") in pairs:
        tokens.add("write")
    if ("io", "r") in pairs:
        tokens.add("read")
    return [
        term
        for term in terms
        if (term in tokens if len(term) <= 2 else term in tokens or term in normalized)
    ]


def _normalize_slash_abbreviations(value: str) -> str:
    return re.sub(r"\b([A-Za-z])\s*/\s*([A-Za-z])\b", r"\1\2", value)


def _escape_literal(value: str) -> str:
    return value.replace("'", "''")
