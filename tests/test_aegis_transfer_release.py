import copy
import json
from pathlib import Path

import pytest

from semantic_rca_bench.aegis_transfer_release import (
    build_release_artifact,
    canonical_sha256,
    load_pilot_scorer_fixture,
)
from semantic_rca_bench.aegis_transfer_scorer import (
    evaluate_aegis_transfer_run,
    source_transfer_audit_sha256,
)
from semantic_rca_bench.contracts import (
    AgentRun,
    AgentUsage,
    DatabaseLoad,
    Diagnosis,
    Evidence,
    FaultCategory,
    QueryResult,
    ToolTrace,
    Visibility,
)


def _query() -> str:
    return """WITH paired AS (
  SELECT CASE
           WHEN c.timestamp >= '2025-07-19 09:52:38'
            AND c.timestamp < '2025-07-19 09:56:38' THEN 'normal'
           WHEN c.timestamp >= '2025-07-19 09:56:38'
            AND c.timestamp < '2025-07-19 10:00:38' THEN 'abnormal'
         END AS period,
         c."span_attributes.http.request.method" AS client_method,
         s."span_attributes.http.request.method" AS server_method
  FROM traces c
  JOIN traces s ON c.trace_id = s.trace_id AND s.parent_span_id = c.span_id
  WHERE c.span_kind = 'SPAN_KIND_CLIENT'
    AND s.span_kind = 'SPAN_KIND_SERVER'
    AND c.service_name = 'ts-security-service'
    AND s.service_name = 'ts-order-other-service'
)
SELECT period, side, method, COUNT(*) AS span_count
FROM (
  SELECT period, 'server' AS side, server_method AS method FROM paired WHERE period = 'normal'
  UNION ALL
  SELECT period, 'client' AS side, client_method AS method FROM paired WHERE period = 'abnormal'
  UNION ALL
  SELECT period, 'server' AS side, server_method AS method FROM paired WHERE period = 'abnormal'
) evidence
GROUP BY period, side, method"""


def _result() -> QueryResult:
    return QueryResult(
        query_id="private-query-id",
        columns=["period", "side", "method", "span_count"],
        rows=[
            ["normal", "server", "GET", 57],
            ["abnormal", "client", "GET", 758],
            ["abnormal", "server", "OPTIONS", 758],
        ],
        elapsed_seconds=12.5,
    )


def _agent_run(visibility: Visibility, run_id: str) -> AgentRun:
    result = _result()
    return AgentRun(
        run_id=run_id,
        visibility=visibility,
        model="deepseek-v4-flash",
        diagnosis=Diagnosis(
            affected_component="ts-security-service",
            causal_dependency="ts-order-other-service",
            fault_category=FaultCategory.OTHER,
            fault_type="HTTP request method replacement",
            confidence=0.9,
            evidence=[Evidence(query_id=result.query_id, claim="private source-derived claim")],
            alternative_candidates=["private alternative"],
            explanation="private provider explanation",
        ),
        tool_calls=[
            ToolTrace(
                tool_name="execute_sql",
                input={"query": _query()},
                query_id=result.query_id,
                output=result.model_dump(mode="json"),
                database_load=DatabaseLoad(query_count=1, rows_returned=3),
            )
        ],
        tool_calls_requested=1,
        usage=AgentUsage(input_tokens=100, output_tokens=20),
        elapsed_seconds=99,
        responses=[
            {
                "thinking": "private provider thinking",
                "usage": {
                    "prompt_cache_hit_tokens": 900,
                    "prompt_cache_miss_tokens": 100,
                },
            }
        ],
    )


