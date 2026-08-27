from __future__ import annotations

import pytest

from semantic_rca_bench.contracts import QueryResult, Visibility
from semantic_rca_bench.greptimedb.visibility import QueryGateway, QueryRejected


class StubClient:
    database = "benchmark_db"

    def query(self, _: str, *, max_rows: int | None) -> QueryResult:
        return QueryResult(
            query_id="q1",
            columns=["table_schema", "table_name"],
            rows=[
                ["public", "traces"],
                ["information_schema", "table_semantics"],
                ["greptime_private", "semantic_entities"],
            ],
            elapsed_seconds=0.0,
        )


def test_raw_hides_semantic_surfaces() -> None:
    gateway = QueryGateway(StubClient(), Visibility.RAW)  # type: ignore[arg-type]
    with pytest.raises(QueryRejected):
        gateway.execute("SELECT * FROM information_schema.table_semantics")
    with pytest.raises(QueryRejected):
        gateway.execute("SELECT * FROM greptime_private.semantic_entities")

    result = gateway.execute(
        "SELECT table_schema, table_name FROM information_schema.tables "
        "WHERE table_schema = 'benchmark_db'"
    )
    assert result.rows == []


def test_table_semantics_hides_graph() -> None:
    gateway = QueryGateway(StubClient(), Visibility.TABLE_SEMANTICS)  # type: ignore[arg-type]
    gateway.execute(
        "SELECT * FROM information_schema.table_semantics WHERE table_schema = 'benchmark_db'"
    )
    with pytest.raises(QueryRejected):
        gateway.execute("SELECT * FROM greptime_private.semantic_relationships")


def test_graph_cannot_read_declared_storage() -> None:
    gateway = QueryGateway(StubClient(), Visibility.SEMANTIC_GRAPH)  # type: ignore[arg-type]
    gateway.execute("SELECT * FROM greptime_private.semantic_entities")
    with pytest.raises(QueryRejected):
        gateway.execute("SELECT * FROM greptime_private.semantic_relationships_declared")


def test_mutation_is_rejected() -> None:
    gateway = QueryGateway(StubClient(), Visibility.SEMANTIC_GRAPH)  # type: ignore[arg-type]
    with pytest.raises(QueryRejected):
        gateway.execute("DROP TABLE traces")


def test_rejects_cross_database_query() -> None:
    gateway = QueryGateway(StubClient(), Visibility.SEMANTIC_GRAPH)  # type: ignore[arg-type]

    with pytest.raises(QueryRejected, match="cross-database"):
        gateway.execute("SELECT * FROM another_database.traces")


def test_information_schema_query_requires_database_scope() -> None:
    gateway = QueryGateway(StubClient(), Visibility.TABLE_SEMANTICS)  # type: ignore[arg-type]

    with pytest.raises(QueryRejected, match="must filter table_schema"):
        gateway.execute("SELECT table_name FROM information_schema.table_semantics")


def test_discovery_filters_before_applying_row_limit() -> None:
    class CrowdedClient:
        database = "benchmark_db"

        def query(self, _: str, *, max_rows: int | None) -> QueryResult:
            rows = [["foreign", f"table_{index}"] for index in range(205)]
            rows.extend([["benchmark_db", "target"], ["benchmark_db", "target_2"]])
            visible = rows if max_rows is None else rows[:max_rows]
            return QueryResult(
                query_id="q1",
                columns=["table_schema", "table_name"],
                rows=visible,
                elapsed_seconds=0,
                truncated=max_rows is not None and len(rows) > max_rows,
            )

    gateway = QueryGateway(
        CrowdedClient(),  # type: ignore[arg-type]
        Visibility.TABLE_SEMANTICS,
        max_rows=1,
    )

    result = gateway.execute(
        "SELECT table_schema, table_name FROM information_schema.tables "
        "WHERE table_schema = 'benchmark_db'"
    )

    assert result.rows == [["benchmark_db", "target"]]
    assert result.truncated is True
