import hashlib
import json
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

import semantic_rca_bench.aegis_transfer_formal as formal_module
import semantic_rca_bench.aegis_transfer_release as release_module
import semantic_rca_bench.cli as cli_module
from semantic_rca_bench.aegis_transfer_formal import (
    FormalRunError,
    bind_formal_execution,
    build_formal_preflight_report,
    execute_formal_runs,
    formal_schedule,
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
    FORMAL_SCORER_FIXTURE,
    audit_transfer_scorer,
    load_transfer_scorer_fixture,
)
from semantic_rca_bench.contracts import (
    AgentRun,
    AgentRunner,
    AgentUsage,
    ApiTransport,
    CaseInput,
    CausalScope,
    DatabaseLoad,
    Diagnosis,
    Evidence,
    EvidenceClaimType,
    FaultCategory,
    MechanismCode,
    QueryResult,
    ToolTrace,
    Visibility,
)
from semantic_rca_bench.datasets.aegis_transfer import (
    AegisTransferCase,
    AegisTransferGroundTruth,
    canonical_mechanism_evidence_query,
)


def _result() -> QueryResult:
    return QueryResult(
        query_id="private-query-id",
        columns=["period", "sample_count", "min_restarts", "max_restarts"],
        rows=[["normal", 24, 0.0, 0.0], ["abnormal", 24, 1.0, 1.0]],
        elapsed_seconds=12.5,
    )


def _source_audit() -> dict[str, object]:
    expected = {
        "normal": {"count": 24, "min_restarts": 0.0, "max_restarts": 0.0},
        "abnormal": {"count": 24, "min_restarts": 1.0, "max_restarts": 1.0},
    }
    edge = {
        "src_type": "service",
        "src_id": "ts-auth-service",
        "dst_type": "service",
        "dst_id": "ts-route-plan-service",
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
                "manifest_name": "aegis-transfer-v31-selection.json",
                "pass": True,
            },
        },
        "case": {
            "agent_facing": {
                "case_id": "aegis-transfer-004",
                "time_start": 1752933592,
                "time_end": 1752934072,
                "alert_time": 1752933832,
                "fault_taxonomy": [],
            },
            "source_mapping": {
                "agent_case_id": "aegis-transfer-004",
                "source_case": "ts4-ts-auth-service-pod-failure-97s6xl",
            },
            "normal_window": [1752933592, 1752933832],
            "abnormal_window": [1752933832, 1752934072],
            "ground_truth_services": ["ts-auth-service"],
            "declared_edge": None,
            "fault_type": "PodFailure",
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
                "source_window": [1752933592, 1752934072],
                "graph_observed_window": [1752933540, 1752934080],
            },
        },
        "mechanism_evidence": {
            "predicate": "source_declared_workload_restart",
            "declared_edge": None,
            "declared_edge_match": True,
            "identity_field": "attr.k8s.container.name",
            "identity_value": "ts-auth-service",
            "declared_pod_identity_match": False,
            "observable": "k8s.container.restarts",
            "query": canonical_mechanism_evidence_query(_case()),
            "result": _result().model_dump(mode="json"),
            "normalized_result": expected,
            "expected_result": expected,
            "evidence_match": True,
            "pass": True,
        },
        "semantic_surfaces": {"coverage": {"graph": {"status": "relational"}}},
        "no_model_gates": {
            "all_passed": True,
            "case_normalized_predicates_source_equivalent": True,
        },
        "exclusive_instance": {
            "ports": {"http": 40000},
            "process_stopped_by_command": False,
        },
    }


