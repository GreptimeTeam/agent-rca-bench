from __future__ import annotations

import sqlglot
from sqlglot import exp

from semantic_rca_bench.contracts import QueryResult, Visibility
from semantic_rca_bench.greptimedb.client import GreptimeClient


class QueryRejected(ValueError):
    pass


class QueryGateway:
    def __init__(
        self,
        client: GreptimeClient,
        visibility: Visibility,
        *,
        max_rows: int = 200,
    ) -> None:
        self.client = client
        self.visibility = visibility
        self.max_rows = max_rows

    def execute(self, sql: str) -> QueryResult:
        statements = sqlglot.parse(sql, read="mysql")
        if len(statements) != 1:
            raise QueryRejected("exactly one SQL statement is allowed")
        statement = statements[0]
        if not isinstance(statement, (exp.Select, exp.Union, exp.Show, exp.Describe)):
            raise QueryRejected("only read-only SELECT, SHOW, and DESCRIBE are allowed")
        if isinstance(statement, exp.Show) and "CREATE" in sql.upper():
            raise QueryRejected("SHOW CREATE is unavailable in benchmark runs")

        tables = list(statement.find_all(exp.Table))
        for table in tables:
            self._check_table(table)
        if isinstance(statement, (exp.Select, exp.Union)):
            self._check_information_schema_scope(sql, tables)

        discovery = self._is_discovery_query(tables)
        result = self.client.query(sql, max_rows=None if discovery else self.max_rows)
        if discovery:
            result = self._filter_discovery_rows(result)
            return self._truncate(result)
        return result

    @staticmethod
    def _is_discovery_query(tables: list[exp.Table]) -> bool:
        return any(
            (table.db or "").lower() == "information_schema"
            and table.name.lower() in {"tables", "columns", "table_semantics"}
            for table in tables
        )

    def _truncate(self, result: QueryResult) -> QueryResult:
        return result.model_copy(
            update={
                "rows": result.rows[: self.max_rows],
                "truncated": result.truncated or len(result.rows) > self.max_rows,
            }
        )

    def _check_table(self, table: exp.Table) -> None:
        catalog = (table.catalog or "").lower()
        database = (table.db or "").lower()
        name = table.name.lower()
        if catalog:
            raise QueryRejected("cross-catalog queries are unavailable")
        if database not in {
            "",
            self.client.database.lower(),
            "information_schema",
            "greptime_private",
        }:
            raise QueryRejected("cross-database queries are unavailable")
        qualified = f"{database}.{name}".strip(".")
        if (
            qualified.endswith("information_schema.table_semantics")
            or qualified == "table_semantics"
        ) and self.visibility is Visibility.RAW:
            raise QueryRejected("table not found")
        if qualified.startswith("greptime_private."):
            if self.visibility is not Visibility.SEMANTIC_GRAPH:
                raise QueryRejected("table not found")
            if qualified.endswith("semantic_relationships_declared"):
                raise QueryRejected("declared relationship storage is unavailable")

    def _check_information_schema_scope(self, sql: str, tables: list[exp.Table]) -> None:
        reads_rows = any(
            (table.db or "").lower() == "information_schema"
            and table.name.lower() in {"tables", "columns", "table_semantics"}
            for table in tables
        )
        if reads_rows and self.client.database.lower() not in sql.lower():
            raise QueryRejected(
                f"information_schema queries must filter table_schema to '{self.client.database}'"
            )

    def _filter_discovery_rows(self, result: QueryResult) -> QueryResult:
        lowered = [column.lower() for column in result.columns]
        if "table_schema" not in lowered or "table_name" not in lowered:
            return result
        schema_index = lowered.index("table_schema")
        table_index = lowered.index("table_name")
        rows: list[list[object]] = []
        for row in result.rows:
            schema = str(row[schema_index]).lower()
            table = str(row[table_index]).lower()
            if schema not in {
                self.client.database.lower(),
                "information_schema",
                "greptime_private",
            }:
                continue
            if schema == "greptime_private" and self.visibility is not Visibility.SEMANTIC_GRAPH:
                continue
            if (
                schema == "information_schema"
                and table == "table_semantics"
                and self.visibility is Visibility.RAW
            ):
                continue
            rows.append(row)
        return result.model_copy(update={"rows": rows})
