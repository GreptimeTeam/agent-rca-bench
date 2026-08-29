from contextlib import contextmanager
from pathlib import Path

import pytest

import semantic_rca_bench.aegis_transfer_benchmark as benchmark_module
from semantic_rca_bench.aegis_transfer_benchmark import (
    TransferRunError,
    build_transfer_run_report,
    execute_transfer_runs,
)
from semantic_rca_bench.aegis_transfer_scorer import (
    CALIBRATION_SCORER_FIXTURE,
    audit_transfer_scorer,
    load_transfer_scorer_fixture,
    source_transfer_audit_sha256,
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
)

SCORER_PATH = CALIBRATION_SCORER_FIXTURE


def _query() -> str:
    return """WITH paired AS (
  SELECT CASE
           WHEN c.timestamp >= '2025-07-20 12:32:50'
            AND c.timestamp < '2025-07-20 12:36:50' THEN 'normal'
           WHEN c.timestamp >= '2025-07-20 12:36:50'
            AND c.timestamp < '2025-07-20 12:40:49' THEN 'abnormal'
         END AS period,
         CAST(s.timestamp AS BIGINT) - CAST(c.timestamp AS BIGINT) AS server_start_gap_ns
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
SELECT period, COUNT(*) AS span_count,
       MIN(server_start_gap_ns) AS min_start_gap_ns,
       MAX(server_start_gap_ns) AS max_start_gap_ns,
       SUM(CASE WHEN server_start_gap_ns >= 3070000000 THEN 1 ELSE 0 END)
         AS at_or_above_threshold
FROM paired
GROUP BY period"""


def _result() -> QueryResult:
    return QueryResult(
        query_id="q01",
        columns=[
            "period",
            "span_count",
            "min_start_gap_ns",
            "max_start_gap_ns",
            "at_or_above_threshold",
        ],
        rows=[
            ["normal", 37, -1_580_610, 18_958_433, 0],
            ["abnormal", 25, 3_070_537_663, 3_111_430_672, 25],
        ],
        elapsed_seconds=0,
    )


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


def _source_audit() -> dict[str, object]:
    expected = {
        "normal": {
            "count": 37,
            "min_start_gap_ns": -1_580_610,
            "max_start_gap_ns": 18_958_433,
            "at_or_above_threshold": 0,
        },
        "abnormal": {
            "count": 25,
            "min_start_gap_ns": 3_070_537_663,
            "max_start_gap_ns": 3_111_430_672,
            "at_or_above_threshold": 25,
        },
    }
    return {
        "audit_schema_version": 1,
        "mode": "aegis-transfer-no-model-audit",
        "dataset_revision": "dataset",
        "adapter_revision": "adapter",
        "pinned_source": {"verified": True},
        "selection_audit": {"frozen_selection_gate": {"pass": True}},
        "case": {
            "agent_facing": {
                "case_id": "aegis-transfer-002",
                "time_start": 1753014770,
                "time_end": 1753015249,
                "alert_time": 1753015010,
                "fault_taxonomy": [],
            },
            "normal_window": [1753014770, 1753015010],
            "abnormal_window": [1753015010, 1753015249],
            "declared_edge": ["ts-route-plan-service", "ts-travel2-service"],
            "fault_type": "HTTPRequestDelay",
        },
        "greptimedb": {"head": "head"},
        "source_audit": {},
        "ingestion": {},
        "edge_equality": {
            "exact_edge_set_equality": True,
            "window_contract": {
                "source_window": [1753014770, 1753015249],
                "graph_observed_window": [1753014720, 1753015260],
            },
        },
        "mechanism_evidence": {
            "predicate": "source_declared_http_client_server_start_gap",
            "observable": "server.timestamp - client.timestamp",
            "span_name": "POST /api/v1/travel2service/trips/left",
            "declared_delay_ns": 3_070_000_000,
            "query": _query(),
            "result": _result().model_dump(mode="json"),
            "normalized_result": expected,
            "expected_result": expected,
            "pass": True,
        },
        "no_model_gates": {
            "all_passed": True,
            "case_normalized_predicates_source_equivalent": True,
        },
    }