def _case() -> AegisTransferCase:
    paths = (Path("normal.parquet"), Path("abnormal.parquet"))
    return AegisTransferCase(
        agent_case_id="aegis-transfer-004",
        source_case="ts4-ts-auth-service-pod-failure-97s6xl",
        dataset="dataset",
        system="Train Ticket",
        root=Path("case"),
        input=CaseInput(
            case_token="aegis-transfer-004",
            time_start=1752933592,
            time_end=1752934072,
            alert_time=1752933832,
            database="case_04",
            fault_taxonomy=[],
        ),
        ground_truth=AegisTransferGroundTruth(
            services=("ts-auth-service",),
            declared_edge=None,
            fault_type="PodFailure",
        ),
        normal_window=(1752933592, 1752933832),
        abnormal_window=(1752933832, 1752934072),
        gauge_paths=paths,
        sum_paths=paths,
        histogram_paths=paths,
        log_paths=paths,
        trace_paths=paths,
        selected_manifest={
            "mechanism_evidence": {
                "predicate": "source_declared_workload_restart",
                "metric": "k8s.container.restarts",
                "identity_field": "attr.k8s.container.name",
                "identity_value": "ts-auth-service",
            }
        },
    )


def _audits():
    source = _source_audit()
    scorer_fixture = load_transfer_scorer_fixture(FORMAL_SCORER_FIXTURE)
    protocol_fixture = load_transfer_protocol_fixture(DEFAULT_PROTOCOL_FIXTURE)
    scorer = audit_transfer_scorer(source, scorer_fixture, FORMAL_SCORER_FIXTURE)
    protocol = audit_transfer_protocol(
        protocol_fixture,
        DEFAULT_PROTOCOL_FIXTURE,
        scorer_fixture,
        FORMAL_SCORER_FIXTURE,
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
        FORMAL_SCORER_FIXTURE,
        protocol_fixture,
        DEFAULT_PROTOCOL_FIXTURE,
    )
    return report, source, scorer, protocol, scorer_fixture, protocol_fixture


