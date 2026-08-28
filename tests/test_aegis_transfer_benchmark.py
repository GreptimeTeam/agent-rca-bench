from contextlib import contextmanager
from pathlib import Path

import pytest

from semantic_rca_bench.aegis_transfer_benchmark import (
    TransferRunError,
    build_transfer_run_report,
    execute_transfer_runs,
)
from semantic_rca_bench.aegis_transfer_scorer import (
    audit_transfer_scorer,
    load_transfer_scorer_fixture,
    source_transfer_audit_sha256,
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

SCORER_PATH = Path("fixtures/reference/aegis-transfer-scorer.json")


def _query() -> str:
    return """WITH paired AS (
  SELECT CASE
           WHEN c.timestamp >= '2025-07-19 09:52:38'
            AND c.timestamp < '2025-07-19 09:56:38' THEN 'normal'
           WHEN c.timestamp >= '2025-07-19 09:56:38'
            AND c.timestamp < '2025-07-19 10:00:38' THEN 'abnormal'
         END AS period,
         c.\"span_attributes.http.request.method\" AS client_method,
         s.\"span_attributes.http.request.method\" AS server_method
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
        query_id="q01",
        columns=["period", "side", "method", "span_count"],
        rows=[
            ["normal", "server", "GET", 57],
            ["abnormal", "client", "GET", 758],
            ["abnormal", "server", "OPTIONS", 758],
        ],
        elapsed_seconds=0,
    )


def _case() -> AegisTransferCase:
    paths = (Path("normal.parquet"), Path("abnormal.parquet"))
    return AegisTransferCase(
        agent_case_id="aegis-transfer-001",
        source_case="ts0-ts-security-service-request-replace-method-j6gpxx",
        dataset="dataset",
        system="Train Ticket",
        root=Path("case"),
        input=CaseInput(
            case_token="aegis-transfer-001",
            time_start=1752918758,
            time_end=1752919238,
            alert_time=1752918998,
            database="case_01",
            fault_taxonomy=[],
        ),
        ground_truth=AegisTransferGroundTruth(
            services=("ts-order-other-service", "ts-security-service"),
            declared_edge=("ts-security-service", "ts-order-other-service"),
            fault_type="HTTPRequestReplaceMethod",
        ),
        normal_window=(1752918758, 1752918998),
        abnormal_window=(1752918998, 1752919238),
        gauge_paths=paths,
        sum_paths=paths,
        histogram_paths=paths,
        log_paths=paths,
        trace_paths=paths,
        selected_manifest={},
    )


def _source_audit() -> dict[str, object]:
    expected = {
        "normal_server_methods": {"GET": 57},
        "abnormal_client_methods": {"GET": 758},
        "abnormal_server_methods": {"OPTIONS": 758},
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
                "case_id": "aegis-transfer-001",
                "time_start": 1752918758,
                "time_end": 1752919238,
                "alert_time": 1752918998,
                "fault_taxonomy": [],
            },
            "normal_window": [1752918758, 1752918998],
            "abnormal_window": [1752918998, 1752919238],
            "declared_edge": ["ts-security-service", "ts-order-other-service"],
            "fault_type": "HTTPRequestReplaceMethod",
        },
        "greptimedb": {"head": "head"},
        "source_audit": {},
        "ingestion": {},
        "edge_equality": {
            "exact_edge_set_equality": True,
            "window_contract": {
                "source_window": [1752918758, 1752919238],
                "graph_observed_window": [1752918720, 1752919260],
            },
        },
        "mechanism_evidence": {
            "predicate": "source_declared_http_method_replacement",
            "query": _query(),
            "result": _result().model_dump(mode="json"),
            "normalized_result": expected,
            "expected_result": expected,
            "pass": True,
        },
        "no_model_gates": {"all_passed": True},
    }


def _agent_run(visibility: Visibility) -> AgentRun:
    result = _result()
    return AgentRun(
        run_id=f"run-{visibility.value}",
        visibility=visibility,
        model="deepseek-v4-flash",
        runner=AgentRunner.API,
        diagnosis=Diagnosis(
            affected_component="ts-security-service",
            causal_dependency="ts-order-other-service",
            fault_category=FaultCategory.OTHER,
            fault_type="HTTP method replacement",
            confidence=1,
            evidence=[Evidence(query_id="q01", claim="paired methods changed")],
            explanation="The paired spans show the method replacement.",
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
    database = "case_01"

    @contextmanager
    def measure_query_load(self):
        yield DatabaseLoad()


def test_canonical_transfer_wiring_balances_positions_and_uses_graph_envelope() -> None:
    case = _case()
    source = _source_audit()
    fixture = load_transfer_scorer_fixture()
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
        "expected_runs": 9,
        "completed_runs": 9,
        "runner_errors": 0,
        "budget_exhaustions": 0,
        "complete": True,
    }
    assert all(item["evaluation"]["success"] for item in report["runs"])
    assert {call[0] for call in calls} == set(Visibility)
    assert all(
        window == (1752918720, 1752919260)
        for level, window, _, _ in calls
        if level is Visibility.SEMANTIC_GRAPH
    )
    assert all(
        window is None for level, window, _, _ in calls if level is not Visibility.SEMANTIC_GRAPH
    )
    for position in range(3):
        assert {
            report["runs"][repetition * 3 + position]["run"]["visibility"]
            for repetition in range(3)
        } == {level.value for level in Visibility}
    assert all(call[2].fault_taxonomy == [] for call in calls)
    assert all(call[3]["max_tool_calls"] == 48 for call in calls)
    assert all(call[3]["max_turns"] == 58 for call in calls)
    assert all(call[3]["max_tokens"] == 4096 for call in calls)


def test_canonical_transfer_run_stops_after_first_runner_error() -> None:
    case = _case()
    source = _source_audit()
    fixture = load_transfer_scorer_fixture()
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
    fixture = load_transfer_scorer_fixture()
    scorer = audit_transfer_scorer(source, fixture, SCORER_PATH)
    coverage = {"graph": {"status": "relational"}}
    mutation(source, scorer, coverage)

    with pytest.raises(ValueError, match=message):
        build_transfer_run_report(case, fixture, SCORER_PATH, source, scorer, coverage)