def _agent_run(visibility: Visibility) -> AgentRun:
    result = _result()
    return AgentRun(
        run_id=f"run-{visibility.value}",
        visibility=visibility,
        model="deepseek-v4-flash",
        runner=AgentRunner.API,
        api_transport=ApiTransport.ANTHROPIC_COMPATIBLE_MESSAGES,
        max_output_tokens=4096,
        diagnosis=Diagnosis(
            affected_component="ts-route-plan-service",
            causal_dependency="ts-travel2-service",
            causal_scope=CausalScope.DEPENDENCY_EDGE,
            causal_operation="POST /api/v1/travel2service/trips/left",
            fault_category=FaultCategory.DELAY,
            mechanism_code=MechanismCode.CALL_PATH_DELAY,
            fault_type="injected call-path delay",
            confidence=1,
            evidence=[
                Evidence(
                    query_id="q01",
                    claim="paired timestamps cross the injected-delay threshold",
                    claim_types=[
                        EvidenceClaimType.CAUSAL_SCOPE,
                        EvidenceClaimType.FAULT_MECHANISM,
                    ],
                )
            ],
            explanation="The paired spans show the injected call-path delay.",
        ),
        tool_calls=[
            ToolTrace(
                tool_name="execute_sql",
                input={"query": _query()},
                query_id="q01",
                output=result.model_dump(mode="json"),
                database_load=DatabaseLoad(query_count=1, rows_returned=3),
            )
        ],
        tool_calls_requested=1,
        usage=AgentUsage(),
        elapsed_seconds=0,
        responses=[],
    )


class _Client:
    database = "case_02"

    @contextmanager
    def measure_query_load(self):
        yield DatabaseLoad()


def test_canonical_transfer_wiring_balances_positions_and_uses_graph_envelope(monkeypatch) -> None:
    monkeypatch.setattr(benchmark_module, "require_current_protocol", lambda version: None)
    case = _case()
    source = _source_audit()
    fixture = load_transfer_scorer_fixture(SCORER_PATH)
    scorer = audit_transfer_scorer(source, fixture, SCORER_PATH)
    coverage = {"graph": {"status": "relational"}}
    report = build_transfer_run_report(case, fixture, SCORER_PATH, source, scorer, coverage)
    calls = []

    def fake_agent(gateway, case_input, visibility, **kwargs):
        calls.append((visibility, gateway.semantic_graph_window, case_input, kwargs))
        return _agent_run(visibility)

    execute_transfer_runs(
        _Client(),  # type: ignore[arg-type]
        case,
        fixture,
        report,
        run_agent_fn=fake_agent,
    )

    assert report["execution"] == {
        "expected_runs": 4,
        "completed_runs": 4,
        "runner_errors": 0,
        "budget_exhaustions": 0,
        "complete": True,
    }
    assert all(item["evaluation"]["success"] for item in report["runs"])
    assert {call[0] for call in calls} == set(Visibility)
    assert all(
        window == (1753014720, 1753015260)
        for level, window, _, _ in calls
        if level is Visibility.SEMANTIC_GRAPH
    )
    assert all(
        window is None for level, window, _, _ in calls if level is not Visibility.SEMANTIC_GRAPH
    )
    for position in range(2):
        assert {
            report["runs"][repetition * 2 + position]["run"]["visibility"]
            for repetition in range(2)
        } == {level.value for level in Visibility}
    assert all(call[2].fault_taxonomy == [] for call in calls)
    assert all(call[3]["max_tool_calls"] == 48 for call in calls)
    assert all(call[3]["max_turns"] == 58 for call in calls)
    assert all(call[3]["max_output_tokens"] == 4096 for call in calls)
    assert all(
        call[3]["api_transport"] is ApiTransport.ANTHROPIC_COMPATIBLE_MESSAGES for call in calls
    )