def _agent_run(visibility: Visibility, model: str, *, error: str | None = None) -> AgentRun:
    result = _result()
    runner_contract = {
        "gpt-5.6-sol": (ApiTransport.OPENAI_RESPONSES, "medium", 16_384),
        "deepseek-v4-pro": (
            ApiTransport.ANTHROPIC_COMPATIBLE_MESSAGES,
            "high",
            16_384,
        ),
        "claude-opus-5": (ApiTransport.ANTHROPIC_MESSAGES, "high", 16_384),
        "claude-fable-5": (ApiTransport.ANTHROPIC_MESSAGES, "high", 16_384),
        "glm-5.3": (ApiTransport.BIGMODEL_CHAT_COMPLETIONS, "max", 16_384),
        "qwen3.8-2.4t-a95b": (
            ApiTransport.DASHSCOPE_CN_BEIJING_RESPONSES,
            "xhigh",
            16_384,
        ),
    }[model]
    return AgentRun(
        run_id=f"private-{model}-{visibility.value}",
        visibility=visibility,
        model=model,
        runner=AgentRunner.API,
        api_transport=runner_contract[0],
        reasoning_effort=runner_contract[1],
        max_output_tokens=runner_contract[2],
        diagnosis=(
            Diagnosis(
                causal_scope=CausalScope.COMPONENT,
                causal_component="ts-auth-service",
                causal_operation=None,
                fault_category=FaultCategory.OTHER,
                mechanism_code=MechanismCode.WORKLOAD_RESTART,
                fault_type="workload restart",
                confidence=1,
                evidence=[
                    Evidence(
                        query_id=result.query_id,
                        claim="private evidence claim",
                        claim_types=[
                            EvidenceClaimType.CAUSAL_LOCUS,
                            EvidenceClaimType.FAULT_MECHANISM,
                        ],
                    )
                ],
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
                    input={"query": canonical_mechanism_evidence_query(_case())},
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
    database = "case_04"

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
        FORMAL_SCORER_FIXTURE,
        protocol_fixture,
        DEFAULT_PROTOCOL_FIXTURE,
    )


def test_preflight_expands_frozen_schedule_without_provider_access(monkeypatch) -> None:
    def fail_provider(*args, **kwargs):
        raise AssertionError("preflight must not create a provider client")

    monkeypatch.setattr("semantic_rca_bench.agent._anthropic_client", fail_provider)

    report, *_ = _preflight()

    assert len(report["schedule"]) == 24
    assert report["execution"]["completed_runs"] == 0
    assert report["authorization"] == {
        "paid_api_required": True,
        "reusable_confirmation_stored": False,
        "confirmation_scope": "per invocation; supplied out of band and never persisted",
        "preflight_calls_provider": False,
    }
    assert [cell["model"] for cell in report["schedule"][::4]] == [
        "gpt-5.6-sol",
        "deepseek-v4-pro",
        "claude-opus-5",
        "claude-fable-5",
        "glm-5.3",
        "qwen3.8-2.4t-a95b",
    ]
    assert (
        report["bindings"]["scorer_fixture_sha256"]
        == hashlib.sha256(FORMAL_SCORER_FIXTURE.read_bytes()).hexdigest()
    )
    assert (
        report["bindings"]["protocol_fixture_sha256"]
        == hashlib.sha256(DEFAULT_PROTOCOL_FIXTURE.read_bytes()).hexdigest()
    )


def test_preflight_binds_pricing_snapshot_across_resume(monkeypatch) -> None:
    report, *_, scorer_fixture, protocol_fixture = _preflight()
    frozen = json.loads(json.dumps(report["pricing_snapshot"]))
    monkeypatch.setitem(
        formal_module.MODEL_PRICING,
        "deepseek-v4-pro",
        {"input_per_million": 999, "output_per_million": 999},
    )

    validate_formal_report(
        report,
        scorer_fixture,
        FORMAL_SCORER_FIXTURE,
        protocol_fixture,
        DEFAULT_PROTOCOL_FIXTURE,
    )
    assert report["pricing_snapshot"] == frozen

    report["pricing_snapshot"]["deepseek-v4-pro"]["input_per_million"] = 998
    with pytest.raises(ValueError, match="pricing snapshot binding drifted"):
        validate_formal_report(
            report,
            scorer_fixture,
            FORMAL_SCORER_FIXTURE,
            protocol_fixture,
            DEFAULT_PROTOCOL_FIXTURE,
        )


def test_v31_protocol_binds_measurement_case_and_strong_model_roster() -> None:
    protocol = load_transfer_protocol_fixture(DEFAULT_PROTOCOL_FIXTURE)
    scorer = load_transfer_scorer_fixture(FORMAL_SCORER_FIXTURE)
    schedule = formal_schedule(protocol)

    assert protocol.agent_case_id == scorer.agent_case_id == "aegis-transfer-004"
    assert protocol.benchmark_protocol_version == 31
    assert protocol.case_role == scorer.case_role == "measurement"
    assert [model.model for model in protocol.models] == [
        "gpt-5.6-sol",
        "deepseek-v4-pro",
        "claude-opus-5",
        "claude-fable-5",
        "glm-5.3",
        "qwen3.8-2.4t-a95b",
    ]
    assert len(schedule) == 24
    assert [cell["model"] for cell in schedule[::4]] == [
        "gpt-5.6-sol",
        "deepseek-v4-pro",
        "claude-opus-5",
        "claude-fable-5",
        "glm-5.3",
        "qwen3.8-2.4t-a95b",
    ]
    assert protocol.models[0].api_transport is ApiTransport.OPENAI_RESPONSES
    assert protocol.models[0].reasoning_effort == "medium"
    assert protocol.models[0].max_output_tokens == 16_384
    assert protocol.models[1].reasoning_effort == "high"
    assert protocol.models[1].max_output_tokens == 16_384
    assert protocol.models[2].reasoning_effort == "high"
    assert protocol.models[2].max_output_tokens == 16_384
    assert protocol.models[3].reasoning_effort == "high"
    assert protocol.models[3].max_output_tokens == 16_384
    assert protocol.models[4].api_transport is ApiTransport.BIGMODEL_CHAT_COMPLETIONS
    assert protocol.models[4].reasoning_effort == "max"
    assert protocol.models[5].api_transport is ApiTransport.DASHSCOPE_CN_BEIJING_RESPONSES
    assert protocol.models[5].reasoning_effort == "xhigh"


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
            "--scorer",
            str(FORMAL_SCORER_FIXTURE),
            "--protocol",
            str(DEFAULT_PROTOCOL_FIXTURE),
            "--output",
            str(output),
        ]
    )

    assert cli_module.aegis_transfer_formal_preflight(args) == 0
    assert json.loads(output.read_text())["authorization"]["reusable_confirmation_stored"] is False


