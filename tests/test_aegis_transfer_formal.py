import hashlib
import json
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

import semantic_rca_bench.cli as cli_module
from semantic_rca_bench.aegis_transfer_formal import (
    FormalRunError,
    bind_formal_execution,
    build_formal_preflight_report,
    execute_formal_runs,
    formal_source_semantic_sha256,
    validate_formal_report,
    write_formal_report,
)
from semantic_rca_bench.aegis_transfer_protocol import (
    DEFAULT_PROTOCOL_FIXTURE,
    audit_transfer_protocol,
    load_transfer_protocol_fixture,
)
from semantic_rca_bench.aegis_transfer_release import build_measurement_artifact
from semantic_rca_bench.aegis_transfer_scorer import (
    DELAY_SCORER_FIXTURE,
    audit_transfer_scorer,
    load_transfer_scorer_fixture,
)
from semantic_rca_bench.contracts import (
    AgentRun,
    AgentRunner,
    AgentUsage,
    CaseInput,
    DatabaseLoad,
    Diagnosis,
    Evidence,
    FaultCategory,
    QueryResult,
    ToolTrace,
    Visibility,
)
from semantic_rca_bench.datasets.aegis_transfer import (
    AegisTransferCase,
    AegisTransferGroundTruth,
)


def _query() -> str:
    return """WITH paired AS (
  SELECT CASE
           WHEN c.timestamp >= '2025-07-20 12:32:50'
            AND c.timestamp < '2025-07-20 12:36:50' THEN 'normal'
           WHEN c.timestamp >= '2025-07-20 12:36:50'
            AND c.timestamp < '2025-07-20 12:40:49' THEN 'abnormal'
         END AS period,
         s.duration_nano AS server_duration_ns
  FROM traces c
  JOIN traces s ON c.trace_id = s.trace_id AND s.parent_span_id = c.span_id
  WHERE c.span_kind = 'SPAN_KIND_CLIENT'
    AND s.span_kind = 'SPAN_KIND_SERVER'
    AND c.service_name = 'ts-route-plan-service'
    AND s.service_name = 'ts-travel2-service'
    AND s.span_name = 'POST /api/v1/travel2service/trips/left'
    AND c.timestamp >= '2025-07-20 12:32:50'
    AND c.timestamp < '2025-07-20 12:40:49'
)
SELECT period, COUNT(*) AS span_count, MAX(server_duration_ns) AS max_duration_ns
FROM paired
GROUP BY period
ORDER BY period"""


def _result() -> QueryResult:
    return QueryResult(
        query_id="private-query-id",
        columns=["period", "span_count", "max_duration_ns"],
        rows=[["abnormal", 25, 3_252_068_825], ["normal", 37, 846_092_899]],
        elapsed_seconds=12.5,
    )


