from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

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
    QueryResult,
    ToolTrace,
    Visibility,
)
from semantic_rca_bench.datasets.openrca2_transfer import (
    TransferCaseSpec,
    load_selection_fixture,
)
from semantic_rca_bench.transfer_scorer import evaluate_transfer_run

SELECTION = Path("fixtures/reference/openrca2-transfer-v32-selection.json")


def _case(index: int) -> TransferCaseSpec:
    return load_selection_fixture(SELECTION).selected_cases[index]


def _time(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")


def _diagnosis(case: TransferCaseSpec, query_id: str = "q1") -> Diagnosis:
    return Diagnosis(
        causal_scope=case.causal_scope,
        causal_component=case.causal_component,
        edge_source=case.edge_source,
        edge_destination=case.edge_destination,
        fault_category=case.fault_category,
        mechanism_code=case.mechanism_code,
        fault_type=case.source_fault_type,
        causal_operation=(
            case.mechanism_evidence.allowed_operations[0]
            if case.mechanism_evidence.allowed_operations
            else None
        ),
        confidence=1,
        evidence=[
            Evidence(
                query_id=query_id,
                claim="The cited result proves the incident locus and mechanism transition.",
                claim_types=[
                    EvidenceClaimType.CAUSAL_LOCUS,
                    EvidenceClaimType.FAULT_MECHANISM,
                ],
            )
        ],
        explanation="The baseline and anomalous windows separate the mechanism.",
    )


def _configuration_event(case: TransferCaseSpec, *, container: str | None = None) -> str:
    evidence = case.direct_log_mechanism_evidence[0]
    return json.dumps(
        {
            "object": {
                "reason": evidence.event_reason,
                "note": f"OCI runtime create failed: {evidence.message_fragment}",
                "regarding": {
                    "fieldPath": f"spec.containers{{{container or evidence.container_identity}}}"
                },
            }
        }
    )


def _run(case: TransferCaseSpec, query: str, result: QueryResult) -> AgentRun:
    return AgentRun(
        run_id="synthetic",
        visibility=Visibility.RAW,
        model="test-model",
        runner=AgentRunner.API,
        api_transport=ApiTransport.OPENAI_RESPONSES,
        reasoning_effort="high",
        max_output_tokens=16384,
        diagnosis=_diagnosis(case, result.query_id),
        tool_calls=[
            ToolTrace(
                tool_name="execute_sql",
                input={"query": query},
                query_id=result.query_id,
                output=result.model_dump(mode="json"),
                database_load=DatabaseLoad(query_count=1, rows_returned=len(result.rows)),
            )
        ],
        tool_calls_requested=1,
        usage=AgentUsage(),
        elapsed_seconds=0,
        responses=[],
    )


def _evaluate(run: AgentRun, case: TransferCaseSpec):
    return evaluate_transfer_run(
        run,
        case,
        expected_model="test-model",
        expected_transport=ApiTransport.OPENAI_RESPONSES,
        expected_reasoning_effort="high",
        expected_max_output_tokens=16384,
        max_tool_calls=48,
    )


def _metric_query(case: TransferCaseSpec, *, between: bool = False) -> str:
    evidence = case.mechanism_evidence
    start = _time(case.normal_window[0])
    end = _time(case.abnormal_window[1])
    time_filter = (
        f"greptime_timestamp BETWEEN '{start}' AND '{end}'"
        if between
        else f"greptime_timestamp >= '{start}' AND greptime_timestamp < '{end}'"
    )
    return f"""
        SELECT
          CASE WHEN greptime_timestamp < '{_time(case.abnormal_window[0])}'
               THEN 'normal' ELSE 'abnormal' END AS phase,
          COUNT(*) AS observations,
          MIN(greptime_value) AS low_value,
          MAX(greptime_value) AS high_value,
          SUM(CASE WHEN greptime_value >= {evidence.threshold}
                   THEN 1 ELSE 0 END) AS threshold_hits
        FROM {evidence.source_table}
        WHERE {evidence.identity_column} = '{evidence.identity_value}'
          AND {time_filter}
        GROUP BY phase
    """


def _metric_result(case: TransferCaseSpec) -> QueryResult:
    evidence = case.mechanism_evidence
    return QueryResult(
        query_id="q1",
        columns=["phase", "observations", "low_value", "high_value", "threshold_hits"],
        rows=[
            [
                "normal",
                evidence.normal["count"],
                evidence.normal["min"],
                evidence.normal["max"],
                evidence.normal["high_count"],
            ],
            [
                "abnormal",
                evidence.abnormal["count"],
                evidence.abnormal["min"],
                evidence.abnormal["max"],
                evidence.abnormal["high_count"],
            ],
        ],
        elapsed_seconds=0,
    )


@pytest.mark.parametrize("index", [0, 7, 9])
def test_metric_scorer_accepts_expression_equivalent_aggregate_aliases(index: int) -> None:
    case = _case(index)

    evaluation = _evaluate(_run(case, _metric_query(case), _metric_result(case)), case)

    assert evaluation.diagnosis_correct is True
    assert evaluation.mechanism_evidence_match is True
    assert evaluation.efficiency_eligible is True


@pytest.mark.parametrize(
    ("index", "value_expression", "query_threshold", "result_factor"),
    [
        (7, "greptime_value * 100", 50, 100),
        (9, "greptime_value / 1048576", 512, 1 / 1048576),
    ],
)
def test_metric_scorer_normalizes_equivalent_units(
    index: int,
    value_expression: str,
    query_threshold: float,
    result_factor: float,
) -> None:
    case = _case(index)
    evidence = case.mechanism_evidence
    query = f"""
        SELECT
          CASE WHEN greptime_timestamp < '{_time(case.abnormal_window[0])}'
               THEN 'normal' ELSE 'abnormal' END AS phase,
          COUNT(*) AS observations,
          MIN({value_expression}) AS low_value,
          MAX({value_expression}) AS high_value,
          SUM(CASE WHEN {value_expression} >= {query_threshold}
                   THEN 1 ELSE 0 END) AS threshold_hits
        FROM {evidence.source_table}
        WHERE {evidence.identity_column} = '{evidence.identity_value}'
          AND greptime_timestamp >= '{_time(case.normal_window[0])}'
          AND greptime_timestamp < '{_time(case.abnormal_window[1])}'
        GROUP BY phase
    """
    result = QueryResult(
        query_id="q1",
        columns=["phase", "observations", "low_value", "high_value", "threshold_hits"],
        rows=[
            [
                "normal",
                evidence.normal["count"],
                evidence.normal["min"] * result_factor,
                evidence.normal["max"] * result_factor,
                evidence.normal["high_count"],
            ],
            [
                "abnormal",
                evidence.abnormal["count"],
                evidence.abnormal["min"] * result_factor,
                evidence.abnormal["max"] * result_factor,
                evidence.abnormal["high_count"],
            ],
        ],
        elapsed_seconds=0,
    )

    assert _evaluate(_run(case, query, result), case).mechanism_evidence_match is True


def test_metric_scorer_normalizes_scaled_raw_rows() -> None:
    case = _case(7)
    evidence = case.mechanism_evidence
    query = f"""
        SELECT greptime_timestamp, greptime_value * 100 AS utilization_percent
        FROM {evidence.source_table}
        WHERE {evidence.identity_column} = '{evidence.identity_value}'
          AND greptime_timestamp >= '{_time(case.normal_window[0])}'
          AND greptime_timestamp < '{_time(case.abnormal_window[1])}'
    """
    result = QueryResult(
        query_id="q1",
        columns=["greptime_timestamp", "utilization_percent"],
        rows=[
            [_time(case.normal_window[0] + 1), 1.0],
            [_time(case.abnormal_window[0] + 1), 100.0],
            [_time(case.abnormal_window[0] + 2), 110.0],
        ],
        elapsed_seconds=0,
    )

    assert _evaluate(_run(case, query, result), case).mechanism_evidence_match is True


@pytest.mark.parametrize("non_finite", [float("nan"), float("inf"), float("-inf")])
def test_metric_scorer_rejects_non_finite_raw_values(non_finite: float) -> None:
    case = _case(7)
    evidence = case.mechanism_evidence
    query = f"""
        SELECT greptime_timestamp, greptime_value
        FROM {evidence.source_table}
        WHERE {evidence.identity_column} = '{evidence.identity_value}'
          AND greptime_timestamp >= '{_time(case.normal_window[0])}'
          AND greptime_timestamp < '{_time(case.abnormal_window[1])}'
    """
    result = QueryResult(
        query_id="q1",
        columns=["greptime_timestamp", "greptime_value"],
        rows=[
            [_time(case.normal_window[0] + 1), non_finite],
            [_time(case.abnormal_window[0] + 1), 1.0],
            [_time(case.abnormal_window[0] + 2), 1.0],
        ],
        elapsed_seconds=0,
    )

    assert not _evaluate(_run(case, query, result), case).mechanism_evidence_match


@pytest.mark.parametrize(
    "value_expression",
    ["greptime_value + 1", "greptime_value * -1", "ROUND(greptime_value, 0)"],
)
def test_metric_scorer_rejects_non_equivalent_or_lossy_transform(
    value_expression: str,
) -> None:
    case = _case(7)
    query = _metric_query(case).replace("greptime_value", value_expression)

    assert not _evaluate(_run(case, query, _metric_result(case)), case).mechanism_evidence_match


@pytest.mark.parametrize(
    ("operator", "query_threshold"),
    [(">=", 1_000_000_000), (">=", -1), (">", 1)],
)
def test_metric_scorer_rejects_threshold_count_that_cannot_prove_both_periods(
    operator: str,
    query_threshold: float,
) -> None:
    case = _case(7)
    evidence = case.mechanism_evidence
    query = f"""
        SELECT
          CASE WHEN greptime_timestamp < '{_time(case.abnormal_window[0])}'
               THEN 'normal' ELSE 'abnormal' END AS phase,
          COUNT(*) AS observations,
          SUM(CASE WHEN greptime_value {operator} {query_threshold}
                   THEN 1 ELSE 0 END) AS threshold_hits
        FROM {evidence.source_table}
        WHERE {evidence.identity_column} = '{evidence.identity_value}'
          AND greptime_timestamp >= '{_time(case.normal_window[0])}'
          AND greptime_timestamp < '{_time(case.abnormal_window[1])}'
        GROUP BY phase
    """
    result = QueryResult(
        query_id="q1",
        columns=["phase", "observations", "threshold_hits"],
        rows=[
            ["normal", evidence.normal["count"], 0],
            ["abnormal", evidence.abnormal["count"], 2],
        ],
        elapsed_seconds=0,
    )

    assert not _evaluate(_run(case, query, result), case).mechanism_evidence_match


def test_metric_scorer_accepts_between_when_source_boundary_has_no_rows() -> None:
    case = _case(7)

    evaluation = _evaluate(
        _run(case, _metric_query(case, between=True), _metric_result(case)), case
    )

    assert evaluation.mechanism_evidence_match is True


def test_metric_scorer_accepts_complete_select_star_rows() -> None:
    case = _case(7)
    evidence = case.mechanism_evidence
    query = f"""
        SELECT * FROM {evidence.source_table}
        WHERE {evidence.identity_column} = '{evidence.identity_value}'
          AND greptime_timestamp >= '{_time(case.normal_window[0])}'
          AND greptime_timestamp < '{_time(case.abnormal_window[1])}'
    """
    result = QueryResult(
        query_id="q1",
        columns=["greptime_timestamp", "greptime_value", evidence.identity_column],
        rows=[
            [_time(case.normal_window[0] + 1), 0.01, evidence.identity_value],
            [_time(case.abnormal_window[0] + 1), 1.0, evidence.identity_value],
            [_time(case.abnormal_window[0] + 2), 1.1, evidence.identity_value],
        ],
        elapsed_seconds=0,
    )

    assert _evaluate(_run(case, query, result), case).mechanism_evidence_match is True


def test_metric_scorer_accepts_transparent_cte_aggregation() -> None:
    case = _case(7)
    evidence = case.mechanism_evidence
    query = f"""
        WITH scoped AS (
          SELECT greptime_timestamp, greptime_value
          FROM {evidence.source_table}
          WHERE {evidence.identity_column} = '{evidence.identity_value}'
            AND greptime_timestamp >= '{_time(case.normal_window[0])}'
            AND greptime_timestamp < '{_time(case.abnormal_window[1])}'
        )
        SELECT CASE WHEN greptime_timestamp < '{_time(case.abnormal_window[0])}'
                    THEN 'normal' ELSE 'abnormal' END AS phase,
               COUNT(*) AS samples,
               MIN(greptime_value) AS minimum,
               MAX(greptime_value) AS maximum,
               SUM(CASE WHEN greptime_value >= {evidence.threshold}
                        THEN 1 ELSE 0 END) AS threshold_hits
        FROM scoped
        GROUP BY phase
    """
    result = QueryResult(
        query_id="q1",
        columns=["phase", "samples", "minimum", "maximum", "threshold_hits"],
        rows=[
            [
                "normal",
                evidence.normal["count"],
                evidence.normal["min"],
                evidence.normal["max"],
                evidence.normal["high_count"],
            ],
            [
                "abnormal",
                evidence.abnormal["count"],
                evidence.abnormal["min"],
                evidence.abnormal["max"],
                evidence.abnormal["high_count"],
            ],
        ],
        elapsed_seconds=0,
    )

    assert _evaluate(_run(case, query, result), case).mechanism_evidence_match


@pytest.mark.parametrize(
    "query_template",
    [
        "SELECT * FROM ({source}) scoped WHERE phase = 'abnormal' OR high_value < {threshold}",
        "WITH scoped AS ({source}) "
        "SELECT * FROM scoped WHERE phase = 'abnormal' OR high_value < {threshold}",
        "WITH scoped AS ({source}), filtered AS ("
        "SELECT * FROM scoped WHERE phase = 'abnormal' OR high_value < {threshold}) "
        "SELECT * FROM filtered",
    ],
)
def test_metric_scorer_rejects_consumer_filter_that_can_hide_baseline_violations(
    query_template: str,
) -> None:
    case = _case(7)
    evidence = case.mechanism_evidence
    query = query_template.format(
        source=_metric_query(case),
        threshold=evidence.threshold,
    )

    evaluation = _evaluate(_run(case, query, _metric_result(case)), case)

    assert evaluation.baseline_evidence_match is False
    assert evaluation.mechanism_evidence_match is False


def test_metric_scorer_combines_split_time_bucket_aggregates() -> None:
    case = _case(0)
    evidence = case.mechanism_evidence
    traces = []
    citations = []
    normal_start = case.normal_window[0]
    abnormal_start = case.abnormal_window[0]
    for period, window, rows in (
        (
            "normal",
            case.normal_window,
            [[_time(normal_start), 12, 0, 0], [_time(normal_start + 60), 12, 0, 0]],
        ),
        (
            "abnormal",
            case.abnormal_window,
            [
                [_time(abnormal_start), 12, 1, 1],
                [_time(abnormal_start + 60), 12, 1, 1],
            ],
        ),
    ):
        query_id = f"q-{period}"
        query = f"""
            SELECT date_bin('1 minute', greptime_timestamp) AS bucket,
                   COUNT(*) AS observations,
                   MIN(greptime_value) AS low_value,
                   MAX(greptime_value) AS high_value
            FROM {evidence.source_table}
            WHERE {evidence.identity_column} = '{evidence.identity_value}'
              AND greptime_timestamp >= '{_time(window[0])}'
              AND greptime_timestamp < '{_time(window[1])}'
            GROUP BY bucket
        """
        result = QueryResult(
            query_id=query_id,
            columns=["bucket", "observations", "low_value", "high_value"],
            rows=rows,
            elapsed_seconds=0,
        )
        traces.append(
            ToolTrace(
                tool_name="execute_sql",
                input={"query": query},
                query_id=query_id,
                output=result.model_dump(mode="json"),
                database_load=DatabaseLoad(query_count=1, rows_returned=len(rows)),
            )
        )
        citations.append(
            Evidence(
                query_id=query_id,
                claim=f"{period} window evidence",
                claim_types=[
                    EvidenceClaimType.CAUSAL_LOCUS,
                    EvidenceClaimType.FAULT_MECHANISM,
                ],
            )
        )
    run = _run(case, _metric_query(case), _metric_result(case))
    assert run.diagnosis is not None
    run = run.model_copy(
        update={
            "diagnosis": run.diagnosis.model_copy(update={"evidence": citations}),
            "tool_calls": traces,
            "tool_calls_requested": len(traces),
        }
    )

    assert _evaluate(run, case).mechanism_evidence_match is True


def test_metric_scorer_combines_complete_baseline_with_partial_anomaly_existence() -> None:
    case = _case(7)
    evidence = case.mechanism_evidence
    normal_query = f"""
        SELECT COUNT(*) AS samples, MAX(greptime_value) AS maximum
        FROM {evidence.source_table}
        WHERE {evidence.identity_column} = '{evidence.identity_value}'
          AND greptime_timestamp >= '{_time(case.normal_window[0])}'
          AND greptime_timestamp < '{_time(case.normal_window[1])}'
    """
    abnormal_query = f"""
        SELECT greptime_timestamp, greptime_value
        FROM {evidence.source_table}
        WHERE {evidence.identity_column} = '{evidence.identity_value}'
          AND greptime_value >= {evidence.threshold}
          AND greptime_timestamp >= '{_time(case.abnormal_window[0])}'
          AND greptime_timestamp < '{_time(case.abnormal_window[0] + 60)}'
        LIMIT 1000
    """
    traces = [
        ToolTrace(
            tool_name="execute_sql",
            input={"query": normal_query},
            query_id="q-normal",
            output=QueryResult(
                query_id="q-normal",
                columns=["samples", "maximum"],
                rows=[[evidence.normal["count"], evidence.normal["max"]]],
                elapsed_seconds=0,
            ).model_dump(mode="json"),
            database_load=DatabaseLoad(query_count=1, rows_returned=1),
        ),
        ToolTrace(
            tool_name="execute_sql",
            input={"query": abnormal_query},
            query_id="q-abnormal",
            output=QueryResult(
                query_id="q-abnormal",
                columns=["greptime_timestamp", "greptime_value"],
                rows=[
                    [_time(case.abnormal_window[0] + 1), evidence.threshold],
                    [_time(case.abnormal_window[0] + 2), evidence.threshold],
                ],
                elapsed_seconds=0,
            ).model_dump(mode="json"),
            database_load=DatabaseLoad(query_count=1, rows_returned=2),
        ),
    ]
    run = _run(case, _metric_query(case), _metric_result(case))
    assert run.diagnosis is not None
    run = run.model_copy(
        update={
            "diagnosis": run.diagnosis.model_copy(
                update={
                    "evidence": [
                        Evidence(
                            query_id=trace.query_id,
                            claim="Source-derived transition evidence.",
                            claim_types=[EvidenceClaimType.EXCLUSION],
                        )
                        for trace in traces
                    ]
                }
            ),
            "tool_calls": traces,
            "tool_calls_requested": 2,
        }
    )

    evaluation = _evaluate(run, case)

    assert evaluation.mechanism_evidence_match is True
    assert evaluation.efficiency_eligible is True


def test_metric_scorer_rejects_unphased_bucket_across_shared_boundary() -> None:
    case = _case(0)
    evidence = case.mechanism_evidence
    query = f"""
        SELECT date_bin('1 minute', greptime_timestamp) AS bucket,
               COUNT(*) AS samples,
               MAX(greptime_value) AS maximum
        FROM {evidence.source_table}
        WHERE {evidence.identity_column} = '{evidence.identity_value}'
          AND greptime_timestamp >= '{_time(case.normal_window[0])}'
          AND greptime_timestamp < '{_time(case.abnormal_window[1])}'
        GROUP BY bucket
    """
    result = QueryResult(
        query_id="q1",
        columns=["bucket", "samples", "maximum"],
        rows=[[_time(case.normal_window[0]), 1, 0], [_time(case.abnormal_window[0]), 2, 1]],
        elapsed_seconds=0,
    )

    assert not _evaluate(_run(case, query, result), case).mechanism_evidence_match


def test_metric_scorer_rejects_value_filtered_query() -> None:
    case = _case(7)
    query = _metric_query(case).replace(
        "GROUP BY phase",
        "AND greptime_value >= 0.5 GROUP BY phase",
    )

    evaluation = _evaluate(_run(case, query, _metric_result(case)), case)

    assert evaluation.mechanism_evidence_match is False


def test_metric_scorer_rejects_unfrozen_identity_filter() -> None:
    case = _case(7)
    evidence = case.mechanism_evidence
    query = _metric_query(case).replace(
        "GROUP BY phase",
        f"AND {evidence.identity_column} LIKE 'wrong-%' GROUP BY phase",
    )

    assert (
        _evaluate(_run(case, query, _metric_result(case)), case).mechanism_evidence_match is False
    )


def test_metric_scorer_accepts_limit_when_result_is_provably_complete() -> None:
    case = _case(7)
    query = _metric_query(case).rstrip() + " LIMIT 1000"

    evaluation = _evaluate(_run(case, query, _metric_result(case)), case)

    assert evaluation.mechanism_evidence_match is True


def test_metric_scorer_rejects_limit_at_the_returned_cardinality() -> None:
    case = _case(7)
    query = _metric_query(case).rstrip() + " LIMIT 2"

    evaluation = _evaluate(_run(case, query, _metric_result(case)), case)

    assert evaluation.baseline_evidence_match is False
    assert evaluation.anomaly_evidence_match is True
    assert evaluation.mechanism_evidence_match is False


def test_metric_scorer_accepts_frozen_exact_namespace() -> None:
    case = _case(7)
    predicate = case.mechanism_evidence.scope_preserving_predicates[0]
    query = _metric_query(case).replace(
        "GROUP BY phase",
        f"AND {predicate.column} = '{predicate.value}' GROUP BY phase",
    )

    assert _evaluate(_run(case, query, _metric_result(case)), case).mechanism_evidence_match


@pytest.mark.parametrize("operator,value", [("=", "wrong"), ("LIKE", "wrong%")])
def test_metric_scorer_rejects_non_equivalent_namespace(operator: str, value: str) -> None:
    case = _case(7)
    predicate = case.mechanism_evidence.scope_preserving_predicates[0]
    query = _metric_query(case).replace(
        "GROUP BY phase",
        f"AND {predicate.column} {operator} '{value}' GROUP BY phase",
    )

    assert not _evaluate(_run(case, query, _metric_result(case)), case).mechanism_evidence_match


def test_metric_scorer_accepts_source_proven_equivalent_pod_identity() -> None:
    case = _case(7)
    evidence = case.mechanism_evidence
    equivalent = evidence.identity_equivalent_predicates[0]
    query = _metric_query(case).replace(
        f"{evidence.identity_column} = '{evidence.identity_value}'",
        f"{equivalent.column} = '{equivalent.value}'",
    )

    assert _evaluate(_run(case, query, _metric_result(case)), case).mechanism_evidence_match


def test_metric_scorer_accepts_source_proven_alternative_metric_and_service_identity() -> None:
    case = _case(7)
    signal = case.mechanism_evidence.alternative_metric_signals[0]
    service = next(
        predicate
        for predicate in signal.identity_equivalent_predicates
        if predicate.column == "service_name"
    )
    query = f"""
        SELECT CASE WHEN greptime_timestamp < '{_time(case.abnormal_window[0])}'
                    THEN 'normal' ELSE 'abnormal' END AS phase,
               COUNT(*) AS observations,
               MIN(greptime_value) AS low_value,
               MAX(greptime_value) AS high_value
        FROM {signal.source_table}
        WHERE {service.column} = '{service.value}'
          AND greptime_timestamp >= '{_time(case.normal_window[0])}'
          AND greptime_timestamp < '{_time(case.abnormal_window[1])}'
        GROUP BY phase
    """
    result = QueryResult(
        query_id="q1",
        columns=["phase", "observations", "low_value", "high_value"],
        rows=[["normal", 3, 0.01, 0.01], ["abnormal", 3, 1.0, 1.1]],
        elapsed_seconds=0,
    )

    assert _evaluate(_run(case, query, result), case).mechanism_evidence_match


def test_metric_scorer_accepts_conditional_aggregate_on_alternative_metric() -> None:
    case = _case(7)
    signal = case.mechanism_evidence.alternative_metric_signals[0]
    query = f"""
        SELECT MIN(CASE WHEN greptime_timestamp < '{_time(case.abnormal_window[0])}'
                        THEN greptime_value END) AS normal_min,
               MAX(CASE WHEN greptime_timestamp < '{_time(case.abnormal_window[0])}'
                        THEN greptime_value END) AS normal_max,
               MIN(CASE WHEN greptime_timestamp >= '{_time(case.abnormal_window[0])}'
                        THEN greptime_value END) AS abnormal_min,
               MAX(CASE WHEN greptime_timestamp >= '{_time(case.abnormal_window[0])}'
                        THEN greptime_value END) AS abnormal_max
        FROM {signal.source_table}
        WHERE {signal.identity_column} = '{signal.identity_value}'
          AND greptime_timestamp >= '{_time(case.normal_window[0])}'
          AND greptime_timestamp < '{_time(case.abnormal_window[1])}'
    """
    result = QueryResult(
        query_id="q1",
        columns=["normal_min", "normal_max", "abnormal_min", "abnormal_max"],
        rows=[[0.01, 0.01, 1.0, 1.1]],
        elapsed_seconds=0,
    )

    assert _evaluate(_run(case, query, result), case).mechanism_evidence_match


def test_configuration_error_accepts_direct_source_event_without_fabricating_baseline() -> None:
    case = _case(0)
    result = QueryResult(
        query_id="q1",
        columns=["greptime_timestamp", "line"],
        rows=[
            [
                case.abnormal_window[0] * 1_000_000_000,
                _configuration_event(case),
            ]
        ],
        elapsed_seconds=0,
    )
    run = _run(case, "SELECT greptime_timestamp, line FROM logs", result)
    run.diagnosis = run.diagnosis.model_copy(
        update={"mechanism_code": case.direct_log_mechanism_evidence[0].mechanism_code}
    )

    evaluation = _evaluate(run, case)

    assert evaluation.diagnosis_correct
    assert evaluation.baseline_evidence_match is False
    assert evaluation.anomaly_evidence_match
    assert evaluation.mechanism_evidence_match
    assert evaluation.efficiency_eligible


@pytest.mark.parametrize(
    ("timestamp_period", "container"),
    [("normal", None), ("abnormal", "unrelated-container")],
)
def test_configuration_error_rejects_non_causal_event(
    timestamp_period: str,
    container: str | None,
) -> None:
    case = _case(0)
    epoch = case.normal_window[0] if timestamp_period == "normal" else case.abnormal_window[0]
    result = QueryResult(
        query_id="q1",
        columns=["greptime_timestamp", "line"],
        rows=[[epoch * 1_000_000_000, _configuration_event(case, container=container)]],
        elapsed_seconds=0,
    )
    run = _run(case, "SELECT greptime_timestamp, line FROM logs", result)
    run.diagnosis = run.diagnosis.model_copy(
        update={"mechanism_code": case.direct_log_mechanism_evidence[0].mechanism_code}
    )

    assert not _evaluate(run, case).mechanism_evidence_match


def test_configuration_error_is_not_accepted_without_source_frozen_evidence() -> None:
    case = _case(3)
    result = QueryResult(
        query_id="q1",
        columns=["greptime_timestamp", "line"],
        rows=[[case.abnormal_window[0] * 1_000_000_000, "executable file not found in $PATH"]],
        elapsed_seconds=0,
    )
    run = _run(case, "SELECT greptime_timestamp, line FROM logs", result)
    run.diagnosis = run.diagnosis.model_copy(update={"mechanism_code": "configuration_error"})

    assert not _evaluate(run, case).diagnosis_correct


@pytest.mark.parametrize("operator", ["=", "IN"])
def test_metric_scorer_accepts_parenthesized_exact_identity(operator: str) -> None:
    case = _case(7)
    evidence = case.mechanism_evidence
    predicate = (
        f"({evidence.identity_column} = '{evidence.identity_value}')"
        if operator == "="
        else f"({evidence.identity_column} IN ('{evidence.identity_value}'))"
    )
    query = _metric_query(case).replace(
        f"{evidence.identity_column} = '{evidence.identity_value}'",
        predicate,
    )

    assert _evaluate(_run(case, query, _metric_result(case)), case).mechanism_evidence_match


def test_metric_scorer_accepts_source_unique_identity_prefix() -> None:
    case = _case(1)
    evidence = case.mechanism_evidence
    pod = next(
        item for item in evidence.identity_equivalent_predicates if item.column == "k8s_pod_name"
    )
    prefix = pod.value.split("-", 1)[0]
    query = _metric_query(case).replace(
        f"{evidence.identity_column} = '{evidence.identity_value}'",
        f"{pod.column} LIKE '{prefix}-%'",
    )

    assert _evaluate(_run(case, query, _metric_result(case)), case).mechanism_evidence_match


def test_metric_scorer_rejects_identity_prefix_matching_multiple_source_identities() -> None:
    case = _case(1)
    evidence = case.mechanism_evidence
    pod = next(
        item for item in evidence.identity_equivalent_predicates if item.column == "k8s_pod_name"
    )
    query = _metric_query(case).replace(
        f"{evidence.identity_column} = '{evidence.identity_value}'",
        f"{pod.column} LIKE '%'",
    )

    assert not _evaluate(_run(case, query, _metric_result(case)), case).mechanism_evidence_match


def test_metric_scorer_accepts_broader_population_with_projected_identity() -> None:
    case = _case(7)
    evidence = case.mechanism_evidence
    equivalent = evidence.identity_equivalent_predicates[0]
    query = f"""
        SELECT greptime_timestamp, greptime_value, {evidence.identity_column}
        FROM {evidence.source_table}
        WHERE {equivalent.column} LIKE '%search%'
          AND greptime_timestamp >= '{_time(case.normal_window[0])}'
          AND greptime_timestamp < '{_time(case.abnormal_window[1])}'
        ORDER BY greptime_timestamp
    """
    result = QueryResult(
        query_id="q1",
        columns=["greptime_timestamp", "greptime_value", evidence.identity_column],
        rows=[
            [_time(case.normal_window[0] + 1), 0.01, evidence.identity_value],
            [_time(case.normal_window[0] + 2), 9.0, "other-container"],
            [_time(case.abnormal_window[0] + 1), 1.0, evidence.identity_value],
            [_time(case.abnormal_window[0] + 2), 1.1, evidence.identity_value],
        ],
        elapsed_seconds=0,
    )

    assert _evaluate(_run(case, query, result), case).mechanism_evidence_match


def test_metric_scorer_rejects_namespace_projection_as_result_identity() -> None:
    case = _case(7)
    evidence = case.mechanism_evidence
    namespace = evidence.scope_preserving_predicates[0]
    query = f"""
        SELECT greptime_timestamp, greptime_value, {namespace.column}
        FROM {evidence.source_table}
        WHERE {evidence.identity_column} IN ('{evidence.identity_value}', 'other-container')
          AND {namespace.column} = '{namespace.value}'
          AND greptime_timestamp >= '{_time(case.normal_window[0])}'
          AND greptime_timestamp < '{_time(case.abnormal_window[1])}'
    """
    result = QueryResult(
        query_id="q1",
        columns=["greptime_timestamp", "greptime_value", namespace.column],
        rows=[
            [_time(case.normal_window[0] + 1), 0.01, namespace.value],
            [_time(case.abnormal_window[0] + 1), 1.0, namespace.value],
            [_time(case.abnormal_window[0] + 2), 1.1, namespace.value],
        ],
        elapsed_seconds=0,
    )

    assert not _evaluate(_run(case, query, result), case).mechanism_evidence_match


def test_metric_scorer_rejects_broader_identity_without_result_identity() -> None:
    case = _case(7)
    evidence = case.mechanism_evidence
    query = _metric_query(case).replace(
        f"{evidence.identity_column} = '{evidence.identity_value}'",
        f"({evidence.identity_column} = '{evidence.identity_value}' "
        f"OR {evidence.identity_column} = 'other-container')",
    )

    assert not _evaluate(_run(case, query, _metric_result(case)), case).mechanism_evidence_match


def test_metric_scorer_accepts_complete_raw_rows_from_wider_time_scope() -> None:
    case = _case(7)
    evidence = case.mechanism_evidence
    equivalent = evidence.identity_equivalent_predicates[0]
    query = f"""
        SELECT greptime_timestamp, greptime_value
        FROM {evidence.source_table}
        WHERE {equivalent.column} = '{equivalent.value}'
          AND greptime_timestamp >= '{_time(case.normal_window[0] - 30)}'
          AND greptime_timestamp < '{_time(case.abnormal_window[1] + 30)}'
        ORDER BY greptime_timestamp
    """
    result = QueryResult(
        query_id="q1",
        columns=["greptime_timestamp", "greptime_value"],
        rows=[
            [_time(case.normal_window[0] + 1), 0.01],
            [_time(case.abnormal_window[0] + 1), 1.0],
            [_time(case.abnormal_window[0] + 2), 1.1],
        ],
        elapsed_seconds=0,
    )

    assert _evaluate(_run(case, query, result), case).mechanism_evidence_match


def test_metric_scorer_rejects_unrecognized_additional_time_filter() -> None:
    case = _case(7)
    query = _metric_query(case).replace(
        "GROUP BY phase",
        "AND date(greptime_timestamp) = '2026-05-01' GROUP BY phase",
    )

    assert not _evaluate(_run(case, query, _metric_result(case)), case).mechanism_evidence_match


def test_metric_scorer_accepts_having_when_returned_period_facts_are_complete() -> None:
    case = _case(7)
    query = _metric_query(case).rstrip() + " HAVING COUNT(*) > 0"

    assert _evaluate(_run(case, query, _metric_result(case)), case).mechanism_evidence_match


def test_metric_scorer_rejects_having_that_filters_the_claimed_value() -> None:
    case = _case(7)
    evidence = case.mechanism_evidence
    query = f"""
        SELECT CASE WHEN greptime_timestamp < '{_time(case.abnormal_window[0])}'
                    THEN 'normal' ELSE 'abnormal' END AS phase,
               date_bin('1 minute', greptime_timestamp) AS bucket,
               COUNT(*) AS samples,
               MAX(greptime_value) AS maximum
        FROM {evidence.source_table}
        WHERE {evidence.identity_column} = '{evidence.identity_value}'
          AND greptime_timestamp >= '{_time(case.normal_window[0])}'
          AND greptime_timestamp < '{_time(case.abnormal_window[1])}'
        GROUP BY phase, bucket
        HAVING MAX(greptime_value) < {evidence.threshold}
    """
    result = QueryResult(
        query_id="q1",
        columns=["phase", "bucket", "samples", "maximum"],
        rows=[
            ["normal", _time(case.normal_window[0]), 6, 0.01],
            ["abnormal", _time(case.abnormal_window[0]), 2, 1.1],
        ],
        elapsed_seconds=0,
    )

    evaluation = _evaluate(_run(case, query, result), case)

    assert evaluation.baseline_evidence_match is False
    assert evaluation.mechanism_evidence_match is False


def test_metric_scorer_rejects_having_that_can_hide_target_buckets() -> None:
    case = _case(7)
    evidence = case.mechanism_evidence
    query = f"""
        SELECT CASE WHEN greptime_timestamp < '{_time(case.abnormal_window[0])}'
                    THEN 'normal' ELSE 'abnormal' END AS phase,
               date_bin('1 minute', greptime_timestamp) AS bucket,
               COUNT(*) AS samples,
               MAX(greptime_value) AS maximum
        FROM {evidence.source_table}
        WHERE {evidence.identity_column} = '{evidence.identity_value}'
          AND greptime_timestamp >= '{_time(case.normal_window[0])}'
          AND greptime_timestamp < '{_time(case.abnormal_window[1])}'
        GROUP BY phase, bucket
        HAVING COUNT(*) > 2
    """
    result = QueryResult(
        query_id="q1",
        columns=["phase", "bucket", "samples", "maximum"],
        rows=[
            ["normal", _time(case.normal_window[0]), 3, 0.01],
            ["abnormal", _time(case.abnormal_window[0]), 3, 1.1],
        ],
        elapsed_seconds=0,
    )

    evaluation = _evaluate(_run(case, query, result), case)

    assert evaluation.baseline_evidence_match is False
    assert evaluation.mechanism_evidence_match is False


def test_metric_scorer_accepts_conditional_extremes_with_observed_onset() -> None:
    case = _case(7)
    evidence = case.mechanism_evidence
    equivalent = evidence.identity_equivalent_predicates[0]
    onset = _time(case.abnormal_window[0] + 1)
    query = f"""
        SELECT {equivalent.column},
               MIN(CASE WHEN greptime_timestamp < '{onset}'
                        THEN greptime_value END) AS baseline_min,
               MAX(CASE WHEN greptime_timestamp < '{onset}'
                        THEN greptime_value END) AS baseline_max,
               MIN(CASE WHEN greptime_timestamp >= '{onset}'
                        THEN greptime_value END) AS anomalous_min,
               MAX(CASE WHEN greptime_timestamp >= '{onset}'
                        THEN greptime_value END) AS anomalous_max
        FROM {evidence.source_table}
        WHERE ({equivalent.column} LIKE 'search-%'
               OR {equivalent.column} LIKE 'other-%')
          AND greptime_timestamp >= '{_time(case.normal_window[0])}'
          AND greptime_timestamp < '{_time(case.abnormal_window[1])}'
        GROUP BY {equivalent.column}
    """
    result = QueryResult(
        query_id="q1",
        columns=[
            equivalent.column,
            "baseline_min",
            "baseline_max",
            "anomalous_min",
            "anomalous_max",
        ],
        rows=[[equivalent.value, 0.01, 0.01, 1.0, 1.1]],
        elapsed_seconds=0,
    )

    assert _evaluate(_run(case, query, result), case).mechanism_evidence_match


def test_metric_scorer_uses_case_expression_semantics_instead_of_period_labels() -> None:
    case = _case(3)
    query = (
        _metric_query(case)
        .replace("'normal'", "'before_shutdown'")
        .replace("'abnormal'", "'post_shutdown'")
    )
    result = _metric_result(case).model_copy(
        update={
            "rows": [
                ["before_shutdown", 3, 0.0, 0.0, 0],
                ["post_shutdown", 3, 1.0, 1.0, 3],
            ]
        }
    )

    assert _evaluate(_run(case, query, result), case).mechanism_evidence_match


def test_metric_scorer_accepts_complete_baseline_case_with_anomalous_default() -> None:
    case = _case(7)
    query = _metric_query(case).replace(
        f"greptime_timestamp < '{_time(case.abnormal_window[0])}'",
        f"greptime_timestamp >= '{_time(case.normal_window[0])}' "
        f"AND greptime_timestamp < '{_time(case.normal_window[1])}'",
        1,
    )

    assert _evaluate(_run(case, query, _metric_result(case)), case).mechanism_evidence_match


def test_metric_scorer_accepts_unbounded_conditional_extremes() -> None:
    case = _case(7)
    evidence = case.mechanism_evidence
    onset = _time(case.abnormal_window[0] + 1)
    query = f"""
        SELECT MIN(CASE WHEN greptime_timestamp < '{onset}'
                        THEN greptime_value END) AS baseline_min,
               MAX(CASE WHEN greptime_timestamp < '{onset}'
                        THEN greptime_value END) AS baseline_max,
               MIN(CASE WHEN greptime_timestamp >= '{onset}'
                        THEN greptime_value END) AS anomalous_min,
               MAX(CASE WHEN greptime_timestamp >= '{onset}'
                        THEN greptime_value END) AS anomalous_max
        FROM {evidence.source_table}
        WHERE {evidence.identity_column} = '{evidence.identity_value}'
    """
    result = QueryResult(
        query_id="q1",
        columns=["baseline_min", "baseline_max", "anomalous_min", "anomalous_max"],
        rows=[[0.01, 0.01, 1.0, 1.1]],
        elapsed_seconds=0,
    )

    assert _evaluate(_run(case, query, result), case).mechanism_evidence_match


def test_metric_scorer_accepts_complete_wider_population_grouped_by_identity() -> None:
    case = _case(2)
    evidence = case.mechanism_evidence
    boundary = _time(case.abnormal_window[0])
    query = f"""
        SELECT {evidence.identity_column},
               MAX(CASE WHEN greptime_timestamp < '{boundary}'
                        THEN greptime_value END) AS baseline_max,
               MIN(CASE WHEN greptime_timestamp >= '{boundary}'
                        THEN greptime_value END) AS anomalous_min,
               MAX(CASE WHEN greptime_timestamp >= '{boundary}'
                        THEN greptime_value END) AS anomalous_max
        FROM {evidence.source_table}
        GROUP BY {evidence.identity_column}
    """
    result = QueryResult(
        query_id="q1",
        columns=[
            evidence.identity_column,
            "baseline_max",
            "anomalous_min",
            "anomalous_max",
        ],
        rows=[
            ["unrelated", 0.0, 0.0, 0.0],
            [evidence.identity_value, 0.0, 5.0, 6.0],
        ],
        elapsed_seconds=0,
    )

    assert _evaluate(_run(case, query, result), case).mechanism_evidence_match


def test_restart_counter_maximum_proves_multiple_restart_events() -> None:
    case = _case(2)
    evidence = case.mechanism_evidence
    boundary = _time(case.abnormal_window[0])
    query = f"""
        SELECT {evidence.identity_column},
               MAX(CASE WHEN greptime_timestamp < '{boundary}'
                        THEN greptime_value END) AS baseline_max,
               MAX(CASE WHEN greptime_timestamp >= '{boundary}'
                        THEN greptime_value END) AS anomalous_max
        FROM {evidence.source_table}
        GROUP BY {evidence.identity_column}
    """
    result = QueryResult(
        query_id="q1",
        columns=[evidence.identity_column, "baseline_max", "anomalous_max"],
        rows=[[evidence.identity_value, 0.0, 6.0]],
        elapsed_seconds=0,
    )

    assert _evaluate(_run(case, query, result), case).mechanism_evidence_match


def test_restart_scorer_does_not_fabricate_period_counts_from_counter_extremes() -> None:
    case = _case(0)
    evidence = case.mechanism_evidence
    query = f"""
        SELECT {evidence.identity_column},
               MIN(greptime_value) AS minimum,
               MAX(greptime_value) AS maximum,
               MIN(CASE WHEN greptime_value > 0
                        THEN greptime_timestamp ELSE NULL END) AS first_restart
        FROM {evidence.source_table}
        WHERE {evidence.identity_column} = '{evidence.identity_value}'
          AND greptime_timestamp >= '{_time(case.normal_window[0])}'
          AND greptime_timestamp < '{_time(case.abnormal_window[1])}'
        GROUP BY {evidence.identity_column}
        HAVING MAX(greptime_value) > MIN(greptime_value)
    """
    result = QueryResult(
        query_id="q1",
        columns=[evidence.identity_column, "minimum", "maximum", "first_restart"],
        rows=[
            [
                evidence.identity_value,
                0.0,
                6.0,
                _time(case.abnormal_window[0] + 1),
            ]
        ],
        elapsed_seconds=0,
    )

    assert not _evaluate(_run(case, query, result), case).mechanism_evidence_match


def test_metric_scorer_normalizes_conditional_extreme_units() -> None:
    case = _case(7)
    evidence = case.mechanism_evidence
    onset = _time(case.abnormal_window[0] + 1)
    value = "greptime_value * 100"
    query = f"""
        SELECT MIN(CASE WHEN greptime_timestamp < '{onset}'
                        THEN {value} END) AS baseline_min,
               MAX(CASE WHEN greptime_timestamp < '{onset}'
                        THEN {value} END) AS baseline_max,
               MIN(CASE WHEN greptime_timestamp >= '{onset}'
                        THEN {value} END) AS anomalous_min,
               MAX(CASE WHEN greptime_timestamp >= '{onset}'
                        THEN {value} END) AS anomalous_max
        FROM {evidence.source_table}
        WHERE {evidence.identity_column} = '{evidence.identity_value}'
          AND greptime_timestamp >= '{_time(case.normal_window[0])}'
          AND greptime_timestamp < '{_time(case.abnormal_window[1])}'
    """
    result = QueryResult(
        query_id="q1",
        columns=["baseline_min", "baseline_max", "anomalous_min", "anomalous_max"],
        rows=[[1.0, 1.0, 100.0, 110.0]],
        elapsed_seconds=0,
    )

    assert _evaluate(_run(case, query, result), case).mechanism_evidence_match


def test_metric_scorer_rejects_short_conditional_aggregate_row() -> None:
    case = _case(7)
    evidence = case.mechanism_evidence
    equivalent = evidence.identity_equivalent_predicates[0]
    onset = _time(case.abnormal_window[0] + 1)
    query = f"""
        SELECT {equivalent.column},
               MIN(CASE WHEN greptime_timestamp < '{onset}'
                        THEN greptime_value END) AS baseline_min,
               MAX(CASE WHEN greptime_timestamp < '{onset}'
                        THEN greptime_value END) AS baseline_max,
               MIN(CASE WHEN greptime_timestamp >= '{onset}'
                        THEN greptime_value END) AS anomalous_min,
               MAX(CASE WHEN greptime_timestamp >= '{onset}'
                        THEN greptime_value END) AS anomalous_max
        FROM {evidence.source_table}
        WHERE {equivalent.column} = '{equivalent.value}'
          AND greptime_timestamp >= '{_time(case.normal_window[0])}'
          AND greptime_timestamp < '{_time(case.abnormal_window[1])}'
        GROUP BY {equivalent.column}
    """
    result = QueryResult(
        query_id="q1",
        columns=[
            equivalent.column,
            "baseline_min",
            "baseline_max",
            "anomalous_min",
            "anomalous_max",
        ],
        rows=[[equivalent.value, 0.01]],
        elapsed_seconds=0,
    )

    assert not _evaluate(_run(case, query, result), case).mechanism_evidence_match


def test_metric_scorer_rejects_conditional_baseline_violation_in_any_group() -> None:
    case = _case(7)
    evidence = case.mechanism_evidence
    equivalent = evidence.identity_equivalent_predicates[0]
    onset = _time(case.abnormal_window[0] + 1)
    query = f"""
        SELECT {evidence.identity_column},
               MIN(CASE WHEN greptime_timestamp < '{onset}'
                        THEN greptime_value END) AS baseline_min,
               MAX(CASE WHEN greptime_timestamp < '{onset}'
                        THEN greptime_value END) AS baseline_max,
               MIN(CASE WHEN greptime_timestamp >= '{onset}'
                        THEN greptime_value END) AS anomalous_min,
               MAX(CASE WHEN greptime_timestamp >= '{onset}'
                        THEN greptime_value END) AS anomalous_max
        FROM {evidence.source_table}
        WHERE {equivalent.column} LIKE '%search%'
          AND greptime_timestamp >= '{_time(case.normal_window[0])}'
          AND greptime_timestamp < '{_time(case.abnormal_window[1])}'
        GROUP BY {evidence.identity_column}, k8s_node_name
    """
    result = QueryResult(
        query_id="q1",
        columns=[
            evidence.identity_column,
            "baseline_min",
            "baseline_max",
            "anomalous_min",
            "anomalous_max",
        ],
        rows=[
            [evidence.identity_value, 0.01, 0.01, 1.0, 1.1],
            [evidence.identity_value, 0.01, 0.8, 1.0, 1.1],
        ],
        elapsed_seconds=0,
    )

    assert not _evaluate(_run(case, query, result), case).mechanism_evidence_match


def test_metric_scorer_accepts_explicitly_discriminated_union_branch() -> None:
    case = _case(7)
    evidence = case.mechanism_evidence
    equivalent = evidence.identity_equivalent_predicates[0]
    query = f"""
        SELECT 'target' AS signal,
               CASE WHEN greptime_timestamp < '{_time(case.abnormal_window[0])}'
                    THEN 'baseline' ELSE 'anomalous' END AS period,
               COUNT(*) AS samples,
               MIN(greptime_value) AS minimum,
               MAX(greptime_value) AS maximum
        FROM {evidence.source_table}
        WHERE {equivalent.column} = '{equivalent.value}'
          AND greptime_timestamp >= '{_time(case.normal_window[0])}'
          AND greptime_timestamp < '{_time(case.abnormal_window[1])}'
        GROUP BY period
        UNION ALL
        SELECT 'other' AS signal, 'baseline' AS period,
               COUNT(*) AS samples, MIN(greptime_value), MAX(greptime_value)
        FROM unrelated_metric
    """
    result = QueryResult(
        query_id="q1",
        columns=["signal", "period", "samples", "minimum", "maximum"],
        rows=[
            ["target", "baseline", 3, 0.01, 0.01],
            ["target", "anomalous", 3, 1.0, 1.1],
            ["other", "baseline", 10, 99.0, 99.0],
        ],
        elapsed_seconds=0,
    )

    assert _evaluate(_run(case, query, result), case).mechanism_evidence_match


def test_metric_scorer_rejects_union_without_distinct_branch_discriminator() -> None:
    case = _case(7)
    query = _metric_query(case).replace(
        "SELECT",
        "SELECT 'target' AS signal,",
        1,
    )
    query += " UNION ALL SELECT 'target', 'normal', 2, 0, 0, 0 FROM unrelated_metric"
    result = QueryResult(
        query_id="q1",
        columns=[
            "signal",
            "phase",
            "observations",
            "low_value",
            "high_value",
            "threshold_hits",
        ],
        rows=[
            ["target", "normal", 3, 0.01, 0.01, 0],
            ["target", "abnormal", 3, 1.0, 1.1, 3],
        ],
        elapsed_seconds=0,
    )

    assert not _evaluate(_run(case, query, result), case).mechanism_evidence_match


def test_metric_scorer_rejects_undiscriminated_foreign_union_rows() -> None:
    case = _case(7)
    query = _metric_query(case)
    query += " UNION ALL SELECT 'abnormal', 2, 1, 1, 2 FROM unrelated_metric"
    result = QueryResult(
        query_id="q1",
        columns=["phase", "observations", "low_value", "high_value", "threshold_hits"],
        rows=[["normal", 1, 0, 0, 0], ["abnormal", 2, 1, 1, 2]],
        elapsed_seconds=0,
    )

    assert not _evaluate(_run(case, query, result), case).mechanism_evidence_match


def test_metric_scorer_accepts_discriminated_union_of_disjoint_source_periods() -> None:
    case = _case(7)
    evidence = case.mechanism_evidence
    query = f"""
        SELECT 'normal' AS branch, COUNT(*) AS samples,
               MIN(greptime_value) AS minimum, MAX(greptime_value) AS maximum
        FROM {evidence.source_table}
        WHERE {evidence.identity_column} = '{evidence.identity_value}'
          AND greptime_timestamp >= '{_time(case.normal_window[0])}'
          AND greptime_timestamp < '{_time(case.normal_window[1])}'
        UNION ALL
        SELECT 'abnormal', COUNT(*), MIN(greptime_value), MAX(greptime_value)
        FROM {evidence.source_table}
        WHERE {evidence.identity_column} = '{evidence.identity_value}'
          AND greptime_timestamp >= '{_time(case.abnormal_window[0])}'
          AND greptime_timestamp < '{_time(case.abnormal_window[1])}'
    """
    result = QueryResult(
        query_id="q1",
        columns=["branch", "samples", "minimum", "maximum"],
        rows=[["normal", 3, 0.01, 0.01], ["abnormal", 3, 1.0, 1.1]],
        elapsed_seconds=0,
    )

    assert _evaluate(_run(case, query, result), case).mechanism_evidence_match


def _delay_query(case: TransferCaseSpec, *, wrong_parent: bool = False) -> str:
    parent = "s.span_id = c.parent_span_id" if wrong_parent else "s.parent_span_id = c.span_id"
    operation = case.mechanism_evidence.allowed_operations[0]
    return f"""
        SELECT c.timestamp AS caller_time,
               s.timestamp AS callee_time
        FROM traces c
        JOIN traces s ON c.trace_id = s.trace_id AND {parent}
        WHERE c.span_kind = 'SPAN_KIND_CLIENT'
          AND s.span_kind = 'SPAN_KIND_SERVER'
          AND c.service_name = '{case.edge_source}'
          AND s.service_name = '{case.edge_destination}'
          AND c.span_name = '{operation}'
          AND c.timestamp >= '{_time(case.normal_window[0])}'
          AND c.timestamp < '{_time(case.abnormal_window[1])}'
    """


def _delay_result(case: TransferCaseSpec) -> QueryResult:
    return QueryResult(
        query_id="q1",
        columns=["caller_time", "callee_time"],
        rows=[
            [_time(case.normal_window[0] + 1), _time(case.normal_window[0] + 1)],
            [_time(case.abnormal_window[0] + 1), _time(case.abnormal_window[0] + 2)],
            [_time(case.abnormal_window[0] + 2), _time(case.abnormal_window[0] + 3)],
        ],
        elapsed_seconds=0,
    )


def test_delay_scorer_accepts_source_faithful_client_server_rows() -> None:
    case = _case(6)

    evaluation = _evaluate(_run(case, _delay_query(case), _delay_result(case)), case)

    assert evaluation.causal_scope_match is True
    assert evaluation.mechanism_evidence_match is True
    assert evaluation.efficiency_eligible is True


def test_delay_scorer_rejects_consumer_filter_that_can_hide_baseline_gaps() -> None:
    case = _case(6)
    evidence = case.mechanism_evidence
    gap = "date_part('epoch', s.timestamp - c.timestamp) * 1000000000"
    inner = _delay_query(case).replace(
        "c.timestamp AS caller_time,\n               s.timestamp AS callee_time",
        "c.timestamp AS caller_time,\n"
        "               s.timestamp AS callee_time,\n"
        f"               {gap} AS gap_nanos,\n"
        "               CASE WHEN c.timestamp < '"
        f"{_time(case.abnormal_window[0])}' THEN 'normal' ELSE 'abnormal' END AS phase",
    )
    query = (
        f"SELECT caller_time, callee_time FROM ({inner}) paired "
        f"WHERE phase = 'abnormal' OR gap_nanos < {evidence.threshold}"
    )

    evaluation = _evaluate(_run(case, query, _delay_result(case)), case)

    assert evaluation.baseline_evidence_match is False
    assert evaluation.mechanism_evidence_match is False


def test_delay_scorer_rejects_union_with_foreign_source_rows() -> None:
    case = _case(6)
    query = _delay_query(case) + " UNION ALL SELECT observed_at, value FROM unrelated_metric"
    result = _delay_result(case)

    assert not _evaluate(_run(case, query, result), case).mechanism_evidence_match


def test_delay_scorer_accepts_nanosecond_aggregate_transition() -> None:
    case = _case(6)
    gap = "date_part('epoch', s.timestamp - c.timestamp) * 1000000000"
    query = (
        _delay_query(case).replace(
            "c.timestamp AS caller_time,\n               s.timestamp AS callee_time",
            f"MIN({gap}) AS min_gap,\n"
            f"               SUM(CASE WHEN {gap} >= 500000000 THEN 1 ELSE 0 END) "
            "AS delayed,\n               COUNT(*) AS observations,\n"
            "               CASE WHEN c.timestamp < '"
            f"{_time(case.abnormal_window[0])}' THEN 'normal' ELSE 'abnormal' END AS phase",
        )
        + " GROUP BY phase"
    )
    result = QueryResult(
        query_id="q1",
        columns=["min_gap", "delayed", "observations", "phase"],
        rows=[[1_000_000, 0, 1, "normal"], [1_000_000_000, 2, 2, "abnormal"]],
        elapsed_seconds=0,
    )

    assert _evaluate(_run(case, query, result), case).mechanism_evidence_match is True


def test_delay_scorer_rejects_client_duration_as_start_gap_evidence() -> None:
    case = _case(6)
    query = (
        _delay_query(case).replace(
            "c.timestamp AS caller_time,\n               s.timestamp AS callee_time",
            "CASE WHEN c.timestamp < '"
            f"{_time(case.abnormal_window[0])}' THEN 'baseline' "
            "ELSE 'anomalous' END AS period,\n"
            "               COUNT(*) AS paired_calls,\n"
            "               ROUND(AVG(c.duration_nano) / 1000000.0, 3) "
            "AS avg_client_ms,\n"
            "               ROUND(MAX(c.duration_nano) / 1000000.0, 3) "
            "AS max_client_ms,\n"
            "               ROUND(AVG(s.duration_nano) / 1000000.0, 3) "
            "AS avg_server_ms,\n"
            "               ROUND(MAX(s.duration_nano) / 1000000.0, 3) "
            "AS max_server_ms",
        )
        + " GROUP BY period"
    )
    result = QueryResult(
        query_id="q1",
        columns=[
            "period",
            "paired_calls",
            "avg_client_ms",
            "max_client_ms",
            "avg_server_ms",
            "max_server_ms",
        ],
        rows=[
            ["baseline", 20, 400.0, 400.0, 390.0, 400.0],
            ["anomalous", 20, 550.0, 600.0, 390.0, 400.0],
        ],
        elapsed_seconds=0,
    )

    assert not _evaluate(_run(case, query, result), case).mechanism_evidence_match


@pytest.mark.parametrize(
    ("gap_expression", "query_threshold", "normal_gap", "abnormal_gap"),
    [
        ("date_part('epoch', s.timestamp - c.timestamp) * 1000", 500, 1, 1000),
        (
            "(CAST(s.timestamp AS BIGINT) - CAST(c.timestamp AS BIGINT)) / 1000000000",
            0.5,
            0.001,
            1.0,
        ),
        ("EXTRACT(EPOCH FROM (s.timestamp - c.timestamp))", 0.5, 0.001, 1.0),
        ("CAST((s.timestamp - c.timestamp) AS BIGINT)", 500_000_000, 1_000_000, 1_000_000_000),
        ("s.timestamp - c.timestamp", 500_000_000, 1_000_000, 1_000_000_000),
        ("to_unixtime(s.timestamp) - to_unixtime(c.timestamp)", 0.5, 0.001, 1.0),
    ],
)
def test_delay_scorer_normalizes_equivalent_time_units(
    gap_expression: str,
    query_threshold: float,
    normal_gap: float,
    abnormal_gap: float,
) -> None:
    case = _case(6)
    query = (
        _delay_query(case)
        .replace(
            "c.timestamp AS caller_time,\n               s.timestamp AS callee_time",
            f"MIN({gap_expression}) AS min_gap,\n"
            f"               MAX({gap_expression}) AS max_gap,\n"
            f"               SUM(CASE WHEN {gap_expression} >= {query_threshold} "
            "THEN 1 ELSE 0 END) AS delayed,\n"
            "               COUNT(*) AS observations,\n"
            "               CASE WHEN c.timestamp < '"
            f"{_time(case.abnormal_window[0] + 1)}' THEN 'normal' ELSE 'abnormal' END AS phase",
        )
        .replace(
            f"AND c.timestamp >= '{_time(case.normal_window[0])}'",
            f"AND s.span_name = '{case.mechanism_evidence.allowed_operations[1]}'\n"
            f"          AND c.timestamp >= '{_time(case.normal_window[0])}'",
        )
        + " GROUP BY phase"
    )
    result = QueryResult(
        query_id="q1",
        columns=["min_gap", "max_gap", "delayed", "observations", "phase"],
        rows=[
            [normal_gap, normal_gap, 0, 1, "normal"],
            [abnormal_gap, abnormal_gap, 2, 2, "abnormal"],
        ],
        elapsed_seconds=0,
    )

    assert _evaluate(_run(case, query, result), case).mechanism_evidence_match is True


def test_delay_scorer_uses_average_and_maximum_to_prove_anomaly_count() -> None:
    case = _case(6)
    gap = "(CAST(s.timestamp AS BIGINT) - CAST(c.timestamp AS BIGINT)) / 1000000000"
    query = (
        _delay_query(case).replace(
            "c.timestamp AS caller_time,\n               s.timestamp AS callee_time",
            f"AVG({gap}) AS avg_gap,\n"
            f"               MAX({gap}) AS max_gap,\n"
            "               COUNT(*) AS observations,\n"
            "               CASE WHEN c.timestamp < '"
            f"{_time(case.abnormal_window[0] + 1)}' THEN 'normal' ELSE 'abnormal' END AS phase",
        )
        + " GROUP BY phase"
    )
    result = QueryResult(
        query_id="q1",
        columns=["avg_gap", "max_gap", "observations", "phase"],
        rows=[[0.001, 0.002, 6, "normal"], [1.62, 1.621, 5, "abnormal"]],
        elapsed_seconds=0,
    )

    assert _evaluate(_run(case, query, result), case).mechanism_evidence_match is True


def test_delay_scorer_rejects_average_that_can_come_from_one_anomaly() -> None:
    case = _case(6)
    gap = "(CAST(s.timestamp AS BIGINT) - CAST(c.timestamp AS BIGINT)) / 1000000000"
    query = (
        _delay_query(case).replace(
            "c.timestamp AS caller_time,\n               s.timestamp AS callee_time",
            f"AVG({gap}) AS avg_gap,\n"
            f"               MAX({gap}) AS max_gap,\n"
            "               COUNT(*) AS observations,\n"
            "               CASE WHEN c.timestamp < '"
            f"{_time(case.abnormal_window[0] + 1)}' THEN 'normal' ELSE 'abnormal' END AS phase",
        )
        + " GROUP BY phase"
    )
    result = QueryResult(
        query_id="q1",
        columns=["avg_gap", "max_gap", "observations", "phase"],
        rows=[[0.001, 0.002, 6, "normal"], [0.7, 2.0, 5, "abnormal"]],
        elapsed_seconds=0,
    )

    assert not _evaluate(_run(case, query, result), case).mechanism_evidence_match


def test_delay_scorer_does_not_hide_a_baseline_violation_behind_a_low_average() -> None:
    case = _case(6)
    gap = "(CAST(s.timestamp AS BIGINT) - CAST(c.timestamp AS BIGINT)) / 1000000000"
    query = (
        _delay_query(case).replace(
            "c.timestamp AS caller_time,\n               s.timestamp AS callee_time",
            f"AVG({gap}) AS avg_gap,\n"
            f"               MAX({gap}) AS max_gap,\n"
            "               COUNT(*) AS observations,\n"
            "               CASE WHEN c.timestamp < '"
            f"{_time(case.abnormal_window[0] + 1)}' THEN 'normal' ELSE 'abnormal' END AS phase",
        )
        + " GROUP BY phase"
    )
    result = QueryResult(
        query_id="q1",
        columns=["avg_gap", "max_gap", "observations", "phase"],
        rows=[[0.1, 0.6, 6, "normal"], [1.62, 1.621, 5, "abnormal"]],
        elapsed_seconds=0,
    )

    evaluation = _evaluate(_run(case, query, result), case)

    assert evaluation.mechanism_evidence_match is False
    assert evaluation.baseline_evidence_match is False
    assert evaluation.anomaly_evidence_match is True


def test_delay_scorer_rejects_wrong_parent_relation() -> None:
    case = _case(6)

    evaluation = _evaluate(
        _run(case, _delay_query(case, wrong_parent=True), _delay_result(case)), case
    )

    assert evaluation.mechanism_evidence_match is False
    assert evaluation.required_evidence_covered is False
    assert evaluation.efficiency_eligible is True


@pytest.mark.parametrize(
    "extra_filter",
    [
        "AND c.trace_id = 'selected-trace'",
        "AND c.span_name LIKE '%quote%'",
    ],
)
def test_delay_scorer_rejects_identity_or_operation_cherry_picking(
    extra_filter: str,
) -> None:
    case = _case(6)
    query = _delay_query(case).replace(
        f"AND c.timestamp >= '{_time(case.normal_window[0])}'",
        f"{extra_filter}\n          AND c.timestamp >= '{_time(case.normal_window[0])}'",
    )

    assert _evaluate(_run(case, query, _delay_result(case)), case).mechanism_evidence_match is False


def test_evidence_support_is_inferred_independently_of_claim_annotation() -> None:
    case = _case(6)
    run = _run(case, _delay_query(case), _delay_result(case))
    assert run.diagnosis is not None
    citation = run.diagnosis.evidence[0].model_copy(
        update={"claim_types": [EvidenceClaimType.EXCLUSION]}
    )
    run = run.model_copy(
        update={
            "diagnosis": run.diagnosis.model_copy(update={"evidence": [citation]}),
        }
    )

    evaluation = _evaluate(run, case)

    assert evaluation.mechanism_evidence_match is True
    assert evaluation.required_evidence_covered is True


def test_api_runner_is_part_of_the_execution_contract() -> None:
    case = _case(6)
    run = _run(case, _delay_query(case), _delay_result(case)).model_copy(
        update={"model": "unbound-model"}
    )

    assert _evaluate(run, case).execution_reliability is False


@pytest.mark.parametrize(
    "gap_expression",
    [
        "date_part('epoch', c.timestamp - s.timestamp) * 1000000000",
        "s.timestamp + c.timestamp",
    ],
)
def test_delay_scorer_rejects_wrong_gap_direction_or_unit(gap_expression: str) -> None:
    case = _case(6)
    query = (
        _delay_query(case).replace(
            "c.timestamp AS caller_time,\n               s.timestamp AS callee_time",
            f"MIN({gap_expression}) AS min_gap,\n"
            f"               SUM(CASE WHEN {gap_expression} >= 500000000 THEN 1 ELSE 0 END) "
            "AS delayed,\n               COUNT(*) AS observations,\n"
            "               CASE WHEN c.timestamp < '"
            f"{_time(case.abnormal_window[0])}' THEN 'normal' ELSE 'abnormal' END AS phase",
        )
        + " GROUP BY phase"
    )
    result = QueryResult(
        query_id="q1",
        columns=["min_gap", "delayed", "observations", "phase"],
        rows=[[0, 0, 1, "normal"], [1_000_000_000, 2, 2, "abnormal"]],
        elapsed_seconds=0,
    )

    assert _evaluate(_run(case, query, result), case).mechanism_evidence_match is False


def test_delay_scorer_rejects_threshold_literal_in_the_wrong_unit() -> None:
    case = _case(6)
    gap = "date_part('epoch', s.timestamp - c.timestamp) * 1000"
    query = (
        _delay_query(case).replace(
            "c.timestamp AS caller_time,\n               s.timestamp AS callee_time",
            f"SUM(CASE WHEN {gap} >= 500000000 THEN 1 ELSE 0 END) AS delayed,\n"
            "               COUNT(*) AS observations,\n"
            "               CASE WHEN c.timestamp < '"
            f"{_time(case.abnormal_window[0])}' THEN 'normal' ELSE 'abnormal' END AS phase",
        )
        + " GROUP BY phase"
    )
    result = QueryResult(
        query_id="q1",
        columns=["delayed", "observations", "phase"],
        rows=[[0, 1, "normal"], [2, 2, "abnormal"]],
        elapsed_seconds=0,
    )

    assert not _evaluate(_run(case, query, result), case).mechanism_evidence_match


def test_causal_operation_is_reported_but_not_a_diagnosis_guardrail() -> None:
    case = _case(6)
    run = _run(case, _delay_query(case), _delay_result(case))
    assert run.diagnosis is not None
    run = run.model_copy(
        update={"diagnosis": run.diagnosis.model_copy(update={"causal_operation": "POST /quote"})}
    )

    evaluation = _evaluate(run, case)

    assert evaluation.causal_operation_match is False
    assert evaluation.diagnosis_correct is True


def test_infrastructure_node_scope_is_scored_as_its_own_layer() -> None:
    # A node-scope case names its locus in causal_component like a component case,
    # so a scorer that only branches on COMPONENT would route it to the edge shape
    # and reject the correct answer.
    component_case = _case(0)
    case = component_case.model_copy(update={"causal_scope": CausalScope.INFRASTRUCTURE_NODE})
    query = _metric_query(component_case)
    result = _metric_result(component_case)

    correct = _evaluate(_run(case, query, result), case)
    assert correct.causal_scope_match is True
    assert correct.causal_locus_match is True

    run = _run(case, query, result)
    assert run.diagnosis is not None
    same_locus_wrong_layer = run.model_copy(
        update={
            "diagnosis": run.diagnosis.model_copy(update={"causal_scope": CausalScope.COMPONENT})
        }
    )
    evaluation = _evaluate(same_locus_wrong_layer, case)

    assert evaluation.causal_scope_match is False
    assert evaluation.causal_locus_match is True
    assert evaluation.diagnosis_correct is False