def test_formal_runner_executes_exact_schedule_and_uses_graph_window(monkeypatch) -> None:
    monkeypatch.setattr(formal_module, "require_current_protocol", lambda version: None)
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
            FORMAL_SCORER_FIXTURE,
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
        FORMAL_SCORER_FIXTURE,
        protocol_fixture,
        DEFAULT_PROTOCOL_FIXTURE,
        report,
        paid_api_confirmed=True,
        run_agent_fn=fake_agent,
    )

    assert report["execution"] == {
        "expected_runs": 24,
        "completed_runs": 24,
        "runner_errors": 0,
        "budget_exhaustions": 0,
        "complete": True,
    }
    assert [item["model"] for item in report["runs"]] == [
        item["model"] for item in report["schedule"]
    ]
    assert all(
        window == (1752933540, 1752934080)
        for window, visibility, _ in calls
        if visibility is Visibility.SEMANTIC_GRAPH
    )
    assert all(call[2]["max_tool_calls"] == 48 for call in calls)
    assert all(call[2]["max_turns"] == 58 for call in calls)
    assert calls[0][2]["api_transport"] is ApiTransport.OPENAI_RESPONSES
    assert calls[0][2]["reasoning_effort"] == "medium"
    assert calls[0][2]["max_output_tokens"] == 16_384
    assert all(
        item["execution_bindings"]["source_semantic_sha256"]
        == report["bindings"]["source_semantic_sha256"]
        for item in report["runs"]
    )


def test_formal_runner_can_limit_one_invocation_to_one_pending_cell(monkeypatch) -> None:
    monkeypatch.setattr(formal_module, "require_current_protocol", lambda version: None)
    report, source, scorer, protocol, scorer_fixture, protocol_fixture = _preflight()
    _bind(report, source, scorer, protocol, scorer_fixture, protocol_fixture)
    calls = []

    def fake_agent(gateway, case_input, visibility, **kwargs):
        calls.append(kwargs)
        return _agent_run(visibility, kwargs["model"])

    execute_formal_runs(
        _Client(),  # type: ignore[arg-type]
        _case(),
        scorer_fixture,
        FORMAL_SCORER_FIXTURE,
        protocol_fixture,
        DEFAULT_PROTOCOL_FIXTURE,
        report,
        paid_api_confirmed=True,
        max_new_runs=1,
        run_agent_fn=fake_agent,
    )

    assert len(calls) == 1
    assert len(report["runs"]) == 1
    assert report["runs"][0]["model"] == "gpt-5.6-sol"
    assert report["execution"]["complete"] is False
    validate_formal_report(
        report,
        scorer_fixture,
        FORMAL_SCORER_FIXTURE,
        protocol_fixture,
        DEFAULT_PROTOCOL_FIXTURE,
    )


def test_formal_runner_persists_failed_cell_and_continues_batch(monkeypatch) -> None:
    monkeypatch.setattr(formal_module, "require_current_protocol", lambda version: None)
    report, source, scorer, protocol, scorer_fixture, protocol_fixture = _preflight()
    _bind(report, source, scorer, protocol, scorer_fixture, protocol_fixture)
    first_model = report["schedule"][0]["model"]
    calls = 0

    def failed_agent(gateway, case_input, visibility, **kwargs):
        nonlocal calls
        calls += 1
        return _agent_run(
            visibility,
            kwargs["model"],
            error="provider unavailable" if calls == 1 else None,
        )

    execute_formal_runs(
        _Client(),  # type: ignore[arg-type]
        _case(),
        scorer_fixture,
        FORMAL_SCORER_FIXTURE,
        protocol_fixture,
        DEFAULT_PROTOCOL_FIXTURE,
        report,
        paid_api_confirmed=True,
        run_agent_fn=failed_agent,
    )

    assert calls == 24
    assert report["runs"][0]["model"] == first_model
    assert report["runs"][0]["run"]["error"] == "provider unavailable"
    assert report["execution"]["runner_errors"] == 1
    assert report["execution"]["complete"] is True