def _source_audit() -> dict[str, object]:
    expected = {
        "normal": {"count": 37, "max_duration_ns": 846_092_899},
        "abnormal": {"count": 25, "max_duration_ns": 3_252_068_825},
    }
    edge = {
        "src_type": "service",
        "src_id": "ts-route-plan-service",
        "dst_type": "service",
        "dst_id": "ts-travel2-service",
        "rel_type": "calls",
        "provenance": "trace",
        "request_count": 62,
        "error_count": 0,
    }
    return {
        "audit_schema_version": 1,
        "mode": "aegis-transfer-no-model-audit",
        "dataset_revision": "dataset",
        "adapter_revision": "adapter",
        "license": {
            "benchmark_code": "Apache-2.0",
            "source_dataset_record": "CC-BY-4.0",
            "reviewer_artifact_data_license": "unclear",
            "publication_mode": "pinned-downloader; telemetry not redistributed",
        },
        "pinned_source": {"verified": True, "expected_md5": "abc", "observed_md5": "abc"},
        "selection_audit": {
            "selection": {"seed": "seed"},
            "frozen_selection_gate": {
                "manifest_name": "aegis-transfer-v25-selection.json",
                "pass": True,
            },
        },
        "case": {
            "agent_facing": {
                "case_id": "aegis-transfer-002",
                "time_start": 1753014770,
                "time_end": 1753015249,
                "alert_time": 1753015010,
                "fault_taxonomy": [],
            },
            "source_mapping": {
                "agent_case_id": "aegis-transfer-002",
                "source_case": "ts8-ts-route-plan-service-request-delay-5dmjfm",
            },
            "normal_window": [1753014770, 1753015010],
            "abnormal_window": [1753015010, 1753015249],
            "ground_truth_services": ["ts-route-plan-service", "ts-travel2-service"],
            "declared_edge": ["ts-route-plan-service", "ts-travel2-service"],
            "fault_type": "HTTPRequestDelay",
        },
        "greptimedb": {"head": "head", "branch": "branch"},
        "source_audit": {"source_row_counts": {"trace_rows": 62}},
        "ingestion": {
            "source_row_counts": {"trace_rows": 62},
            "protocol_counts": {"traces": {"accepted": 62, "rejected": 0}},
            "expected_stored_row_counts": {"trace_rows": 62},
            "stored_row_counts": {"trace_rows": 62},
            "stored_row_counts_match": True,
            "protocol_rejections_zero": True,
            "id_remapping": {"trace_id": 0, "span_id": 0},
            "source_identity": {
                "service_counts_match": True,
                "span_kind_counts_match": True,
                "status_code_counts_match": True,
                "source_span_kind_counts": {"SPAN_KIND_CLIENT": 62},
                "stored_span_kind_counts": {"SPAN_KIND_CLIENT": 62},
                "source_status_code_counts": {"STATUS_CODE_UNSET": 62},
                "stored_status_code_counts": {"STATUS_CODE_UNSET": 62},
            },
        },
        "edge_equality": {
            "raw_edge_query": "SELECT raw edges",
            "graph_edge_query": "SELECT graph edges",
            "normalized_raw_edges": [edge],
            "normalized_graph_edges": [edge],
            "raw_edge_set_sha256": "edge-hash",
            "graph_edge_set_sha256": "edge-hash",
            "exact_edge_set_equality": True,
            "window_contract": {
                "source_window": [1753014770, 1753015249],
                "graph_observed_window": [1753014720, 1753015260],
            },
        },
        "mechanism_evidence": {
            "predicate": "source_declared_http_delay_threshold",
            "declared_edge": ["ts-route-plan-service", "ts-travel2-service"],
            "declared_edge_match": True,
            "span_name": "POST /api/v1/travel2service/trips/left",
            "declared_delay_ns": 3_070_000_000,
            "query": _query(),
            "result": _result().model_dump(mode="json"),
            "normalized_result": expected,
            "expected_result": expected,
            "evidence_match": True,
            "pass": True,
        },
        "semantic_surfaces": {"coverage": {"graph": {"status": "relational"}}},
        "no_model_gates": {"all_passed": True},
        "exclusive_instance": {
            "ports": {"http": 40000},
            "process_stopped_by_command": False,
        },
    }


def _case() -> AegisTransferCase:
    paths = (Path("normal.parquet"), Path("abnormal.parquet"))
    return AegisTransferCase(
        agent_case_id="aegis-transfer-002",
        source_case="ts8-ts-route-plan-service-request-delay-5dmjfm",
        dataset="dataset",
        system="Train Ticket",
        root=Path("case"),
        input=CaseInput(
            case_token="aegis-transfer-002",
            time_start=1753014770,
            time_end=1753015249,
            alert_time=1753015010,
            database="case_02",
            fault_taxonomy=[],
        ),
        ground_truth=AegisTransferGroundTruth(
            services=("ts-route-plan-service", "ts-travel2-service"),
            declared_edge=("ts-route-plan-service", "ts-travel2-service"),
            fault_type="HTTPRequestDelay",
        ),
        normal_window=(1753014770, 1753015010),
        abnormal_window=(1753015010, 1753015249),
        gauge_paths=paths,
        sum_paths=paths,
        histogram_paths=paths,
        log_paths=paths,
        trace_paths=paths,
        selected_manifest={},
    )


def _audits():
    source = _source_audit()
    scorer_fixture = load_transfer_scorer_fixture(DELAY_SCORER_FIXTURE)
    protocol_fixture = load_transfer_protocol_fixture(DEFAULT_PROTOCOL_FIXTURE)
    scorer = audit_transfer_scorer(source, scorer_fixture, DELAY_SCORER_FIXTURE)
    protocol = audit_transfer_protocol(
        protocol_fixture,
        DEFAULT_PROTOCOL_FIXTURE,
        scorer_fixture,
        DELAY_SCORER_FIXTURE,
        source,
        scorer,
    )
    return source, scorer, protocol, scorer_fixture, protocol_fixture