def test_scorer_audit_checks_model_against_revision_contract(tmp_path: Path) -> None:
    fixture = load_transfer_scorer_fixture(SCORER_PATH)
    mutated = fixture.model_copy(
        update={
            "canonical_api_runner": fixture.canonical_api_runner.model_copy(
                update={"model": "unexpected-model"}
            )
        }
    )
    fixture_path = tmp_path / "scorer.json"
    fixture_path.write_text(mutated.model_dump_json())

    audit = audit_transfer_scorer(_source_audit(), mutated, fixture_path)

    assert audit["no_model_gates"]["canonical_runner_contract_match"] is False
    assert audit["no_model_gates"]["all_passed"] is False


def test_scorer_audit_reports_unknown_revision_as_failed_gate(tmp_path: Path) -> None:
    fixture = load_transfer_scorer_fixture(SCORER_PATH)
    mutated = fixture.model_copy(update={"scorer_revision": "unknown-revision"})
    fixture_path = tmp_path / "scorer.json"
    fixture_path.write_text(mutated.model_dump_json())

    audit = audit_transfer_scorer(_source_audit(), mutated, fixture_path)

    assert audit["no_model_gates"]["canonical_runner_contract_match"] is False
    assert audit["no_model_gates"]["all_passed"] is False


def test_scorer_audit_requires_case_normalization_domain_proof() -> None:
    source = _source_audit()
    source["no_model_gates"].pop("case_normalized_predicates_source_equivalent")

    audit = audit_transfer_scorer(
        source,
        load_transfer_scorer_fixture(SCORER_PATH),
        SCORER_PATH,
    )

    assert audit["no_model_gates"]["source_transfer_audit_match"] is False
    assert audit["no_model_gates"]["all_passed"] is False


def test_canonical_transfer_run_stops_after_first_runner_error(monkeypatch) -> None:
    monkeypatch.setattr(benchmark_module, "require_current_protocol", lambda version: None)
    case = _case()
    source = _source_audit()
    fixture = load_transfer_scorer_fixture(SCORER_PATH)
    scorer = audit_transfer_scorer(source, fixture, SCORER_PATH)
    report = build_transfer_run_report(
        case,
        fixture,
        SCORER_PATH,
        source,
        scorer,
        {"graph": {"status": "relational"}},
    )

    def failed_agent(gateway, case_input, visibility, **kwargs):
        return _agent_run(visibility).model_copy(update={"error": "provider unavailable"})

    with pytest.raises(TransferRunError, match="provider unavailable"):
        execute_transfer_runs(
            _Client(),  # type: ignore[arg-type]
            case,
            fixture,
            report,
            run_agent_fn=failed_agent,
        )

    assert report["execution"]["completed_runs"] == 1
    assert report["execution"]["runner_errors"] == 1


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda source, scorer, coverage: source["no_model_gates"].update(all_passed=False),
            "source no-model gates",
        ),
        (
            lambda source, scorer, coverage: scorer["no_model_gates"].update(all_passed=False),
            "scorer no-model gates",
        ),
        (
            lambda source, scorer, coverage: coverage["graph"].update(status="entity-only"),
            "coverage is not relational",
        ),
        (
            lambda source, scorer, coverage: (
                source["edge_equality"].update(exact_edge_set_equality=False),
                scorer.update(source_transfer_audit_sha256=source_transfer_audit_sha256(source)),
            ),
            "exact raw/Graph equality",
        ),
    ],
)
def test_transfer_run_preflight_fails_before_agent_execution(mutation, message: str) -> None:
    case = _case()
    source = _source_audit()
    fixture = load_transfer_scorer_fixture(SCORER_PATH)
    scorer = audit_transfer_scorer(source, fixture, SCORER_PATH)
    coverage = {"graph": {"status": "relational"}}
    mutation(source, scorer, coverage)

    with pytest.raises(ValueError, match=message):
        build_transfer_run_report(case, fixture, SCORER_PATH, source, scorer, coverage)
