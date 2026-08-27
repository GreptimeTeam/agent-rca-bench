from __future__ import annotations

from datetime import UTC, datetime

from semantic_rca_bench.contracts import CaseInput
from semantic_rca_bench.greptimedb.client import GreptimeClient, GreptimeError


class GraphIsolationError(RuntimeError):
    pass


def _timestamp(epoch_seconds: int) -> str:
    return datetime.fromtimestamp(epoch_seconds, UTC).strftime("%Y-%m-%d %H:%M:%S")


def inspect_semantic_surfaces(
    client: GreptimeClient,
    case_input: CaseInput,
) -> dict[str, object]:
    start = _timestamp(case_input.time_start)
    end = _timestamp(case_input.time_end)
    database = case_input.database.replace("'", "''")
    queries = {
        "tables": f"""
            SELECT table_schema, table_name
            FROM information_schema.tables
            WHERE table_schema = '{database}'
            ORDER BY table_schema, table_name
        """,
        "table_semantics": f"""
            SELECT signal_type, source, COUNT(*) AS table_count
            FROM information_schema.table_semantics
            WHERE table_schema = '{database}'
            GROUP BY signal_type, source
            ORDER BY signal_type, source
        """,
        "entities": f"""
            SELECT entity_type, COUNT(DISTINCT entity_id) AS distinct_entity_count,
                   COUNT(*) AS observation_count
            FROM greptime_private.semantic_entities
            WHERE observed_at >= '{start}' AND observed_at <= '{end}'
            GROUP BY entity_type
            ORDER BY entity_type
        """,
        "relationships": f"""
            SELECT rel_type, COUNT(*) AS distinct_relationship_count,
                   SUM(observation_count) AS observation_count
            FROM (
                SELECT src_type, src_id, dst_type, dst_id, rel_type,
                       COUNT(*) AS observation_count
                FROM greptime_private.semantic_relationships
                WHERE observed_at >= '{start}' AND observed_at <= '{end}'
                GROUP BY src_type, src_id, dst_type, dst_id, rel_type, provenance
            ) witnessed_edges
            GROUP BY rel_type
            ORDER BY rel_type
        """,
    }
    output: dict[str, object] = {}
    for name, sql in queries.items():
        try:
            output[name] = client.query(sql, max_rows=200).model_dump(mode="json")
        except GreptimeError as error:
            output[name] = {"error": str(error)}
    output["coverage"] = summarize_semantic_surfaces(output)
    return output


def assert_semantic_graph_isolated(client: GreptimeClient, database: str) -> None:
    escaped = database.replace("'", "''")
    result = client.query(
        f"""
            SELECT table_schema, table_name
            FROM information_schema.table_semantics
            WHERE table_schema <> '{escaped}'
              AND table_schema NOT IN ('information_schema', 'greptime_private', 'pg_catalog')
              AND entity_declarations IS NOT NULL
            ORDER BY table_schema, table_name
        """,
        max_rows=20,
    )
    if not result.rows:
        return
    sources = ", ".join(f"{row[0]}.{row[1]}" for row in result.rows)
    suffix = "" if not result.truncated else ", ..."
    raise GraphIsolationError(
        "Semantic Graph derives across every user schema in a catalog; "
        f"database {database!r} shares this instance with graph sources "
        f"{sources}{suffix}. Run this case on a fresh GreptimeDB instance."
    )


def summarize_semantic_surfaces(surfaces: dict[str, object]) -> dict[str, object]:
    table_semantics = _result(surfaces.get("table_semantics"))
    entities = _result(surfaces.get("entities"))
    relationships = _result(surfaces.get("relationships"))

    entity_types: dict[str, int] = {}
    if entities:
        columns, rows = entities
        type_index = _column_index(columns, "entity_type")
        count_index = _column_index(columns, "distinct_entity_count")
        id_index = _column_index(columns, "entity_id")
        if type_index is not None and count_index is not None:
            entity_types = {str(row[type_index]): int(row[count_index] or 0) for row in rows}
        elif type_index is not None and id_index is not None:
            distinct = {(str(row[type_index]), str(row[id_index])) for row in rows}
            for entity_type, _ in distinct:
                entity_types[entity_type] = sum(item[0] == entity_type for item in distinct)

    relationship_types: dict[str, int] = {}
    relationship_observations: dict[str, int] = {}
    if relationships:
        columns, rows = relationships
        type_index = _column_index(columns, "rel_type")
        distinct_index = _column_index(columns, "distinct_relationship_count")
        observation_index = _column_index(columns, "observation_count")
        if type_index is not None:
            for row in rows:
                rel_type = str(row[type_index])
                distinct_count = int(row[distinct_index] or 0) if distinct_index is not None else 1
                observation_count = (
                    int(row[observation_index] or 0)
                    if observation_index is not None
                    else distinct_count
                )
                relationship_types[rel_type] = relationship_types.get(rel_type, 0) + distinct_count
                relationship_observations[rel_type] = (
                    relationship_observations.get(rel_type, 0) + observation_count
                )

    graph_errors = [
        str(value["error"])
        for key in ("entities", "relationships")
        if isinstance((value := surfaces.get(key)), dict) and value.get("error")
    ]
    distinct_entity_count = sum(entity_types.values())
    distinct_relationship_count = sum(relationship_types.values())
    relationship_observation_count = sum(relationship_observations.values())
    if graph_errors:
        graph_status = "unavailable"
    elif distinct_relationship_count:
        graph_status = "relational"
    elif distinct_entity_count:
        graph_status = "entity-only"
    else:
        graph_status = "empty"

    semantic_table_count = 0
    semantic_table_groups: list[dict[str, object]] = []
    if table_semantics:
        columns, rows = table_semantics
        count_index = _column_index(columns, "table_count")
        signal_index = _column_index(columns, "signal_type")
        source_index = _column_index(columns, "source")
        if count_index is None:
            semantic_table_count = len(rows)
        else:
            semantic_table_count = sum(int(row[count_index] or 0) for row in rows)
            semantic_table_groups = [
                {
                    "signal_type": row[signal_index] if signal_index is not None else None,
                    "source": row[source_index] if source_index is not None else None,
                    "table_count": int(row[count_index] or 0),
                }
                for row in rows
            ]
    return {
        "table_semantics": {
            "table_count": semantic_table_count,
            "groups": semantic_table_groups,
        },
        "graph": {
            "status": graph_status,
            "distinct_entity_count": distinct_entity_count,
            "entity_types": entity_types,
            "distinct_relationship_count": distinct_relationship_count,
            "relationship_observation_count": relationship_observation_count,
            "relationship_types": relationship_types,
            "errors": graph_errors,
        },
    }


def _result(value: object) -> tuple[list[str], list[list[object]]] | None:
    if not isinstance(value, dict) or value.get("error"):
        return None
    columns = value.get("columns")
    rows = value.get("rows")
    if not isinstance(columns, list) or not isinstance(rows, list):
        return None
    return [str(column) for column in columns], rows


def _column_index(columns: list[str], name: str) -> int | None:
    try:
        return columns.index(name)
    except ValueError:
        return None