def _preflight():
    source, scorer, protocol, scorer_fixture, protocol_fixture = _audits()
    report = build_formal_preflight_report(
        source,
        scorer,
        protocol,
        scorer_fixture,
        DELAY_SCORER_FIXTURE,
        protocol_fixture,
        DEFAULT_PROTOCOL_FIXTURE,
    )
    return report, source, scorer, protocol, scorer_fixture, protocol_fixture


def _agent_run(visibility: Visibility, model: str, *, error: str | None = None) -> AgentRun:
    result = _result()
    return AgentRun(
        run_id=f"private-{model}-{visibility.value}",
        visibility=visibility,
        model=model,
        runner=AgentRunner.API,
        diagnosis=(
            Diagnosis(
                affected_component="ts-route-plan-service",
                causal_dependency="ts-travel2-service",
                fault_category=FaultCategory.DELAY,
                fault_type="HTTP request delay",
                confidence=1,
                evidence=[Evidence(query_id=result.query_id, claim="private evidence claim")],
                explanation="private provider explanation",
            )
            if error is None
            else None
        ),
        error=error,
        tool_calls=(
            [
                ToolTrace(
                    tool_name="execute_sql",
                    input={"query": _query()},
                    query_id=result.query_id,
                    output=result.model_dump(mode="json"),
                    database_load=DatabaseLoad(query_count=1, rows_returned=2),
                )
            ]
            if error is None
            else []
        ),
        tool_calls_requested=1 if error is None else 0,
        usage=AgentUsage(input_tokens=100, output_tokens=20),
        elapsed_seconds=1,
        responses=[{"thinking": "private provider thinking"}],
    )


class _Client:
    database = "case_02"

    @contextmanager
    def measure_query_load(self):
        yield DatabaseLoad(query_count=1, rows_returned=2, max_concurrency=1)


def _bind(report, source, scorer, protocol, scorer_fixture, protocol_fixture):
    bind_formal_execution(
        report,
        _case(),
        source,
        scorer,
        protocol,
        {"graph": {"status": "relational"}},
        scorer_fixture,
        DELAY_SCORER_FIXTURE,
        protocol_fixture,
        DEFAULT_PROTOCOL_FIXTURE,
    )


def test_preflight_expands_frozen_schedule_without_provider_access(monkeypatch) -> None:
    def fail_provider(*args, **kwargs):
        raise AssertionError("preflight must not create a provider client")

    monkeypatch.setattr("semantic_rca_bench.agent._anthropic_client", fail_provider)

    report, *_ = _preflight()

    assert len(report["schedule"]) == 27
    assert report["execution"]["completed_runs"] == 0
    assert report["authorization"] == {
        "paid_api_required": True,
        "reusable_confirmation_stored": False,
        "confirmation_scope": "per invocation; supplied out of band and never persisted",
        "preflight_calls_provider": False,
    }
    assert [cell["model"] for cell in report["schedule"][::9]] == [
        "deepseek-v4-flash",
        "deepseek-v4-pro",
        "claude-sonnet-5",
    ]
    assert (
        report["bindings"]["scorer_fixture_sha256"]
        == hashlib.sha256(DELAY_SCORER_FIXTURE.read_bytes()).hexdigest()
    )
    assert (
        report["bindings"]["protocol_fixture_sha256"]
        == hashlib.sha256(DEFAULT_PROTOCOL_FIXTURE.read_bytes()).hexdigest()
    )


def test_preflight_command_writes_report_without_provider_access(monkeypatch, tmp_path) -> None:
    source, scorer, protocol, *_ = _audits()
    inputs = {}
    for name, value in (("source", source), ("scorer", scorer), ("protocol", protocol)):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(value, default=str))
        inputs[name] = path
    output = tmp_path / "preflight.json"

    def fail_provider(*args, **kwargs):
        raise AssertionError("preflight command must not create a provider client")

    monkeypatch.setattr("semantic_rca_bench.agent._anthropic_client", fail_provider)
    args = cli_module._parser().parse_args(
        [
            "aegis-transfer-formal-preflight",
            "--source-audit",
            str(inputs["source"]),
            "--scorer-audit",
            str(inputs["scorer"]),
            "--protocol-audit",
            str(inputs["protocol"]),
            "--output",
            str(output),
        ]
    )

    assert cli_module.aegis_transfer_formal_preflight(args) == 0
    assert json.loads(output.read_text())["authorization"]["reusable_confirmation_stored"] is False


