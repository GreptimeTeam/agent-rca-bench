import pytest

from semantic_rca_bench.contracts import QueryResult, Visibility
from semantic_rca_bench.greptimedb.profile import TableProfileError, TableProfiler


class StubClient:
    database = "benchmark_db"

    def __init__(self) -> None:
        self.queries: list[str] = []

    def query(self, statement: str, *, max_rows: int = 200) -> QueryResult:
        self.queries.append(statement)
        query_id = f"q{len(self.queries)}"
        if "information_schema.columns" in statement:
            return QueryResult(
                query_id=query_id,
                columns=[
                    "column_name",
                    "data_type",
                    "semantic_type",
                    "is_nullable",
                    "column_comment",
                ],
                rows=[
                    ["service_name", "String", "TAG", "NO", None],
                    ["timestamp", "TimestampNanosecond", "TIMESTAMP", "NO", None],
                    ["duration_nano", "UInt64", "FIELD", "YES", "span duration"],
                ],
                elapsed_seconds=0,
            )
        if "SELECT table_name" in statement and "information_schema.table_semantics" in statement:
            return QueryResult(
                query_id=query_id,
                columns=[
                    "table_name",
                    "signal_type",
                    "source",
                    "source_version",
                    "pipeline",
                    "metadata_quality",
                    "semantic_options",
                    "entity_declarations",
                ],
                rows=[
                    [
                        "JVM_Memory_HeapMemoryUsage",
                        "metric",
                        "prometheus",
                        None,
                        None,
                        "inferred",
                        None,
                        None,
                    ],
                    [
                        "redis_Redis_used_memory",
                        "metric",
                        "prometheus",
                        None,
                        None,
                        "inferred",
                        None,
                        None,
                    ],
                    [
                        "redis_connected_clients",
                        "metric",
                        "prometheus",
                        None,
                        None,
                        "inferred",
                        None,
                        None,
                    ],
                ],
                elapsed_seconds=0,
            )
        if "information_schema.table_semantics" in statement:
            return QueryResult(
                query_id=query_id,
                columns=[
                    "signal_type",
                    "source",
                    "source_version",
                    "pipeline",
                    "metadata_quality",
                    "semantic_options",
                    "entity_declarations",
                ],
                rows=[
                    [
                        "trace",
                        "opentelemetry",
                        None,
                        "greptime_trace_v1",
                        None,
                        '{"trace.version": "v1"}',
                        '[{"entity_type": "service", "id": ["service_name"]}]',
                    ]
                ],
                elapsed_seconds=0,
            )
        return QueryResult(
            query_id=query_id,
            columns=["service_name", "timestamp", "duration_nano"],
            rows=[["checkoutservice", "2024-01-19T12:15:11Z", 200_000_000]],
            elapsed_seconds=0,
        )


def test_raw_profile_does_not_read_semantics() -> None:
    client = StubClient()
    result = TableProfiler(client, Visibility.RAW).describe("traces")

    assert result["semantics"] == {"included": False}
    assert result["schema"]["time_index"] == "timestamp"
    assert result["schema"]["primary_keys"] == ["service_name"]
    assert result["samples"]["rows"][0]["service_name"] == "checkoutservice"
    assert not any("table_semantics" in query for query in client.queries)


def test_semantic_profile_returns_structured_facts() -> None:
    client = StubClient()
    result = TableProfiler(client, Visibility.TABLE_SEMANTICS).describe("traces")

    semantics = result["semantics"]
    assert semantics["signal_type"] == "trace"
    assert semantics["source"] == "opentelemetry"
    assert semantics["semantic_options"] == {"trace.version": "v1"}
    assert semantics["entity_declarations"][0]["entity_type"] == "service"


def test_semantic_search_ranks_tables_by_matched_concepts() -> None:
    client = StubClient()

    result = TableProfiler(client, Visibility.TABLE_SEMANTICS).search(
        "redis memory",
        signal_type="metric",
        limit=2,
    )

    assert result["matched_table_count"] == 3
    assert result["truncated"] is True
    assert result["matches"][0]["table"] == "redis_Redis_used_memory"
    assert result["matches"][0]["matched_terms"] == ["redis", "memory"]
    assert "signal_type = 'metric'" in client.queries[0]
    assert "LIMIT 1000" in client.queries[0]


def test_raw_visibility_cannot_search_semantic_catalog() -> None:
    with pytest.raises(TableProfileError, match="unavailable"):
        TableProfiler(StubClient(), Visibility.RAW).search("memory")


def test_semantic_search_does_not_rank_fields_omitted_from_sql_predicate() -> None:
    client = StubClient()

    result = TableProfiler(client, Visibility.TABLE_SEMANTICS).search("prometheus")

    assert result["matched_table_count"] == 0
    assert result["matches"] == []
    assert "COALESCE(source" not in client.queries[0]
