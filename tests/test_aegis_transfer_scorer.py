from collections.abc import Iterable

import pytest

from semantic_rca_bench.aegis_transfer_scorer import (
    FORMAL_SCORER_FIXTURE,
    evaluate_aegis_transfer_run,
    load_transfer_scorer_fixture,
)
from semantic_rca_bench.contracts import (
    AgentRun,
    AgentRunner,
    AgentUsage,
    ApiTransport,
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

NORMAL_START = "2025-07-19T13:59:52Z"
BOUNDARY = "2025-07-19T14:03:52Z"
ABNORMAL_END = "2025-07-19T14:07:52Z"


def _aggregate_query(
    *,
    identity: str = "ts-auth-service",
    table: str = "k8s_container_restarts",
    time_predicate: str | None = None,
    extra_predicate: str = "",
    max_expression: str = "MAX(greptime_value)",
    suffix: str = "",
) -> str:
    time_predicate = time_predicate or (
        f"greptime_timestamp >= '{NORMAL_START}' AND greptime_timestamp < '{ABNORMAL_END}'"
    )
    return f"""SELECT CASE
  WHEN greptime_timestamp < '{BOUNDARY}' THEN 'normal' ELSE 'abnormal' END AS period,
  COUNT(*) AS sample_count,
  MIN(greptime_value) AS min_restarts,
  {max_expression} AS max_restarts
FROM {table}
WHERE k8s_container_name = '{identity}'
  AND {time_predicate}{extra_predicate}
GROUP BY period{suffix}"""


def _aggregate_result(
    *,
    normal_max: float = 0.0,
    abnormal_max: float = 1.0,
    truncated: bool = False,
) -> QueryResult:
    return QueryResult(
        query_id="restart",
        columns=["period", "sample_count", "min_restarts", "max_restarts"],
        rows=[
            ["normal", 24, 0.0, normal_max],
            ["abnormal", 24, 1.0 if abnormal_max else 0.0, abnormal_max],
        ],
        elapsed_seconds=0,
        truncated=truncated,
    )


def _run(
    traces: Iterable[tuple[str, str, QueryResult]],
    *,
    mechanism_code: MechanismCode = MechanismCode.WORKLOAD_RESTART,
) -> AgentRun:
    traces = list(traces)
    evidence = [
        Evidence(
            query_id=query_id,
            claim="The source restart metric establishes the workload and its transition.",
            claim_types=[
                EvidenceClaimType.CAUSAL_LOCUS,
                EvidenceClaimType.FAULT_MECHANISM,
            ],
        )
        for query_id, _, _ in traces
    ]
    return AgentRun(
        run_id="v31-restart-synthetic",
        visibility=Visibility.RAW,
        model="deepseek-v4-pro",
        runner=AgentRunner.API,
        api_transport=ApiTransport.ANTHROPIC_COMPATIBLE_MESSAGES,
        reasoning_effort="high",
        max_output_tokens=16_384,
        diagnosis=Diagnosis(
            causal_scope=CausalScope.COMPONENT,
            causal_component="ts-auth-service",
            causal_operation=None,
            fault_category=FaultCategory.OTHER,
            mechanism_code=mechanism_code,
            fault_type="workload restart",
            confidence=0.9,
            evidence=evidence,
            explanation="The restart counter changes from zero to one after onset.",
        ),
        tool_calls=[
            ToolTrace(
                tool_name="execute_sql",
                input={"query": query},
                query_id=query_id,
                output=result.model_copy(update={"query_id": query_id}).model_dump(mode="json"),
                database_load=DatabaseLoad(query_count=1, rows_returned=len(result.rows)),
            )
            for query_id, query, result in traces
        ],
        tool_calls_requested=len(traces),
        usage=AgentUsage(),
        elapsed_seconds=0,
        responses=[],
    )


def _evaluate(run: AgentRun):
    return evaluate_aegis_transfer_run(run, load_transfer_scorer_fixture())


def test_v31_fixture_is_bound_to_fresh_component_restart_case() -> None:
    fixture = load_transfer_scorer_fixture(FORMAL_SCORER_FIXTURE)

    assert fixture.agent_case_id == "aegis-transfer-004"
    assert fixture.case_role == "measurement"
    assert fixture.ground_truth.causal_scope is CausalScope.COMPONENT
    assert fixture.ground_truth.causal_component == "ts-auth-service"
    assert fixture.ground_truth.mechanism_code is MechanismCode.WORKLOAD_RESTART


def test_v31_accepts_complete_aggregate_restart_transition() -> None:
    evaluation = _evaluate(_run([("restart", _aggregate_query(), _aggregate_result())]))

    assert evaluation.success is True
    assert evaluation.diagnosis_correct is True
    assert evaluation.required_evidence_covered is True
    assert evaluation.mechanism_evidence_query_ids == ["restart"]


def test_v31_accepts_case_normalization_and_between_when_boundaries_have_no_rows() -> None:
    query = _aggregate_query(
        identity="TS-AUTH-SERVICE",
        time_predicate=(f"greptime_timestamp BETWEEN '{NORMAL_START}' AND '{ABNORMAL_END}'"),
    ).replace("k8s_container_name =", "UPPER(k8s_container_name) =")

    assert _evaluate(_run([("restart", query, _aggregate_result())])).success is True


def test_v31_accepts_complete_raw_metric_rows_without_prescribed_aggregation() -> None:
    query = f"""SELECT greptime_timestamp, greptime_value
FROM k8s_container_restarts
WHERE k8s_container_name = 'ts-auth-service'
  AND greptime_timestamp >= '{NORMAL_START}'
  AND greptime_timestamp < '{ABNORMAL_END}'"""
    result = QueryResult(
        query_id="restart",
        columns=["greptime_timestamp", "greptime_value"],
        rows=[
            ["2025-07-19T14:00:01Z", 0.0],
            ["2025-07-19T14:01:01Z", 0.0],
            ["2025-07-19T14:04:01Z", 1.0],
            ["2025-07-19T14:05:01Z", 1.0],
        ],
        elapsed_seconds=0,
    )

    assert _evaluate(_run([("restart", query, result)])).success is True


def _single_period_query(start: str, end: str) -> str:
    return f"""SELECT COUNT(*) AS sample_count,
  MIN(greptime_value) AS min_restarts,
  MAX(greptime_value) AS max_restarts
FROM k8s_container_restarts
WHERE k8s_container_name = 'ts-auth-service'
  AND greptime_timestamp >= '{start}'
  AND greptime_timestamp < '{end}'"""


def test_v31_combines_separate_complete_period_queries() -> None:
    normal = QueryResult(
        query_id="normal",
        columns=["sample_count", "min_restarts", "max_restarts"],
        rows=[[24, 0.0, 0.0]],
        elapsed_seconds=0,
    )
    abnormal = QueryResult(
        query_id="abnormal",
        columns=["sample_count", "min_restarts", "max_restarts"],
        rows=[[24, 1.0, 1.0]],
        elapsed_seconds=0,
    )

    evaluation = _evaluate(
        _run(
            [
                ("normal", _single_period_query(NORMAL_START, BOUNDARY), normal),
                ("abnormal", _single_period_query(BOUNDARY, ABNORMAL_END), abnormal),
            ]
        )
    )

    assert evaluation.success is True
    assert evaluation.mechanism_evidence_query_ids == ["normal", "abnormal"]


@pytest.mark.parametrize(
    "query",
    [
        _aggregate_query(identity="wrong-service"),
        _aggregate_query(table="k8s_container_ready"),
        _aggregate_query(extra_predicate=" AND greptime_value > 0"),
        _aggregate_query(extra_predicate=" AND k8s_pod_name = 'one-pod'"),
        _aggregate_query(max_expression="1"),
        _aggregate_query(suffix=" LIMIT 1"),
        _aggregate_query(time_predicate=f"greptime_timestamp >= '{NORMAL_START}'"),
    ],
)
def test_v31_rejects_unbound_narrowed_or_hardcoded_restart_evidence(query: str) -> None:
    evaluation = _evaluate(_run([("restart", query, _aggregate_result())]))

    assert evaluation.mechanism_evidence_match is False
    assert evaluation.required_evidence_covered is False


@pytest.mark.parametrize(
    "result",
    [
        _aggregate_result(normal_max=1.0),
        _aggregate_result(abnormal_max=0.0),
        _aggregate_result(truncated=True),
    ],
)
def test_v31_rejects_dirty_missing_or_truncated_restart_transition(
    result: QueryResult,
) -> None:
    evaluation = _evaluate(_run([("restart", _aggregate_query(), result)]))

    assert evaluation.success is False
    assert evaluation.mechanism_evidence_match is False


def test_v31_graph_entity_is_navigation_not_restart_evidence() -> None:
    result = QueryResult(
        query_id="entity",
        columns=["entity_type", "entity_id"],
        rows=[["service", "ts-auth-service"]],
        elapsed_seconds=0,
    )
    run = _run([("entity", "SELECT 1", result)]).model_copy(
        update={
            "visibility": Visibility.SEMANTIC_GRAPH,
            "tool_calls": [
                ToolTrace(
                    tool_name="query_semantic_graph",
                    input={"view": "entities"},
                    query_id="entity",
                    output=result.model_dump(mode="json"),
                    database_load=DatabaseLoad(query_count=1, rows_returned=1),
                )
            ],
        }
    )

    evaluation = _evaluate(run)

    assert evaluation.diagnosis_correct is True
    assert evaluation.mechanism_evidence_match is False
    assert evaluation.efficiency_eligible is False


def test_v31_wrong_structured_mechanism_fails_with_valid_restart_evidence() -> None:
    evaluation = _evaluate(
        _run(
            [("restart", _aggregate_query(), _aggregate_result())],
            mechanism_code=MechanismCode.APPLICATION_ERROR,
        )
    )

    assert evaluation.mechanism_evidence_match is True
    assert evaluation.mechanism_code_match is False
    assert evaluation.diagnosis_correct is False
    assert evaluation.efficiency_eligible is False