def test_formal_runner_executes_exact_schedule_and_uses_graph_window() -> None:
    report, source, scorer, protocol, scorer_fixture, protocol_fixture = _preflight()
    _bind(report, source, scorer, protocol, scorer_fixture, protocol_fixture)
    calls = []

    def fake_agent(gateway, case_input, visibility, **kwargs):
        calls.append((gateway.semantic_graph_window, visibility, kwargs))
        return _agent_run(visibility, kwargs["model"])

    with pytest.raises(ValueError, match="explicitly confirmed"):
        execute_formal_runs(
            _Client(),  # type: ignore[arg-type]
            _case(),
            scorer_fixture,
            DELAY_SCORER_FIXTURE,
            protocol_fixture,
            DEFAULT_PROTOCOL_FIXTURE,
            report,
            paid_api_confirmed=False,
            run_agent_fn=fake_agent,
        )
    execute_formal_runs(
        _Client(),  # type: ignore[arg-type]
        _case(),
        scorer_fixture,
        DELAY_SCORER_FIXTURE,
        protocol_fixture,
        DEFAULT_PROTOCOL_FIXTURE,
        report,
        paid_api_confirmed=True,
        run_agent_fn=fake_agent,
    )

    assert report["execution"] == {
        "expected_runs": 27,
        "completed_runs": 27,
        "runner_errors": 0,
        "budget_exhaustions": 0,
        "complete": True,
    }
    assert [item["model"] for item in report["runs"]] == [
        item["model"] for item in report["schedule"]
    ]
    assert all(
        window == (1753014720, 1753015260)
        for window, visibility, _ in calls
        if visibility is Visibility.SEMANTIC_GRAPH
    )
    assert all(call[2]["max_tool_calls"] == 48 for call in calls)
    assert all(call[2]["max_turns"] == 58 for call in calls)
    assert all(
        item["execution_bindings"]["source_semantic_sha256"]
        == report["bindings"]["source_semantic_sha256"]
        for item in report["runs"]
    )


def test_formal_resume_keeps_failed_cell_and_continues_with_next_cell() -> None:
    report, source, scorer, protocol, scorer_fixture, protocol_fixture = _preflight()
    _bind(report, source, scorer, protocol, scorer_fixture, protocol_fixture)
    first_model = report["schedule"][0]["model"]

    def failed_agent(gateway, case_input, visibility, **kwargs):
        return _agent_run(visibility, kwargs["model"], error="provider unavailable")

    with pytest.raises(FormalRunError, match="persisted error"):
        execute_formal_runs(
            _Client(),  # type: ignore[arg-type]
            _case(),
            scorer_fixture,
            DELAY_SCORER_FIXTURE,
            protocol_fixture,
            DEFAULT_PROTOCOL_FIXTURE,
            report,
            paid_api_confirmed=True,
            run_agent_fn=failed_agent,
        )

    resumed_models = []

    def resumed_agent(gateway, case_input, visibility, **kwargs):
        resumed_models.append(kwargs["model"])
        return _agent_run(visibility, kwargs["model"])

    execute_formal_runs(
        _Client(),  # type: ignore[arg-type]
        _case(),
        scorer_fixture,
        DELAY_SCORER_FIXTURE,
        protocol_fixture,
        DEFAULT_PROTOCOL_FIXTURE,
        report,
        paid_api_confirmed=True,
        run_agent_fn=resumed_agent,
    )

    assert len(resumed_models) == 26
    assert resumed_models[0] == first_model
    assert report["runs"][0]["run"]["error"] == "provider unavailable"
    assert report["execution"]["runner_errors"] == 1
    assert report["execution"]["complete"] is True
    artifact = build_measurement_artifact(
        report,
        source,
        scorer,
        protocol,
        scorer_fixture,
        DELAY_SCORER_FIXTURE,
        protocol_fixture,
        DEFAULT_PROTOCOL_FIXTURE,
    )
    assert artifact["experiment"]["runs"][0]["execution"]["runner_error"] is True
    assert "provider unavailable" not in str(artifact)