def _source_audit() -> dict[str, object]:
    expected = {
        "normal_server_methods": {"GET": 57},
        "abnormal_client_methods": {"GET": 758},
        "abnormal_server_methods": {"OPTIONS": 758},
    }
    edge = {
        "src_type": "service",
        "src_id": "ts-security-service",
        "dst_type": "service",
        "dst_id": "ts-order-other-service",
        "rel_type": "calls",
        "provenance": "trace",
        "request_count": 815,
        "error_count": 0,
    }
    return {
        "audit_schema_version": 1,
        "mode": "aegis-transfer-no-model-audit",
        "dataset_revision": "dataset-revision",
        "adapter_revision": "adapter-revision",
        "license": {
            "benchmark_code": "Apache-2.0",
            "source_dataset_record": "CC-BY-4.0",
            "reviewer_artifact_data_license": "not explicitly covered by root Apache-2.0",
            "publication_mode": "pinned-downloader; telemetry not redistributed",
        },
        "pinned_source": {
            "artifact_record": "https://example.test/artifact",
            "expected_md5": "abc",
            "observed_md5": "abc",
            "verified": True,
            "data_redistributed": False,
        },
        "selection_audit": {
            "selection": {"seed": "seed", "selected_case": "source-case"},
            "frozen_selection_gate": {"pass": True},
        },
        "case": {
            "agent_facing": {
                "case_id": "aegis-transfer-001",
                "fault_taxonomy": [],
                "time_start": 1752918758,
                "alert_time": 1752918998,
                "time_end": 1752919238,
            },
            "source_mapping": {
                "agent_case_id": "aegis-transfer-001",
                "source_case": "source-case",
            },
            "normal_window": [1752918758, 1752918998],
            "abnormal_window": [1752918998, 1752919238],
            "ground_truth_services": ["ts-order-other-service", "ts-security-service"],
            "declared_edge": ["ts-security-service", "ts-order-other-service"],
            "fault_type": "HTTPRequestReplaceMethod",
        },
        "greptimedb": {"head": "revision", "branch": "branch"},
        "source_audit": {"reference_causal_graph_ingested": False},
        "ingestion": {
            "source_row_counts": {"metric_rows": 3, "log_rows": 2, "trace_rows": 1},
            "protocol_counts": {"metrics": {}, "logs": {}, "traces": {}},
            "expected_stored_row_counts": {
                "metric_rows": 3,
                "log_rows": 2,
                "trace_rows": 1,
            },
            "stored_row_counts": {"metric_rows": 3, "log_rows": 2, "trace_rows": 1},
            "stored_row_counts_match": True,
            "protocol_rejections_zero": True,
            "id_remapping": {"trace_ids": 0, "span_ids": 0, "pass": True},
            "source_identity": {
                "service_counts_match": True,
                "span_kind_counts_match": True,
                "status_code_counts_match": True,
                "source_span_kind_counts": {"SPAN_KIND_CLIENT": 1},
                "stored_span_kind_counts": {"SPAN_KIND_CLIENT": 1},
                "source_status_code_counts": {"STATUS_CODE_UNSET": 1},
                "stored_status_code_counts": {"STATUS_CODE_UNSET": 1},
            },
        },
        "edge_equality": {
            "raw_edge_query": "SELECT raw edge",
            "graph_edge_query": "SELECT graph edge",
            "raw_edge_result": {"query_id": "raw-id", "elapsed_seconds": 1.2},
            "graph_edge_result": {"query_id": "graph-id", "elapsed_seconds": 2.3},
            "normalized_raw_edges": [edge],
            "normalized_graph_edges": [edge],
            "raw_edge_set_sha256": "edge-hash",
            "graph_edge_set_sha256": "edge-hash",
            "exact_edge_set_equality": True,
            "window_contract": {"source_window": [1752918758, 1752919238]},
        },
        "mechanism_evidence": {
            "predicate": "source_declared_http_method_replacement",
            "query": _query(),
            "result": {
                **_result().model_dump(mode="json"),
                "query_id": "mechanism-id",
                "elapsed_seconds": 3.4,
            },
            "normalized_result": expected,
            "expected_result": expected,
            "declared_edge": ["ts-security-service", "ts-order-other-service"],
            "declared_edge_match": True,
            "original_method": "GET",
            "replacement_method": "OPTIONS",
            "evidence_match": True,
            "pass": True,
        },
        "semantic_surfaces": {
            "coverage": {
                "graph": {"status": "relational", "distinct_relationship_count": 1},
                "table_semantics": {"table_count": 3},
            }
        },
        "no_model_gates": {"all_passed": True},
        "exclusive_instance": {
            "ports": {"http": 12345},
            "run_dir": "/private/tmp/private-run-dir",
        },
    }


def _reports() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    fixture = load_pilot_scorer_fixture()
    source = _source_audit()
    scorer = {
        "fixture_sha256": canonical_sha256(fixture.model_dump(mode="json", exclude_none=True)),
        "source_transfer_audit_sha256": source_transfer_audit_sha256(source),
        "no_model_gates": {"all_passed": True},
        "synthetic_regressions": {
            "canonical_positive": {
                "expected_success": True,
                "observed_success": True,
                "pass": True,
                "evaluation": {"private": "detail"},
            }
        },
    }
    levels = list(fixture.canonical_api_runner.visibility_levels)
    cells = []
    for repetition in range(fixture.canonical_api_runner.repetitions):
        for position, visibility in enumerate(levels):
            run = _agent_run(visibility, f"private-run-{repetition}-{visibility.value}")
            cells.append(
                {
                    "repetition": repetition,
                    "position": position,
                    "run": run.model_dump(mode="json"),
                    "evaluation": evaluate_aegis_transfer_run(run, fixture).model_dump(mode="json"),
                    "database_load": DatabaseLoad(
                        query_count=1,
                        rows_returned=3,
                        query_elapsed_seconds=12.5,
                        max_concurrency=1,
                    ).model_dump(mode="json"),
                }
            )
    run_report = {
        "mode": "aegis-transfer-canonical-api-run",
        "case_role": "measurement",
        "protocol": {"version": 24},
        "canonical_api_runner": fixture.canonical_api_runner.model_dump(mode="json"),
        "case": {"case_id": "aegis-transfer-001", "fault_taxonomy": []},
        "graph_window_contract": {"source_window": [1752918758, 1752919238]},
        "semantic_coverage": {"graph": {"status": "relational"}},
        "orders": [],
        "execution": {
            "expected_runs": 9,
            "completed_runs": 9,
            "runner_errors": 0,
            "budget_exhaustions": 0,
            "complete": True,
        },
        "runs": cells,
        "source_transfer_audit_sha256": source_transfer_audit_sha256(source),
        "scorer_audit_sha256": canonical_sha256(scorer),
        "scorer_fixture_sha256": canonical_sha256(
            fixture.model_dump(mode="json", exclude_none=True)
        ),
    }
    return run_report, source, scorer


