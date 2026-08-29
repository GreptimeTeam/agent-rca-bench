import pytest

from semantic_rca_bench.contracts import CaseInput, QueryResult
from semantic_rca_bench.inspect import (
    GraphIsolationError,
    assert_semantic_graph_isolated,
    assert_semantic_graph_window_empty,
    inspect_semantic_surfaces,
    summarize_semantic_surfaces,
)


def test_semantic_coverage_counts_distinct_edges_and_observations() -> None:
    coverage = summarize_semantic_surfaces(
        {
            "table_semantics": {
                "columns": ["signal_type", "source", "table_count"],
                "rows": [["metric", "prometheus", 70], ["trace", "opentelemetry", 1]],
            },
            "entity_declarations": {
                "columns": ["table_name", "entity_declarations"],
                "rows": [
                    [
                        "k8s_container_restarts",
                        '[{"entity_type":"k8s.container","scope":["k8s.namespace.name"]}]',
                    ]
                ],
            },
            "entities": {
                "columns": [
                    "entity_type",
                    "distinct_entity_count",
                    "observation_count",
                    "scoped_observation_count",
                ],
                "rows": [["service", 7, 100, 0], ["host", 2, 20, 2]],
            },
            "relationships": {
                "columns": [
                    "rel_type",
                    "distinct_relationship_count",
                    "observation_count",
                    "max_window_unmatched_count",
                    "max_request_duration",
                ],
                "rows": [["calls", 3, 45, 2, 1.2], ["runs_on", 2, 100, None, None]],
            },
        }
    )

    assert coverage["surface_contract"] == {
        "entity_scope_queryable": True,
        "table_entity_declarations_queryable": True,
        "table_entity_declarations_well_formed": True,
        "relationship_unmatched_count_queryable": True,
        "relationship_duration_max_queryable": True,
        "scope_declaration_count": 1,
        "current": True,
    }
    assert coverage["table_semantics"] == {
        "table_count": 71,
        "groups": [
            {"signal_type": "metric", "source": "prometheus", "table_count": 70},
            {"signal_type": "trace", "source": "opentelemetry", "table_count": 1},
        ],
    }
    assert coverage["graph"] == {
        "status": "relational",
        "distinct_entity_count": 9,
        "entity_types": {"service": 7, "host": 2},
        "distinct_relationship_count": 5,
        "relationship_observation_count": 145,
        "relationship_types": {"calls": 3, "runs_on": 2},
        "errors": [],
    }


def test_malformed_entity_declaration_row_fails_surface_contract() -> None:
    coverage = summarize_semantic_surfaces(
        {
            "entity_declarations": {
                "columns": ["table_name", "entity_declarations"],
                "rows": [["traces"]],
            },
            "entities": {
                "columns": ["scoped_observation_count"],
                "rows": [],
            },
            "relationships": {
                "columns": ["max_window_unmatched_count", "max_request_duration"],
                "rows": [],
            },
        }
    )

    contract = coverage["surface_contract"]
    assert contract["scope_declaration_count"] is None
    assert contract["table_entity_declarations_well_formed"] is False
    assert contract["current"] is False


class StubClient:
    def __init__(self, results: list[QueryResult] | None = None) -> None:
        self.queries: list[str] = []
        self.results = results or []

    def query(self, statement: str, *, max_rows: int = 200) -> QueryResult:
        self.queries.append(statement)
        if self.results:
            return self.results.pop(0)
        return QueryResult(
            query_id=f"q{len(self.queries)}",
            columns=[],
            rows=[],
            elapsed_seconds=0,
        )


def test_relationship_coverage_deduplicates_endpoint_tuple() -> None:
    client = StubClient()
    inspect_semantic_surfaces(
        client,  # type: ignore[arg-type]
        CaseInput(case_token="case", time_start=0, time_end=60, alert_time=60),
    )

    relationship_query = next(
        query for query in client.queries if "semantic_relationships" in query
    )
    assert "GROUP BY src_type, src_id, dst_type, dst_id, rel_type, provenance" in relationship_query
    assert "COUNT(*) AS distinct_relationship_count" in relationship_query
    assert "MAX(unmatched_count) AS max_window_unmatched_count" in relationship_query
    assert "MAX(duration_max) AS max_request_duration" in relationship_query
    entity_query = next(query for query in client.queries if "semantic_entities" in query)
    assert "COUNT(scope) AS scoped_observation_count" in entity_query


def test_graph_isolation_accepts_an_instance_without_other_sources() -> None:
    client = StubClient()

    assert_semantic_graph_isolated(client, "case_a")  # type: ignore[arg-type]

    assert "table_schema <> 'case_a'" in client.queries[0]
    assert "entity_declarations IS NOT NULL" in client.queries[0]


def test_graph_isolation_rejects_sources_from_another_database() -> None:
    client = StubClient(
        [
            QueryResult(
                query_id="q1",
                columns=["table_schema", "table_name"],
                rows=[["case_b", "greptime_traces"]],
                elapsed_seconds=0,
            )
        ]
    )

    with pytest.raises(GraphIsolationError, match="case_b.greptime_traces"):
        assert_semantic_graph_isolated(client, "case_a")  # type: ignore[arg-type]


def test_graph_window_isolation_checks_both_surfaces_before_ingest() -> None:
    client = StubClient(
        [
            QueryResult(query_id="q1", columns=["count"], rows=[[0]], elapsed_seconds=0),
            QueryResult(query_id="q2", columns=["count"], rows=[[0]], elapsed_seconds=0),
        ]
    )
    case = CaseInput(case_token="case", time_start=0, time_end=60, alert_time=30)

    isolation = assert_semantic_graph_window_empty(client, case)  # type: ignore[arg-type]

    assert isolation["mode"] == "empty-window-before-ingest"
    assert isolation["entity_rows"] == 0
    assert isolation["relationship_rows"] == 0
    assert "semantic_entities" in client.queries[0]
    assert "semantic_relationships" in client.queries[1]
    assert all("observed_at < '1970-01-01 00:01:00'" in query for query in client.queries)
