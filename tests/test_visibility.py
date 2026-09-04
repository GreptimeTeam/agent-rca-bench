from __future__ import annotations

import pytest

from agent_rca_bench.contracts import QueryResult, Visibility
from agent_rca_bench.greptimedb.visibility import QueryGateway, QueryRejected


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


def test_semantic_graph_exposes_table_and_graph_surfaces() -> None:
    gateway = QueryGateway(StubClient(), Visibility.SEMANTIC_GRAPH)  # type: ignore[arg-type]
    gateway.execute(
        "SELECT * FROM information_schema.table_semantics WHERE table_schema = 'benchmark_db'"
    )
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
    gateway = QueryGateway(StubClient(), Visibility.SEMANTIC_GRAPH)  # type: ignore[arg-type]

    with pytest.raises(QueryRejected, match="must filter table_schema"):
        gateway.execute("SELECT table_name FROM information_schema.table_semantics")


@pytest.mark.parametrize(
    "query",
    [
        "SELECT DISTINCT table_schema FROM information_schema.tables "
        "WHERE table_name LIKE '%benchmark_db%'",
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'benchmark_db' OR table_name LIKE '%'",
        "SELECT table_name FROM information_schema.tables WHERE NOT table_schema = 'benchmark_db'",
    ],
)
def test_information_schema_scope_cannot_be_satisfied_by_text_or_bypass(
    query: str,
) -> None:
    gateway = QueryGateway(StubClient(), Visibility.SEMANTIC_GRAPH)  # type: ignore[arg-type]

    with pytest.raises(QueryRejected, match="AND-conjunctive"):
        gateway.execute(query)


def test_information_schema_scope_accepts_qualified_conjunctive_predicate() -> None:
    gateway = QueryGateway(StubClient(), Visibility.SEMANTIC_GRAPH)  # type: ignore[arg-type]

    gateway.execute(
        "SELECT t.table_name FROM information_schema.tables AS t "
        "WHERE t.table_schema = 'benchmark_db' AND t.table_name LIKE 'metric%'"
    )


def test_information_schema_scope_rejects_predicate_from_nested_select() -> None:
    gateway = QueryGateway(StubClient(), Visibility.SEMANTIC_GRAPH)  # type: ignore[arg-type]

    with pytest.raises(QueryRejected, match="AND-conjunctive"):
        gateway.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE EXISTS (SELECT 1 WHERE table_schema = 'benchmark_db')"
        )


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
        Visibility.SEMANTIC_GRAPH,
        max_rows=1,
    )

    result = gateway.execute(
        "SELECT table_schema, table_name FROM information_schema.tables "
        "WHERE table_schema = 'benchmark_db'"
    )

    assert result.rows == [["benchmark_db", "target"]]
    assert result.truncated is True


def test_query_can_raise_row_limit_within_the_benchmark_cap() -> None:
    observed_limits = []

    class RecordingClient:
        database = "benchmark_db"

        def query(self, _: str, *, max_rows: int | None) -> QueryResult:
            observed_limits.append(max_rows)
            return QueryResult(query_id="q1", columns=[], rows=[], elapsed_seconds=0)

    gateway = QueryGateway(RecordingClient(), Visibility.RAW)  # type: ignore[arg-type]

    gateway.execute("SELECT * FROM traces")
    gateway.execute("SELECT * FROM traces", max_rows=1000)

    assert observed_limits == [200, 1000]


@pytest.mark.parametrize("max_rows", [0, 1001, True, "1000"])
def test_query_rejects_invalid_or_excessive_row_limit(max_rows: object) -> None:
    gateway = QueryGateway(StubClient(), Visibility.RAW)  # type: ignore[arg-type]

    with pytest.raises(QueryRejected, match="max_rows"):
        gateway.execute("SELECT * FROM traces", max_rows=max_rows)  # type: ignore[arg-type]