def test_formal_runner_persists_and_rejects_wrong_scheduled_model() -> None:
    report, source, scorer, protocol, scorer_fixture, protocol_fixture = _preflight()
    _bind(report, source, scorer, protocol, scorer_fixture, protocol_fixture)

    def wrong_model_agent(gateway, case_input, visibility, **kwargs):
        return _agent_run(visibility, "claude-sonnet-5")

    with pytest.raises(FormalRunError, match="runner violated"):
        execute_formal_runs(
            _Client(),  # type: ignore[arg-type]
            _case(),
            scorer_fixture,
            DELAY_SCORER_FIXTURE,
            protocol_fixture,
            DEFAULT_PROTOCOL_FIXTURE,
            report,
            paid_api_confirmed=True,
            run_agent_fn=wrong_model_agent,
        )

    assert report["runs"][0]["run"]["model"] == "claude-sonnet-5"
    assert report["runs"][0]["evaluation"]["runner_contract_match"] is False
    with pytest.raises(ValueError, match="violates the frozen runner contract"):
        validate_formal_report(
            report,
            scorer_fixture,
            DELAY_SCORER_FIXTURE,
            protocol_fixture,
            DEFAULT_PROTOCOL_FIXTURE,
        )


def test_formal_resume_rejects_nonprefix_or_tampered_evaluation() -> None:
    report, source, scorer, protocol, scorer_fixture, protocol_fixture = _preflight()
    _bind(report, source, scorer, protocol, scorer_fixture, protocol_fixture)
    cell = report["schedule"][1]
    run = _agent_run(Visibility(cell["visibility"]), cell["model"])
    report["runs"] = [
        {
            **cell,
            "run": run.model_dump(mode="json"),
            "evaluation": {},
            "database_load": DatabaseLoad().model_dump(mode="json"),
        }
    ]
    report["execution"]["completed_runs"] = 1

    with pytest.raises(ValueError, match="exact schedule prefix"):
        validate_formal_report(
            report,
            scorer_fixture,
            DELAY_SCORER_FIXTURE,
            protocol_fixture,
            DEFAULT_PROTOCOL_FIXTURE,
        )


def test_formal_source_binding_ignores_instance_metadata_but_not_edges() -> None:
    source = _source_audit()
    first = formal_source_semantic_sha256(source)
    source["exclusive_instance"]["ports"]["http"] = 50000
    source["mechanism_evidence"]["result"]["query_id"] = "another-id"
    source["mechanism_evidence"]["result"]["elapsed_seconds"] = 99

    assert formal_source_semantic_sha256(source) == first
    source["edge_equality"]["normalized_raw_edges"][0]["request_count"] = 61
    assert formal_source_semantic_sha256(source) != first


def test_measurement_export_rescores_all_models_and_removes_private_payloads() -> None:
    report, source, scorer, protocol, scorer_fixture, protocol_fixture = _preflight()
    _bind(report, source, scorer, protocol, scorer_fixture, protocol_fixture)

    def fake_agent(gateway, case_input, visibility, **kwargs):
        return _agent_run(visibility, kwargs["model"])

    execute_formal_runs(
        _Client(),  # type: ignore[arg-type]
        _case(),
        scorer_fixture,
        DELAY_SCORER_FIXTURE,
        protocol_fixture,
        DEFAULT_PROTOCOL_FIXTURE,
        report,
        paid_api_confirmed=True,
        run_agent_fn=fake_agent,
    )
    artifact = build_measurement_artifact(
        report,
        source,
        scorer,
        protocol,
        scorer_fixture,
        DELAY_SCORER_FIXTURE,
        protocol_fixture,
        DEFAULT_PROTOCOL_FIXTURE,
    )

    assert artifact["analysis_role"] == "measurement"
    assert artifact["experiment"]["cross_model_pooling"] is False
    assert set(artifact["experiment"]["model_reports"]) == {
        "deepseek-v4-flash",
        "deepseek-v4-pro",
        "claude-sonnet-5",
    }
    assert len(artifact["experiment"]["runs"]) == 27
    assert all(
        report["successful_runs"] == 9
        for report in artifact["experiment"]["model_reports"].values()
    )
    serialized = str(artifact)
    for forbidden in (
        "private provider thinking",
        "private provider explanation",
        "private evidence claim",
        "private-query-id",
        "private-deepseek",
    ):
        assert forbidden not in serialized


def test_atomic_formal_report_write_round_trips(tmp_path: Path) -> None:
    report, *_ = _preflight()
    path = tmp_path / "formal.json"

    write_formal_report(path, report)

    assert path.read_text().endswith("\n")
    assert not list(tmp_path.glob("*.tmp"))


def test_formal_report_write_rejects_non_json_types(tmp_path: Path) -> None:
    report, *_ = _preflight()
    report["invalid"] = datetime.now(UTC)
    path = tmp_path / "formal.json"

    with pytest.raises(TypeError):
        write_formal_report(path, report)

    assert not path.exists()