def test_formal_runner_persists_and_rejects_wrong_scheduled_model(monkeypatch) -> None:
    monkeypatch.setattr(formal_module, "require_current_protocol", lambda version: None)
    report, source, scorer, protocol, scorer_fixture, protocol_fixture = _preflight()
    _bind(report, source, scorer, protocol, scorer_fixture, protocol_fixture)

    def wrong_model_agent(gateway, case_input, visibility, **kwargs):
        return _agent_run(visibility, "claude-opus-5")

    with pytest.raises(FormalRunError, match="runner violated"):
        execute_formal_runs(
            _Client(),  # type: ignore[arg-type]
            _case(),
            scorer_fixture,
            FORMAL_SCORER_FIXTURE,
            protocol_fixture,
            DEFAULT_PROTOCOL_FIXTURE,
            report,
            paid_api_confirmed=True,
            run_agent_fn=wrong_model_agent,
        )

    assert report["runs"][0]["run"]["model"] == "claude-opus-5"
    assert report["runs"][0]["evaluation"]["runner_contract_match"] is False
    with pytest.raises(ValueError, match="violates the frozen runner contract"):
        validate_formal_report(
            report,
            scorer_fixture,
            FORMAL_SCORER_FIXTURE,
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
            FORMAL_SCORER_FIXTURE,
            protocol_fixture,
            DEFAULT_PROTOCOL_FIXTURE,
        )


def test_formal_runner_checks_protocol_with_wrapped_agent(monkeypatch) -> None:
    report, source, scorer, protocol, scorer_fixture, protocol_fixture = _preflight()
    _bind(report, source, scorer, protocol, scorer_fixture, protocol_fixture)
    called = False

    def wrapped_agent(gateway, case_input, visibility, **kwargs):
        nonlocal called
        called = True
        return _agent_run(visibility, kwargs["model"])

    def reject_protocol(version: int) -> None:
        raise RuntimeError(f"protocol guard called for v{version}")

    monkeypatch.setattr(formal_module, "require_current_protocol", reject_protocol)
    with pytest.raises(RuntimeError, match="protocol guard called for v31"):
        execute_formal_runs(
            _Client(),  # type: ignore[arg-type]
            _case(),
            scorer_fixture,
            FORMAL_SCORER_FIXTURE,
            protocol_fixture,
            DEFAULT_PROTOCOL_FIXTURE,
            report,
            paid_api_confirmed=True,
            run_agent_fn=wrapped_agent,
        )

    assert called is False


def test_formal_source_binding_ignores_instance_metadata_but_not_edges() -> None:
    source = _source_audit()
    first = formal_source_semantic_sha256(source)
    source["exclusive_instance"]["ports"]["http"] = 50000
    source["mechanism_evidence"]["result"]["query_id"] = "another-id"
    source["mechanism_evidence"]["result"]["elapsed_seconds"] = 99

    assert formal_source_semantic_sha256(source) == first
    source["edge_equality"]["normalized_raw_edges"][0]["request_count"] = 61
    assert formal_source_semantic_sha256(source) != first


def test_formal_source_binding_includes_mechanism_observable() -> None:
    source = _source_audit()
    source["mechanism_evidence"]["observable"] = "server.timestamp - client.timestamp"
    first = formal_source_semantic_sha256(source)

    source["mechanism_evidence"]["observable"] = "server.duration_nano"

    assert formal_source_semantic_sha256(source) != first