def test_release_artifact_is_sanitized_and_semantically_hashed() -> None:
    run_report, source, scorer = _reports()
    artifact = build_release_artifact(
        run_report,
        source,
        scorer,
        load_pilot_scorer_fixture(),
        private_input_sha256={"run_report": "private-file-hash"},
    )

    assert artifact["analysis_role"] == "development"
    assert artifact["historical_run_case_role"] == "measurement"
    assert artifact["experiment"]["descriptive_summary"]["successful_runs"] == 9
    first = artifact["experiment"]["runs"][0]
    assert first["citations"] == [
        {"ordinal": 1, "execution_valid": True, "supports_mechanism": True}
    ]
    assert first["supporting_mechanism_queries"][0]["result"] == {
        "columns": ["period", "side", "method", "span_count"],
        "rows": [
            ["normal", "server", "GET", 57],
            ["abnormal", "client", "GET", 758],
            ["abnormal", "server", "OPTIONS", 758],
        ],
        "truncated": False,
    }
    assert canonical_sha256(artifact)
    text = str(artifact)
    assert "private provider thinking" not in text
    assert "private provider explanation" not in text
    assert "private source-derived claim" not in text
    assert "private alternative" not in text
    assert "private-run-" not in text
    assert "private-query-id" not in text
    assert "/private/tmp/private-run-dir" not in text
    payload = {key: value for key, value in artifact.items() if key != "integrity"}
    assert artifact["integrity"]["semantic_payload_sha256"] == canonical_sha256(payload)


def test_source_semantic_hash_ignores_run_local_metadata_but_not_edges() -> None:
    run_report, source, scorer = _reports()
    fixture = load_pilot_scorer_fixture()
    first = build_release_artifact(run_report, source, scorer, fixture)
    changed = copy.deepcopy(source)
    changed["exclusive_instance"]["ports"]["http"] = 54321
    changed["exclusive_instance"]["run_dir"] = "/another/private/path"
    changed["edge_equality"]["raw_edge_result"]["query_id"] = "another-id"
    changed["edge_equality"]["raw_edge_result"]["elapsed_seconds"] = 99
    changed["mechanism_evidence"]["result"]["query_id"] = "another-mechanism-id"
    changed["mechanism_evidence"]["result"]["elapsed_seconds"] = 88
    run_report["source_transfer_audit_sha256"] = source_transfer_audit_sha256(changed)
    scorer["source_transfer_audit_sha256"] = source_transfer_audit_sha256(changed)
    run_report["scorer_audit_sha256"] = canonical_sha256(scorer)
    second = build_release_artifact(run_report, changed, scorer, fixture)

    assert (
        first["integrity"]["source_semantic_sha256"]
        == second["integrity"]["source_semantic_sha256"]
    )

    changed["edge_equality"]["normalized_raw_edges"][0]["request_count"] = 814
    run_report["source_transfer_audit_sha256"] = source_transfer_audit_sha256(changed)
    scorer["source_transfer_audit_sha256"] = source_transfer_audit_sha256(changed)
    run_report["scorer_audit_sha256"] = canonical_sha256(scorer)
    third = build_release_artifact(run_report, changed, scorer, fixture)
    assert (
        first["integrity"]["source_semantic_sha256"] != third["integrity"]["source_semantic_sha256"]
    )


def test_release_artifact_rejects_tampered_evaluation() -> None:
    run_report, source, scorer = _reports()
    run_report["runs"][0]["evaluation"]["success"] = False

    with pytest.raises(ValueError, match="deterministic rescoring"):
        build_release_artifact(run_report, source, scorer, load_pilot_scorer_fixture())


def test_tracked_development_artifact_is_self_consistent_and_sanitized() -> None:
    artifact = json.loads(
        Path("artifacts/development/aegis-transfer-v24-deepseek.json").read_text()
    )
    payload = {key: value for key, value in artifact.items() if key != "integrity"}

    assert artifact["integrity"]["semantic_payload_sha256"] == canonical_sha256(payload)
    assert artifact["integrity"]["source_semantic_sha256"] == canonical_sha256(artifact["source"])
    assert artifact["experiment"]["descriptive_summary"]["successful_runs"] == 0
    assert artifact["experiment"]["descriptive_summary"]["total_runs"] == 9
    serialized = json.dumps(artifact, sort_keys=True)
    for forbidden in (
        "/Users/",
        "/private/tmp",
        '"query_id"',
        '"run_id"',
        '"responses"',
        '"signature"',
    ):
        assert forbidden not in serialized
