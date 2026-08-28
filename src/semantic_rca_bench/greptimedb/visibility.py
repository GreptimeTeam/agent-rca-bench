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
            self._check_information_schema_scope(tables)

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

    def _check_information_schema_scope(self, tables: list[exp.Table]) -> None:
        scoped_selects: set[int] = set()
        for table in tables:
            if (table.db or "").lower() != "information_schema" or table.name.lower() not in {
                "tables",
                "columns",
                "table_semantics",
            }:
                continue
            select = table.find_ancestor(exp.Select)
            if select is None or id(select) in scoped_selects:
                continue
            scoped_selects.add(id(select))
            if not self._has_table_schema_scope(select):
                raise QueryRejected(
                    "information_schema queries must filter table_schema with an "
                    f"AND-conjunctive table_schema = '{self.client.database}' predicate"
                )

    def _has_table_schema_scope(self, select: exp.Select) -> bool:
        where = select.args.get("where")
        if where is None:
            return False
        expected = self.client.database.lower()
        for equality in where.find_all(exp.EQ):
            if equality.find_ancestor(exp.Select) is not select or not _is_schema_equality(
                equality, expected
            ):
                continue
            node: exp.Expression | None = equality.parent
            while node is not None and node is not select:
                if isinstance(node, (exp.Or, exp.Not)):
                    break
                node = node.parent
            else:
                return True
        return False

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


def _is_schema_equality(equality: exp.EQ, expected: str) -> bool:
    sides = (
        (equality.this, equality.expression),
        (equality.expression, equality.this),
    )
    for column, literal in sides:
        if (
            isinstance(column, exp.Column)
            and column.name.lower() == "table_schema"
            and isinstance(literal, exp.Literal)
            and literal.is_string
            and str(literal.this).lower() == expected
        ):
            return True
    return False