def test_formal_source_binding_ignores_period_query_metadata_but_not_edge_semantics() -> None:
    source = _source_audit()
    source["edge_equality"]["period_raw_replay"] = {
        period: {
            "source_window": [1, 2],
            "raw_edge_query": "SELECT edge",
            "raw_edge_result": {"query_id": f"{period}-one", "elapsed_seconds": 1},
            "normalized_stored_raw_edges": [{"request_count": 1}],
            "normalized_source_raw_edges": [{"request_count": 1}],
            "stored_raw_edge_set_sha256": "same",
            "source_raw_edge_set_sha256": "same",
            "exact_source_stored_edge_set_equality": True,
        }
        for period in ("normal", "abnormal")
    }
    first = formal_source_semantic_sha256(source)
    source["edge_equality"]["period_raw_replay"]["normal"]["raw_edge_result"] = {
        "query_id": "normal-two",
        "elapsed_seconds": 99,
    }

    assert formal_source_semantic_sha256(source) == first
    source["edge_equality"]["period_raw_replay"]["normal"]["normalized_stored_raw_edges"][0][
        "request_count"
    ] = 2
    assert formal_source_semantic_sha256(source) != first


def test_formal_source_binding_sanitizes_split_graph_replay_metadata() -> None:
    source = _source_audit()
    source["edge_equality"]["period_graph_replay"] = {
        period: {
            "graph_observed_window": [1, 2],
            "graph_edge_query": "SELECT edge",
            "graph_edge_result": {"query_id": f"{period}-one", "elapsed_seconds": 1},
            "normalized_graph_edges": [{"request_count": 1}],
            "normalized_raw_edges": [{"request_count": 1}],
            "graph_edge_set_sha256": "same",
            "raw_edge_set_sha256": "same",
            "exact_raw_graph_edge_set_equality": True,
        }
        for period in ("normal", "abnormal")
    }
    first = formal_source_semantic_sha256(source)
    source["edge_equality"]["period_graph_replay"]["normal"]["graph_edge_result"] = {
        "query_id": "normal-two",
        "elapsed_seconds": 99,
    }

    assert formal_source_semantic_sha256(source) == first
    source["edge_equality"]["period_graph_replay"]["normal"]["normalized_graph_edges"][0][
        "request_count"
    ] = 2
    assert formal_source_semantic_sha256(source) != first


def test_measurement_export_rescores_all_models_and_removes_private_payloads(monkeypatch) -> None:
    monkeypatch.setattr(formal_module, "require_current_protocol", lambda version: None)
    report, source, scorer, protocol, scorer_fixture, protocol_fixture = _preflight()
    _bind(report, source, scorer, protocol, scorer_fixture, protocol_fixture)

    def fake_agent(gateway, case_input, visibility, **kwargs):
        return _agent_run(visibility, kwargs["model"])

    execute_formal_runs(
        _Client(),  # type: ignore[arg-type]
        _case(),
        scorer_fixture,
        FORMAL_SCORER_FIXTURE,
        protocol_fixture,
        DEFAULT_PROTOCOL_FIXTURE,
        report,
        paid_api_confirmed=True,
        run_agent_fn=fake_agent,
    )
    frozen_pricing = json.loads(json.dumps(report["pricing_snapshot"]))
    monkeypatch.setitem(
        release_module.MODEL_PRICING,
        "deepseek-v4-pro",
        {"input_per_million": 999, "output_per_million": 999},
    )
    monkeypatch.setattr(release_module, "_validate_measurement_bindings", lambda *args: None)
    artifact = build_measurement_artifact(
        report,
        source,
        scorer,
        protocol,
        scorer_fixture,
        FORMAL_SCORER_FIXTURE,
        protocol_fixture,
        DEFAULT_PROTOCOL_FIXTURE,
    )

    assert artifact["analysis_role"] == "measurement"
    assert artifact["experiment"]["cross_model_pooling"] is False
    assert artifact["experiment"]["pricing_snapshot"] == frozen_pricing
    assert (
        artifact["experiment"]["model_reports"]["deepseek-v4-pro"]["usage"]["pricing"]
        == frozen_pricing["deepseek-v4-pro"]
    )
    assert set(artifact["experiment"]["model_reports"]) == {
        "deepseek-v4-pro",
        "claude-opus-5",
        "claude-fable-5",
        "gpt-5.6-sol",
        "glm-5.3",
        "qwen3.8-2.4t-a95b",
    }
    assert len(artifact["experiment"]["runs"]) == 24
    assert all(
        report["successful_runs"] == 4
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
