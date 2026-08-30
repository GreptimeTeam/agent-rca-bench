import pytest

from semantic_rca_bench.aegis_transfer_scorer import (
    CALIBRATION_SCORER_FIXTURE,
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


def _start_gap_query(parent_operator: str = "=") -> str:
    return f"""WITH paired AS (
  SELECT CASE
           WHEN c.timestamp >= '2025-07-20T12:32:50Z'
            AND c.timestamp < '2025-07-20T12:36:50Z' THEN 'normal'
           WHEN c.timestamp >= '2025-07-20T12:36:50Z'
            AND c.timestamp < '2025-07-20T12:40:49Z' THEN 'abnormal'
         END AS period,
         CAST(s.timestamp AS BIGINT) - CAST(c.timestamp AS BIGINT) AS server_start_gap_ns
  FROM traces c
  JOIN traces s
    ON c.trace_id = s.trace_id
   AND s.parent_span_id {parent_operator} c.span_id
  WHERE c.span_kind = 'SPAN_KIND_CLIENT'
    AND s.span_kind = 'SPAN_KIND_SERVER'
    AND c.service_name = 'ts-route-plan-service'
    AND s.service_name = 'ts-travel2-service'
    AND s.span_name = 'POST /api/v1/travel2service/trips/left'
    AND c.timestamp >= '2025-07-20T12:32:50Z'
    AND c.timestamp < '2025-07-20T12:40:49Z'
)
SELECT period, COUNT(*) AS span_count,
       MIN(server_start_gap_ns) AS min_start_gap_ns,
       MAX(server_start_gap_ns) AS max_start_gap_ns,
       SUM(CASE WHEN server_start_gap_ns >= 3070000000 THEN 1 ELSE 0 END)
         AS at_or_above_threshold
FROM paired
GROUP BY period"""


def _start_gap_result(*, abnormal_confirmations: int = 2) -> QueryResult:
    return QueryResult(
        query_id="q02",
        columns=[
            "period",
            "span_count",
            "min_start_gap_ns",
            "max_start_gap_ns",
            "at_or_above_threshold",
        ],
        rows=[
            ["normal", 5, -1_000_000, 20_000_000, 0],
            [
                "abnormal",
                2,
                3_070_500_000 if abnormal_confirmations == 2 else 1_000_000,
                3_111_000_000,
                abnormal_confirmations,
            ],
        ],
        elapsed_seconds=0,
    )


def _detached_duration_query() -> str:
    wrong_source = """), wrong AS (
  SELECT CASE
           WHEN c.timestamp >= '2025-07-20T12:32:50Z'
            AND c.timestamp < '2025-07-20T12:36:50Z' THEN 'normal'
           WHEN c.timestamp >= '2025-07-20T12:36:50Z'
            AND c.timestamp < '2025-07-20T12:40:49Z' THEN 'abnormal'
         END AS period,
         c.duration_nano AS server_start_gap_ns
  FROM traces c
  WHERE c.timestamp >= '2025-07-20T12:32:50Z'
    AND c.timestamp < '2025-07-20T12:40:49Z'
)"""
    return (
        _start_gap_query()
        .replace(")\nSELECT period", f"{wrong_source}\nSELECT period", 1)
        .replace("FROM paired\nGROUP BY period", "FROM wrong\nGROUP BY period")
    )


def _raw_timestamp_query() -> str:
    return """SELECT c.timestamp AS client_timestamp,
       s.timestamp AS server_timestamp
FROM traces c
JOIN traces s
  ON c.trace_id = s.trace_id
 AND s.parent_span_id = c.span_id
WHERE c.span_kind = 'SPAN_KIND_CLIENT'
  AND s.span_kind = 'SPAN_KIND_SERVER'
  AND c.service_name = 'ts-route-plan-service'
  AND s.service_name = 'ts-travel2-service'
  AND s.span_name = 'POST /api/v1/travel2service/trips/left'
  AND c.timestamp >= '2025-07-20T12:32:50Z'
  AND c.timestamp < '2025-07-20T12:40:49Z'"""


def _raw_timestamp_result() -> QueryResult:
    return QueryResult(
        query_id="q02",
        columns=["client_timestamp", "server_timestamp"],
        rows=[
            ["2025-07-20T12:33:00Z", "2025-07-20T12:33:00.010000Z"],
            ["2025-07-20T12:37:00Z", "2025-07-20T12:37:03.070000Z"],
            ["2025-07-20T12:38:00Z", "2025-07-20T12:38:03.090000Z"],
        ],
        elapsed_seconds=0,
    )


def _time_binned_query() -> str:
    return """SELECT date_bin('30 seconds', c.timestamp) AS bucket,
       COUNT(*) AS span_count,
       MAX(CAST(s.timestamp AS BIGINT) - CAST(c.timestamp AS BIGINT)) AS max_gap_ns
FROM traces c
JOIN traces s
  ON c.trace_id = s.trace_id
 AND s.parent_span_id = c.span_id
WHERE c.span_kind = 'SPAN_KIND_CLIENT'
  AND s.span_kind = 'SPAN_KIND_SERVER'
  AND c.service_name = 'ts-route-plan-service'
  AND s.service_name = 'ts-travel2-service'
  AND s.span_name = 'POST /api/v1/travel2service/trips/left'
  AND c.timestamp >= '2025-07-20T12:32:50Z'
  AND c.timestamp < '2025-07-20T12:40:49Z'
GROUP BY bucket"""


def _graph_result() -> QueryResult:
    return QueryResult(
        query_id="q01",
        columns=[
            "src_type",
            "src_id",
            "dst_type",
            "dst_id",
            "rel_type",
            "provenance",
            "request_count",
            "error_count",
        ],
        rows=[
            [
                "service",
                "ts-route-plan-service",
                "service",
                "ts-travel2-service",
                "calls",
                "trace",
                187,
                0,
            ]
        ],
        elapsed_seconds=0,
    )


def _run(
    *,
    query: str | None = None,
    mechanism_result: QueryResult | None = None,
    mechanism_code: MechanismCode = MechanismCode.CALL_PATH_DELAY,
    extra_invalid_citation: bool = False,
) -> AgentRun:
    evidence = [
        Evidence(
            query_id="q02",
            claim="The paired-span start gap crosses the threshold after onset.",
            claim_types=[
                EvidenceClaimType.CAUSAL_LOCUS,
                EvidenceClaimType.FAULT_MECHANISM,
            ],
        ),
    ]
    if extra_invalid_citation:
        evidence.append(
            Evidence(
                query_id="missing",
                claim="An unrelated extra claim has no executed result.",
                claim_types=[EvidenceClaimType.EXCLUSION],
            )
        )
    graph = _graph_result()
    mechanism = mechanism_result or _start_gap_result()
    return AgentRun(
        run_id="v30-synthetic",
        visibility=Visibility.SEMANTIC_GRAPH,
        model="deepseek-v4-flash",
        runner=AgentRunner.API,
        api_transport=ApiTransport.ANTHROPIC_COMPATIBLE_MESSAGES,
        max_output_tokens=4096,
        diagnosis=Diagnosis(
            causal_scope=CausalScope.DEPENDENCY_EDGE,
            edge_source="ts-route-plan-service",
            edge_destination="ts-travel2-service",
            causal_operation="POST /api/v1/travel2service/trips/left",
            fault_category=FaultCategory.DELAY,
            mechanism_code=mechanism_code,
            fault_type="A fixed caller-to-server start delay on the request path",
            confidence=0.9,
            evidence=evidence,
            explanation="The edge and paired-span transition establish the diagnosis.",
        ),
        tool_calls=[
            ToolTrace(
                tool_name="query_semantic_graph",
                input={"view": "relationships"},
                query_id="q01",
                output=graph.model_dump(mode="json"),
                database_load=DatabaseLoad(query_count=1, rows_returned=1),
            ),
            ToolTrace(
                tool_name="execute_sql",
                input={"query": query or _start_gap_query()},
                query_id="q02",
                output=mechanism.model_dump(mode="json"),
                database_load=DatabaseLoad(query_count=1, rows_returned=2),
            ),
        ],
        tool_calls_requested=2,
        usage=AgentUsage(),
        elapsed_seconds=0,
        responses=[],
    )


def test_v30_scores_structured_diagnosis_evidence_and_execution_separately() -> None:
    evaluation = evaluate_aegis_transfer_run(
        _run(), load_transfer_scorer_fixture(CALIBRATION_SCORER_FIXTURE)
    )

    assert evaluation.diagnosis_correct is True
    assert evaluation.causal_locus_evidence_match is True
    assert evaluation.mechanism_evidence_match is True
    assert evaluation.required_evidence_covered is True
    assert evaluation.citation_integrity is True
    assert evaluation.execution_reliability is True
    assert evaluation.efficiency_eligible is True
    assert evaluation.auditable_completion is True
    assert evaluation.success is True


def test_v30_graph_edge_alone_does_not_ground_causal_locus() -> None:
    run = _run()
    assert run.diagnosis is not None
    evidence = Evidence(
        query_id="q01",
        claim="The Graph contains the directed caller-to-callee edge.",
        claim_types=[EvidenceClaimType.CAUSAL_LOCUS],
    )
    run = run.model_copy(
        update={
            "diagnosis": run.diagnosis.model_copy(update={"evidence": [evidence]}),
            "tool_calls": run.tool_calls[:1],
            "tool_calls_requested": 1,
        }
    )

    evaluation = evaluate_aegis_transfer_run(
        run, load_transfer_scorer_fixture(CALIBRATION_SCORER_FIXTURE)
    )

    assert evaluation.diagnosis_correct is True
    assert evaluation.causal_locus_evidence_match is False
    assert evaluation.required_evidence_covered is False
    assert evaluation.efficiency_eligible is False


def test_v30_free_text_fault_type_does_not_drive_structured_correctness() -> None:
    run = _run()
    assert run.diagnosis is not None
    run = run.model_copy(
        update={
            "diagnosis": run.diagnosis.model_copy(
                update={"fault_type": "arbitrary prose that is not an exact dataset label"}
            )
        }
    )

    evaluation = evaluate_aegis_transfer_run(
        run, load_transfer_scorer_fixture(CALIBRATION_SCORER_FIXTURE)
    )

    assert evaluation.mechanism_code_match is True
    assert evaluation.diagnosis_correct is True


def test_v30_wrong_structured_mechanism_fails_even_with_correct_free_text() -> None:
    evaluation = evaluate_aegis_transfer_run(
        _run(mechanism_code=MechanismCode.CONNECTION_FAILURE),
        load_transfer_scorer_fixture(CALIBRATION_SCORER_FIXTURE),
    )

    assert evaluation.mechanism_code_match is False
    assert evaluation.diagnosis_correct is False
    assert evaluation.efficiency_eligible is False


def test_v30_invalid_extra_citation_is_reliability_not_diagnosis_failure() -> None:
    evaluation = evaluate_aegis_transfer_run(
        _run(extra_invalid_citation=True),
        load_transfer_scorer_fixture(CALIBRATION_SCORER_FIXTURE),
    )

    assert evaluation.diagnosis_correct is True
    assert evaluation.required_evidence_covered is True
    assert evaluation.citation_integrity is False
    assert evaluation.efficiency_eligible is True
    assert evaluation.auditable_completion is False
    assert evaluation.success is False


def test_v30_accepts_threshold_proof_without_canonical_source_counts() -> None:
    evaluation = evaluate_aegis_transfer_run(
        _run(mechanism_result=_start_gap_result()),
        load_transfer_scorer_fixture(CALIBRATION_SCORER_FIXTURE),
    )

    assert evaluation.mechanism_evidence_match is True


def test_v30_rejects_singleton_wrong_parent_and_server_duration_evidence() -> None:
    fixture = load_transfer_scorer_fixture(CALIBRATION_SCORER_FIXTURE)
    singleton = evaluate_aegis_transfer_run(
        _run(mechanism_result=_start_gap_result(abnormal_confirmations=1)), fixture
    )
    wrong_parent = evaluate_aegis_transfer_run(_run(query=_start_gap_query("<>")), fixture)
    duration_query = _start_gap_query().replace(
        "CAST(s.timestamp AS BIGINT) - CAST(c.timestamp AS BIGINT)", "s.duration_nano"
    )
    duration_only = evaluate_aegis_transfer_run(_run(query=duration_query), fixture)

    assert singleton.mechanism_evidence_match is False
    assert wrong_parent.mechanism_evidence_match is False
    assert duration_only.mechanism_evidence_match is False


def test_v30_does_not_double_count_repeated_singleton_evidence() -> None:
    run = _run(mechanism_result=_start_gap_result(abnormal_confirmations=1))
    assert run.diagnosis is not None
    repeated_result = _start_gap_result(abnormal_confirmations=1).model_copy(
        update={"query_id": "q03"}
    )
    repeated_trace = run.tool_calls[1].model_copy(
        update={
            "query_id": "q03",
            "output": repeated_result.model_dump(mode="json"),
        }
    )
    repeated_evidence = Evidence(
        query_id="q03",
        claim="A repeated view of the same singleton is not independent evidence.",
        claim_types=[EvidenceClaimType.FAULT_MECHANISM],
    )
    run = run.model_copy(
        update={
            "diagnosis": run.diagnosis.model_copy(
                update={"evidence": [*run.diagnosis.evidence, repeated_evidence]}
            ),
            "tool_calls": [*run.tool_calls, repeated_trace],
            "tool_calls_requested": 3,
        }
    )

    evaluation = evaluate_aegis_transfer_run(
        run,
        load_transfer_scorer_fixture(CALIBRATION_SCORER_FIXTURE),
    )

    assert evaluation.mechanism_evidence_match is False


def test_v30_accepts_raw_paired_timestamps_without_prescribed_aggregation() -> None:
    evaluation = evaluate_aegis_transfer_run(
        _run(query=_raw_timestamp_query(), mechanism_result=_raw_timestamp_result()),
        load_transfer_scorer_fixture(CALIBRATION_SCORER_FIXTURE),
    )

    assert evaluation.mechanism_evidence_match is True


@pytest.mark.parametrize(
    ("query", "result"),
    [
        (
            _start_gap_query(),
            QueryResult(
                query_id="q02",
                columns=["period", "span_count", "min_start_gap_ns", "max_start_gap_ns"],
                rows=[["normal"]],
                elapsed_seconds=0,
            ),
        ),
        (
            _raw_timestamp_query(),
            QueryResult(
                query_id="q02",
                columns=["client_timestamp", "server_timestamp"],
                rows=[["2025-07-20T12:33:00Z"]],
                elapsed_seconds=0,
            ),
        ),
        (
            _time_binned_query(),
            QueryResult(
                query_id="q02",
                columns=["bucket", "span_count", "max_gap_ns"],
                rows=[["2025-07-20T12:33:00Z"]],
                elapsed_seconds=0,
            ),
        ),
    ],
)
def test_v30_malformed_start_gap_rows_fail_closed(query: str, result: QueryResult) -> None:
    evaluation = evaluate_aegis_transfer_run(
        _run(query=query, mechanism_result=result),
        load_transfer_scorer_fixture(CALIBRATION_SCORER_FIXTURE),
    )

    assert evaluation.mechanism_evidence_match is False


def test_v30_invalid_optional_minimum_fails_closed() -> None:
    result = _start_gap_result().model_copy(
        update={
            "rows": [
                ["normal", 5, "not-a-number", 20_000_000, 0],
                ["abnormal", 2, 3_070_500_000, 3_111_000_000, 2],
            ]
        }
    )

    evaluation = evaluate_aegis_transfer_run(
        _run(mechanism_result=result),
        load_transfer_scorer_fixture(CALIBRATION_SCORER_FIXTURE),
    )

    assert evaluation.mechanism_evidence_match is False


def test_v30_rejects_result_aliases_not_derived_from_start_gap() -> None:
    query = (
        _start_gap_query()
        .replace(
            "CAST(s.timestamp AS BIGINT) - CAST(c.timestamp AS BIGINT) AS server_start_gap_ns",
            "CAST(s.timestamp AS BIGINT) - CAST(c.timestamp AS BIGINT) AS server_start_gap_ns, "
            "s.duration_nano AS duration_nano",
        )
        .replace(
            "MAX(server_start_gap_ns) AS max_start_gap_ns",
            "MAX(duration_nano) AS max_start_gap_ns",
        )
    )
    evaluation = evaluate_aegis_transfer_run(
        _run(query=query),
        load_transfer_scorer_fixture(CALIBRATION_SCORER_FIXTURE),
    )

    assert evaluation.mechanism_evidence_match is False


def test_v30_excludes_time_bins_that_cross_non_minute_window_boundaries() -> None:
    result = QueryResult(
        query_id="q02",
        columns=["bucket", "span_count", "max_gap_ns"],
        rows=[
            ["2025-07-20T12:33:00Z", 5, 20_000_000],
            ["2025-07-20T12:36:30Z", 8, 3_090_000_000],
            ["2025-07-20T12:37:00Z", 8, 3_090_000_000],
        ],
        elapsed_seconds=0,
    )
    evaluation = evaluate_aegis_transfer_run(
        _run(query=_time_binned_query(), mechanism_result=result),
        load_transfer_scorer_fixture(CALIBRATION_SCORER_FIXTURE),
    )

    assert evaluation.mechanism_evidence_match is False


def test_v30_rejects_narrowed_window_and_reversed_start_gap() -> None:
    fixture = load_transfer_scorer_fixture(CALIBRATION_SCORER_FIXTURE)
    narrowed = _start_gap_query().replace(
        "2025-07-20T12:40:49Z",
        "2025-07-20T12:40:00Z",
    )
    reversed_gap = _start_gap_query().replace(
        "CAST(s.timestamp AS BIGINT) - CAST(c.timestamp AS BIGINT)",
        "CAST(c.timestamp AS BIGINT) - CAST(s.timestamp AS BIGINT)",
    )

    assert (
        evaluate_aegis_transfer_run(_run(query=narrowed), fixture).mechanism_evidence_match is False
    )
    assert (
        evaluate_aegis_transfer_run(_run(query=reversed_gap), fixture).mechanism_evidence_match
        is False
    )


def test_v30_accepts_equivalent_case_normalized_pair_scope() -> None:
    query = (
        _start_gap_query()
        .replace(
            "c.span_kind = 'SPAN_KIND_CLIENT'",
            "UPPER(c.span_kind) = 'SPAN_KIND_CLIENT'",
        )
        .replace(
            "s.span_kind = 'SPAN_KIND_SERVER'",
            "LOWER(s.span_kind) = 'span_kind_server'",
        )
        .replace(
            "c.service_name = 'ts-route-plan-service'",
            "LOWER(c.service_name) = 'ts-route-plan-service'",
        )
        .replace(
            "s.service_name = 'ts-travel2-service'",
            "s.service_name IN ('ts-travel2-service')",
        )
    )
    for value in (
        "2025-07-20T12:32:50Z",
        "2025-07-20T12:36:50Z",
        "2025-07-20T12:40:49Z",
    ):
        query = query.replace(f"'{value}'", f"TIMESTAMP '{value}'")

    evaluation = evaluate_aegis_transfer_run(
        _run(query=query), load_transfer_scorer_fixture(CALIBRATION_SCORER_FIXTURE)
    )

    assert evaluation.mechanism_evidence_match is True


@pytest.mark.parametrize(
    "query",
    [
        _start_gap_query().replace(
            "c.span_kind = 'SPAN_KIND_CLIENT'",
            "(c.span_kind = 'SPAN_KIND_CLIENT' OR 1 = 1)",
        ),
        _start_gap_query().replace(
            "s.parent_span_id = c.span_id",
            "(s.parent_span_id = c.span_id OR 1 = 1)",
        ),
        _start_gap_query().replace(
            "    AND c.timestamp >= '2025-07-20T12:32:50Z'\n"
            "    AND c.timestamp < '2025-07-20T12:40:49Z'\n",
            "",
        ),
        _start_gap_query().replace(
            "c.service_name = 'ts-route-plan-service'",
            "c.service_name IN ('ts-route-plan-service', 'ts-route-service')",
        ),
        _start_gap_query().replace(
            "c.span_kind = 'SPAN_KIND_CLIENT'",
            "LOWER(c.span_kind) = 'SPAN_KIND_CLIENT'",
        ),
        _start_gap_query().replace(
            "CASE\n"
            "           WHEN c.timestamp >= '2025-07-20T12:32:50Z'\n"
            "            AND c.timestamp < '2025-07-20T12:36:50Z' THEN 'normal'\n"
            "           WHEN c.timestamp >= '2025-07-20T12:36:50Z'\n"
            "            AND c.timestamp < '2025-07-20T12:40:49Z' THEN 'abnormal'\n"
            "         END AS period",
            "'normal' AS period",
        ),
        _detached_duration_query(),
    ],
)
def test_v30_rejects_neutralized_or_incomplete_pair_scope(query: str) -> None:
    evaluation = evaluate_aegis_transfer_run(
        _run(query=query), load_transfer_scorer_fixture(CALIBRATION_SCORER_FIXTURE)
    )

    assert evaluation.mechanism_evidence_match is False


def _exception_query() -> str:
    return """WITH periods AS (
  SELECT 'normal' AS period UNION ALL SELECT 'abnormal'
), evidence AS (
  SELECT CASE
           WHEN timestamp >= '2025-07-18T12:21:57Z'
            AND timestamp < '2025-07-18T12:25:57Z' THEN 'normal'
           WHEN timestamp >= '2025-07-18T12:25:57Z'
            AND timestamp < '2025-07-18T12:29:56Z' THEN 'abnormal'
         END AS period,
         1 AS error_span_count,
         0 AS exception_log_count
  FROM traces
  WHERE service_name = 'ts-train-service'
    AND span_status_code = 'STATUS_CODE_ERROR'
    AND span_name LIKE '%retrieveByName%'
    AND timestamp >= '2025-07-18T12:21:57Z'
    AND timestamp < '2025-07-18T12:29:56Z'
  UNION ALL
  SELECT CASE
           WHEN greptime_timestamp >= '2025-07-18T12:21:57Z'
            AND greptime_timestamp < '2025-07-18T12:25:57Z' THEN 'normal'
           WHEN greptime_timestamp >= '2025-07-18T12:25:57Z'
            AND greptime_timestamp < '2025-07-18T12:29:56Z' THEN 'abnormal'
         END AS period,
         0 AS error_span_count,
         1 AS exception_log_count
  FROM logs
  WHERE service_name = 'ts-train-service'
    AND level IN ('ERROR', 'SEVERE', 'FATAL')
    AND LOWER(line) LIKE '%exception%'
    AND greptime_timestamp >= '2025-07-18T12:21:57Z'
    AND greptime_timestamp < '2025-07-18T12:29:56Z'
)
SELECT p.period,
       COALESCE(SUM(e.error_span_count), 0) AS error_span_count,
       COALESCE(SUM(e.exception_log_count), 0) AS exception_log_count
FROM periods p
LEFT JOIN evidence e ON e.period = p.period
GROUP BY p.period"""


def _exception_result(query_id: str = "q01") -> QueryResult:
    return QueryResult(
        query_id=query_id,
        columns=["period", "error_span_count", "exception_log_count"],
        rows=[["normal", 0, 0], ["abnormal", 12, 11]],
        elapsed_seconds=0,
    )


def _exception_run(
    *,
    query: str | None = None,
    result: QueryResult | None = None,
    operation: str | None = "TrainController.retrieveByName",
) -> AgentRun:
    evidence = Evidence(
        query_id="q01",
        claim=(
            "The operation has no baseline errors and repeated error spans and "
            "exception logs after onset."
        ),
        claim_types=[EvidenceClaimType.CAUSAL_LOCUS, EvidenceClaimType.FAULT_MECHANISM],
    )
    output = result or _exception_result()
    return AgentRun(
        run_id="v30-formal-synthetic",
        visibility=Visibility.RAW,
        model="deepseek-v4-pro",
        runner=AgentRunner.API,
        api_transport=ApiTransport.ANTHROPIC_COMPATIBLE_MESSAGES,
        max_output_tokens=4096,
        diagnosis=Diagnosis(
            causal_scope=CausalScope.COMPONENT,
            causal_component="ts-train-service",
            causal_operation=operation,
            fault_category=FaultCategory.OTHER,
            mechanism_code=MechanismCode.APPLICATION_ERROR,
            fault_type="JVM exception in retrieveByName",
            confidence=0.9,
            evidence=[evidence],
            explanation=(
                "The baseline-to-anomaly transition identifies the component and mechanism."
            ),
        ),
        tool_calls=[
            ToolTrace(
                tool_name="execute_sql",
                input={"query": query or _exception_query()},
                query_id="q01",
                output=output.model_dump(mode="json"),
                database_load=DatabaseLoad(query_count=1, rows_returned=2),
            )
        ],
        tool_calls_requested=1,
        usage=AgentUsage(),
        elapsed_seconds=0,
        responses=[],
    )


def test_formal_v30_accepts_component_scoped_exception_transition() -> None:
    evaluation = evaluate_aegis_transfer_run(
        _exception_run(), load_transfer_scorer_fixture(FORMAL_SCORER_FIXTURE)
    )

    assert evaluation.diagnosis_correct is True
    assert evaluation.mechanism_evidence_match is True
    assert evaluation.efficiency_eligible is True
    assert evaluation.success is True


def _observed_onset_trace_query() -> str:
    return """SELECT CASE
         WHEN timestamp < '2025-07-18T12:25:58.199Z' THEN 'baseline'
         ELSE 'anomalous'
       END AS period,
       COUNT(*) AS requests,
       SUM(CASE WHEN span_status_code = 'STATUS_CODE_ERROR' THEN 1 ELSE 0 END) AS errors
FROM traces
WHERE service_name = 'ts-train-service'
  AND span_name = 'GET /api/v1/trainservice/trains/byName/{name}'
  AND timestamp >= '2025-07-18T12:21:57Z'
  AND timestamp <= '2025-07-18T12:29:56Z'
GROUP BY period"""


def _grouped_exception_log_query() -> str:
    return """SELECT line,
       COUNT(*) AS occurrences,
       MIN(greptime_timestamp) AS first_seen,
       MAX(greptime_timestamp) AS last_seen
FROM logs
WHERE service_name = 'ts-train-service'
  AND level = 'SEVERE'
  AND greptime_timestamp >= '2025-07-18T12:21:57Z'
  AND greptime_timestamp <= '2025-07-18T12:29:56Z'
GROUP BY line
ORDER BY occurrences DESC"""


def _observed_transition_run() -> AgentRun:
    run = _exception_run()
    assert run.diagnosis is not None
    trace_result = QueryResult(
        query_id="q01",
        columns=["period", "requests", "errors"],
        rows=[["anomalous", 12, 12], ["baseline", 7, 0]],
        elapsed_seconds=0,
    )
    log_result = QueryResult(
        query_id="q02",
        columns=["line", "occurrences", "first_seen", "last_seen"],
        rows=[
            [
                "Request processing failed; root cause java.lang.NullPointerException",
                11,
                1_752_841_558_218_000_000,
                1_752_841_795_992_000_000,
            ]
        ],
        elapsed_seconds=0,
    )
    evidence = [
        Evidence(
            query_id="q01",
            claim="The operation changes from zero Error spans to repeated Error spans.",
            claim_types=[EvidenceClaimType.FAULT_MECHANISM],
        ),
        Evidence(
            query_id="q02",
            claim="The causal service emits repeated NullPointerException logs after onset.",
            claim_types=[EvidenceClaimType.CAUSAL_LOCUS, EvidenceClaimType.FAULT_MECHANISM],
        ),
    ]
    traces = [
        ToolTrace(
            tool_name="execute_sql",
            input={"query": _observed_onset_trace_query()},
            query_id="q01",
            output=trace_result.model_dump(mode="json"),
            database_load=DatabaseLoad(query_count=1, rows_returned=2),
        ),
        ToolTrace(
            tool_name="execute_sql",
            input={"query": _grouped_exception_log_query()},
            query_id="q02",
            output=log_result.model_dump(mode="json"),
            database_load=DatabaseLoad(query_count=1, rows_returned=1),
        ),
    ]
    return run.model_copy(
        update={
            "diagnosis": run.diagnosis.model_copy(update={"evidence": evidence}),
            "tool_calls": traces,
            "tool_calls_requested": 2,
        }
    )


def test_v30_accepts_observed_onset_and_result_proven_exception_signature() -> None:
    evaluation = evaluate_aegis_transfer_run(
        _observed_transition_run(), load_transfer_scorer_fixture(FORMAL_SCORER_FIXTURE)
    )

    assert evaluation.diagnosis_correct is True
    assert evaluation.causal_locus_evidence_match is True
    assert evaluation.mechanism_evidence_match is True
    assert evaluation.required_evidence_covered is True
    assert evaluation.supporting_evidence_query_ids == ["q02", "q01"]
    assert evaluation.tool_calls_through_required_evidence == 2
    assert evaluation.rows_returned_through_required_evidence == 3
    assert evaluation.efficiency_eligible is True


def _status_grouped_trace_query() -> str:
    return """SELECT CASE
         WHEN timestamp < '2025-07-18T12:25:58.199Z' THEN 'baseline'
         ELSE 'anomalous'
       END AS period,
       span_status_code,
       COUNT(*) AS requests,
       SUM(CASE WHEN \"span_attributes.http.response.status_code\" = 500 THEN 1 ELSE 0 END)
         AS http_500s
FROM traces
WHERE service_name = 'ts-train-service'
  AND span_name = 'GET /api/v1/trainservice/trains/byName/{name}'
  AND timestamp >= '2025-07-18T12:21:57Z'
  AND timestamp <= '2025-07-18T12:29:56Z'
GROUP BY period, span_status_code"""


def _period_filtered_exception_query() -> str:
    return """SELECT CASE
         WHEN greptime_timestamp < '2025-07-18T12:25:58.199Z' THEN 'baseline'
         ELSE 'anomalous'
       END AS period,
       COUNT(*) AS severe_npe_logs
FROM logs
WHERE service_name = 'ts-train-service'
  AND level = 'SEVERE'
  AND line LIKE '%NullPointerException%'
  AND greptime_timestamp >= '2025-07-18T12:21:57Z'
  AND greptime_timestamp <= '2025-07-18T12:29:56Z'
GROUP BY period"""


def _equivalent_grouped_transition_run() -> AgentRun:
    run = _exception_run()
    assert run.diagnosis is not None
    traces = [
        ToolTrace(
            tool_name="execute_sql",
            input={"query": _status_grouped_trace_query()},
            query_id="q01",
            output=QueryResult(
                query_id="q01",
                columns=["period", "span_status_code", "requests", "http_500s"],
                rows=[
                    ["anomalous", "STATUS_CODE_ERROR", 12, 12],
                    ["baseline", "STATUS_CODE_UNSET", 7, 0],
                ],
                elapsed_seconds=0,
            ).model_dump(mode="json"),
            database_load=DatabaseLoad(query_count=1, rows_returned=2),
        ),
        ToolTrace(
            tool_name="execute_sql",
            input={"query": _period_filtered_exception_query()},
            query_id="q02",
            output=QueryResult(
                query_id="q02",
                columns=["period", "severe_npe_logs"],
                rows=[["anomalous", 11]],
                elapsed_seconds=0,
            ).model_dump(mode="json"),
            database_load=DatabaseLoad(query_count=1, rows_returned=1),
        ),
    ]
    evidence = [
        Evidence(
            query_id="q01",
            claim="The operation changes from clean baseline spans to source Error spans.",
            claim_types=[EvidenceClaimType.CAUSAL_LOCUS, EvidenceClaimType.FAULT_MECHANISM],
        ),
        Evidence(
            query_id="q02",
            claim="The complete window has NPE logs only after onset.",
            claim_types=[EvidenceClaimType.FAULT_MECHANISM],
        ),
    ]
    return run.model_copy(
        update={
            "diagnosis": run.diagnosis.model_copy(update={"evidence": evidence}),
            "tool_calls": traces,
            "tool_calls_requested": 2,
        }
    )


def test_v30_accepts_status_grouping_and_missing_zero_period_row() -> None:
    evaluation = evaluate_aegis_transfer_run(
        _equivalent_grouped_transition_run(),
        load_transfer_scorer_fixture(FORMAL_SCORER_FIXTURE),
    )

    assert evaluation.mechanism_evidence_match is True
    assert evaluation.causal_locus_evidence_match is True
    assert evaluation.supporting_evidence_query_ids == ["q01", "q02"]
    assert evaluation.success is True


def test_v30_accepts_between_for_complete_trace_and_log_windows() -> None:
    run = _equivalent_grouped_transition_run()
    replacements = (
        (
            "timestamp >= '2025-07-18T12:21:57Z'\n  AND timestamp <= '2025-07-18T12:29:56Z'",
            "timestamp BETWEEN '2025-07-18T12:21:57Z' AND '2025-07-18T12:29:56Z'",
        ),
        (
            "greptime_timestamp >= '2025-07-18T12:21:57Z'\n"
            "  AND greptime_timestamp <= '2025-07-18T12:29:56Z'",
            "greptime_timestamp BETWEEN '2025-07-18T12:21:57Z' AND '2025-07-18T12:29:56Z'",
        ),
    )
    traces = []
    for trace, (old, new) in zip(run.tool_calls, replacements, strict=True):
        traces.append(
            trace.model_copy(
                update={"input": {"query": str(trace.input["query"]).replace(old, new)}}
            )
        )

    evaluation = evaluate_aegis_transfer_run(
        run.model_copy(update={"tool_calls": traces}),
        load_transfer_scorer_fixture(FORMAL_SCORER_FIXTURE),
    )

    assert evaluation.mechanism_evidence_match is True
    assert evaluation.causal_locus_evidence_match is True
    assert evaluation.success is True


def test_v30_zero_error_transition_does_not_ground_causal_locus() -> None:
    run = _equivalent_grouped_transition_run()
    assert run.diagnosis is not None
    trace_result = QueryResult.model_validate(run.tool_calls[0].output).model_copy(
        update={
            "rows": [
                ["anomalous", "STATUS_CODE_UNSET", 12, 12],
                ["baseline", "STATUS_CODE_UNSET", 7, 0],
            ]
        }
    )
    trace = run.tool_calls[0].model_copy(update={"output": trace_result.model_dump(mode="json")})
    evidence = Evidence(
        query_id="q01",
        claim="The operation remains source-status clean in both periods.",
        claim_types=[EvidenceClaimType.CAUSAL_LOCUS],
    )
    run = run.model_copy(
        update={
            "diagnosis": run.diagnosis.model_copy(update={"evidence": [evidence]}),
            "tool_calls": [trace],
            "tool_calls_requested": 1,
        }
    )

    evaluation = evaluate_aegis_transfer_run(
        run,
        load_transfer_scorer_fixture(FORMAL_SCORER_FIXTURE),
    )

    assert evaluation.causal_locus_evidence_match is False
    assert evaluation.required_evidence_covered is False


def test_v30_accepts_single_row_conditional_period_counts() -> None:
    run = _observed_transition_run()
    query = """SELECT
  SUM(CASE WHEN timestamp < '2025-07-18T12:25:57Z'
                 AND span_status_code = 'STATUS_CODE_ERROR' THEN 1 ELSE 0 END)
    AS baseline_errors,
  SUM(CASE WHEN timestamp >= '2025-07-18T12:25:57Z'
                 AND span_status_code = 'STATUS_CODE_ERROR' THEN 1 ELSE 0 END)
    AS anomalous_errors
FROM traces
WHERE service_name = 'ts-train-service'
  AND span_name = 'GET /api/v1/trainservice/trains/byName/{name}'
  AND timestamp >= '2025-07-18T12:21:57Z'
  AND timestamp <= '2025-07-18T12:29:56Z'"""
    result = QueryResult(
        query_id="q01",
        columns=["baseline_errors", "anomalous_errors"],
        rows=[[0, 12]],
        elapsed_seconds=0,
    )
    trace = run.tool_calls[0].model_copy(
        update={"input": {"query": query}, "output": result.model_dump(mode="json")}
    )

    evaluation = evaluate_aegis_transfer_run(
        run.model_copy(update={"tool_calls": [trace, run.tool_calls[1]]}),
        load_transfer_scorer_fixture(FORMAL_SCORER_FIXTURE),
    )

    assert evaluation.mechanism_evidence_match is True
    assert evaluation.causal_locus_evidence_match is True
    assert evaluation.success is True

    dead_status = query.replace(
        "span_status_code = 'STATUS_CODE_ERROR'",
        "EXISTS (SELECT 1 WHERE span_status_code = 'STATUS_CODE_ERROR')",
    )
    dead_trace = trace.model_copy(update={"input": {"query": dead_status}})
    rejected = evaluate_aegis_transfer_run(
        run.model_copy(update={"tool_calls": [dead_trace, run.tool_calls[1]]}),
        load_transfer_scorer_fixture(FORMAL_SCORER_FIXTURE),
    )

    assert rejected.mechanism_evidence_match is False


def _projected_multi_service_trace_query() -> str:
    return """SELECT CASE
         WHEN timestamp < '2025-07-18T12:25:57Z' THEN 'baseline'
         ELSE 'anomalous'
       END AS period,
       service_name,
       span_name,
       COUNT(*) AS spans,
       SUM(CASE WHEN span_status_code = 'STATUS_CODE_ERROR' THEN 1 ELSE 0 END) AS errors
FROM traces
WHERE timestamp >= '2025-07-18T12:21:57Z'
  AND timestamp <= '2025-07-18T12:29:56Z'
  AND ((service_name = 'ts-train-service'
        AND span_name IN ('GET /api/v1/trainservice/trains/byName/{name}',
                          'TrainController.retrieveByName'))
       OR (service_name = 'ts-basic-service'
           AND span_name = 'POST /api/v1/basicservice/basic/travel'))
GROUP BY period, service_name, span_name"""


def _projected_exception_lines_query() -> str:
    return """SELECT CASE
         WHEN greptime_timestamp < '2025-07-18T12:25:57Z' THEN 'baseline'
         ELSE 'anomalous'
       END AS period,
       line,
       COUNT(*) AS occurrences
FROM logs
WHERE greptime_timestamp >= '2025-07-18T12:21:57Z'
  AND greptime_timestamp <= '2025-07-18T12:29:56Z'
  AND service_name = 'ts-train-service'
  AND (line LIKE '[retrieveByName]%' OR level = 'SEVERE')
GROUP BY period, line"""


def _projected_multi_service_transition_run() -> AgentRun:
    run = _exception_run()
    assert run.diagnosis is not None
    traces = [
        ToolTrace(
            tool_name="execute_sql",
            input={"query": _projected_multi_service_trace_query()},
            query_id="q01",
            output=QueryResult(
                query_id="q01",
                columns=["period", "service_name", "span_name", "spans", "errors"],
                rows=[
                    [
                        "anomalous",
                        "ts-train-service",
                        "GET /api/v1/trainservice/trains/byName/{name}",
                        12,
                        12,
                    ],
                    [
                        "baseline",
                        "ts-train-service",
                        "GET /api/v1/trainservice/trains/byName/{name}",
                        7,
                        0,
                    ],
                    [
                        "anomalous",
                        "ts-basic-service",
                        "POST /api/v1/basicservice/basic/travel",
                        4,
                        4,
                    ],
                ],
                elapsed_seconds=0,
            ).model_dump(mode="json"),
            database_load=DatabaseLoad(query_count=1, rows_returned=3),
        ),
        ToolTrace(
            tool_name="execute_sql",
            input={"query": _projected_exception_lines_query()},
            query_id="q02",
            output=QueryResult(
                query_id="q02",
                columns=["period", "line", "occurrences"],
                rows=[
                    ["baseline", "[retrieveByName][Retrieve train]", 7],
                    [
                        "anomalous",
                        "Request failed; root cause java.lang.NullPointerException",
                        11,
                    ],
                ],
                elapsed_seconds=0,
            ).model_dump(mode="json"),
            database_load=DatabaseLoad(query_count=1, rows_returned=2),
        ),
    ]
    evidence = [
        Evidence(
            query_id="q01",
            claim="Projected target rows show the operation's Error transition.",
            claim_types=[EvidenceClaimType.CAUSAL_LOCUS, EvidenceClaimType.FAULT_MECHANISM],
        ),
        Evidence(
            query_id="q02",
            claim="Complete grouped logs show the NPE only after onset.",
            claim_types=[EvidenceClaimType.FAULT_MECHANISM],
        ),
    ]
    return run.model_copy(
        update={
            "diagnosis": run.diagnosis.model_copy(update={"evidence": evidence}),
            "tool_calls": traces,
            "tool_calls_requested": 2,
        }
    )


def test_v30_accepts_target_rows_from_complete_multi_service_results() -> None:
    evaluation = evaluate_aegis_transfer_run(
        _projected_multi_service_transition_run(),
        load_transfer_scorer_fixture(FORMAL_SCORER_FIXTURE),
    )

    assert evaluation.mechanism_evidence_match is True
    assert evaluation.causal_locus_evidence_match is True
    assert evaluation.success is True


def test_v30_rejects_projected_logs_that_can_filter_out_severe_exceptions() -> None:
    run = _projected_multi_service_transition_run()
    narrowed = run.tool_calls[1].model_copy(
        update={
            "input": {
                "query": _projected_exception_lines_query().replace(
                    "line LIKE '[retrieveByName]%' OR level = 'SEVERE'",
                    "line LIKE '[retrieveByName]%' AND level = 'SEVERE'",
                )
            }
        }
    )

    evaluation = evaluate_aegis_transfer_run(
        run.model_copy(update={"tool_calls": [run.tool_calls[0], narrowed]}),
        load_transfer_scorer_fixture(FORMAL_SCORER_FIXTURE),
    )

    assert evaluation.mechanism_evidence_match is False
    assert evaluation.success is False


def test_v30_observed_transition_rejects_unbound_counts_and_missing_signature() -> None:
    fixture = load_transfer_scorer_fixture(FORMAL_SCORER_FIXTURE)
    run = _observed_transition_run()
    hardcoded_trace = run.tool_calls[0].model_copy(
        update={
            "input": {
                "query": _observed_onset_trace_query().replace(
                    "SUM(CASE WHEN span_status_code = 'STATUS_CODE_ERROR' THEN 1 ELSE 0 END)",
                    "12",
                )
            }
        }
    )
    no_signature_output = QueryResult.model_validate(run.tool_calls[1].output).model_copy(
        update={
            "rows": [["Request failed", 11, 1_752_841_558_218_000_000, 1_752_841_795_992_000_000]]
        }
    )
    no_signature_log = run.tool_calls[1].model_copy(
        update={"output": no_signature_output.model_dump(mode="json")}
    )

    hardcoded = evaluate_aegis_transfer_run(
        run.model_copy(update={"tool_calls": [hardcoded_trace, run.tool_calls[1]]}), fixture
    )
    no_signature = evaluate_aegis_transfer_run(
        run.model_copy(update={"tool_calls": [run.tool_calls[0], no_signature_log]}), fixture
    )

    assert hardcoded.mechanism_evidence_match is False
    assert no_signature.mechanism_evidence_match is False


@pytest.mark.parametrize(
    "query",
    [
        _observed_onset_trace_query().replace(
            "12:25:58.199Z",
            "12:25:00Z",
        ),
        _observed_onset_trace_query().replace(
            "span_status_code = 'STATUS_CODE_ERROR'",
            "span_status_code = 'STATUS_CODE_ERROR' OR 1 = 1",
        ),
        _observed_onset_trace_query().replace(
            "span_status_code = 'STATUS_CODE_ERROR'",
            '"span_attributes.http.response.status_code" = 500',
        ),
        _observed_onset_trace_query().replace(
            "SUM(CASE WHEN span_status_code = 'STATUS_CODE_ERROR' THEN 1 ELSE 0 END)",
            "SUM(CASE WHEN span_status_code = 'STATUS_CODE_ERROR' THEN 1 ELSE 0 END) * 100",
        ),
        _observed_onset_trace_query().replace(
            "2025-07-18T12:29:56Z",
            "2025-07-18T13:29:56Z",
        ),
    ],
)
def test_v30_observed_transition_rejects_invalid_time_or_error_semantics(query: str) -> None:
    fixture = load_transfer_scorer_fixture(FORMAL_SCORER_FIXTURE)
    run = _observed_transition_run()
    trace = run.tool_calls[0].model_copy(update={"input": {"query": query}})

    evaluation = evaluate_aegis_transfer_run(
        run.model_copy(update={"tool_calls": [trace, run.tool_calls[1]]}), fixture
    )

    assert evaluation.mechanism_evidence_match is False


@pytest.mark.parametrize(
    "query",
    [
        f"{_grouped_exception_log_query()} LIMIT 1",
        _grouped_exception_log_query().replace(
            "GROUP BY line\nORDER BY",
            "GROUP BY line HAVING COUNT(*) > 100\nORDER BY",
        ),
        _grouped_exception_log_query().replace(
            "MIN(greptime_timestamp) AS first_seen",
            "MIN(1) + MAX(greptime_timestamp) AS first_seen",
        ),
        _grouped_exception_log_query().replace(
            "2025-07-18T12:29:56Z",
            "2025-07-18T13:29:56Z",
        ),
    ],
)
def test_v30_incomplete_grouped_logs_cannot_prove_baseline_absence(query: str) -> None:
    fixture = load_transfer_scorer_fixture(FORMAL_SCORER_FIXTURE)
    run = _observed_transition_run()
    trace = run.tool_calls[1].model_copy(update={"input": {"query": query}})

    evaluation = evaluate_aegis_transfer_run(
        run.model_copy(update={"tool_calls": [run.tool_calls[0], trace]}), fixture
    )

    assert evaluation.mechanism_evidence_match is False


def test_v30_error_transition_without_exception_does_not_ground_application_error() -> None:
    run = _observed_transition_run()
    assert run.diagnosis is not None
    run = run.model_copy(
        update={
            "diagnosis": run.diagnosis.model_copy(update={"evidence": run.diagnosis.evidence[:1]}),
            "tool_calls": run.tool_calls[:1],
            "tool_calls_requested": 1,
        }
    )

    evaluation = evaluate_aegis_transfer_run(
        run, load_transfer_scorer_fixture(FORMAL_SCORER_FIXTURE)
    )

    assert evaluation.diagnosis_correct is True
    assert evaluation.mechanism_evidence_match is False
    assert evaluation.required_evidence_covered is False
    assert evaluation.efficiency_eligible is False


def test_v30_operation_error_result_grounds_locus_but_not_exception_mechanism() -> None:
    run = _exception_run()
    assert run.diagnosis is not None
    result = QueryResult(
        query_id="q01",
        columns=["train_operation", "span_status_code", "operation_count"],
        rows=[
            [
                "GET /api/v1/trainservice/trains/byName/{name}",
                "STATUS_CODE_ERROR",
                12,
            ]
        ],
        elapsed_seconds=0,
    )
    evidence = Evidence(
        query_id="q01",
        claim="The train by-name operation has source Error spans.",
        claim_types=[EvidenceClaimType.CAUSAL_LOCUS],
    )
    trace = ToolTrace(
        tool_name="execute_sql",
        input={
            "query": """SELECT span_name AS train_operation, span_status_code,
       COUNT(*) AS operation_count
FROM traces
WHERE service_name = 'ts-train-service'
  AND timestamp >= '2025-07-18T12:21:57Z'
  AND timestamp <= '2025-07-18T12:29:56Z'
GROUP BY span_name, span_status_code"""
        },
        query_id="q01",
        output=result.model_dump(mode="json"),
        database_load=DatabaseLoad(query_count=1, rows_returned=1),
    )
    run = run.model_copy(
        update={
            "diagnosis": run.diagnosis.model_copy(update={"evidence": [evidence]}),
            "tool_calls": [trace],
            "tool_calls_requested": 1,
        }
    )

    evaluation = evaluate_aegis_transfer_run(
        run, load_transfer_scorer_fixture(FORMAL_SCORER_FIXTURE)
    )

    assert evaluation.diagnosis_correct is True
    assert evaluation.causal_locus_evidence_match is True
    assert evaluation.mechanism_evidence_match is False
    assert evaluation.required_evidence_covered is False


@pytest.mark.parametrize(
    "identity_filter",
    [
        "trace_id = 'a'",
        "trace_id IN ('a', 'b')",
        "trace_id LIKE 'a%'",
        "trace_id > 'a'",
        "trace_id BETWEEN 'a' AND 'b'",
    ],
)
def test_formal_v30_rejects_literal_identity_filters(identity_filter: str) -> None:
    query = _exception_query().replace(
        "AND span_name LIKE '%retrieveByName%'",
        f"AND span_name LIKE '%retrieveByName%'\n    AND {identity_filter}",
        1,
    )

    evaluation = evaluate_aegis_transfer_run(
        _exception_run(query=query),
        load_transfer_scorer_fixture(FORMAL_SCORER_FIXTURE),
    )

    assert evaluation.mechanism_evidence_match is False
    assert evaluation.success is False


@pytest.mark.parametrize(
    "query",
    [
        """SELECT span_name, span_status_code, COUNT(*) AS calls
FROM traces
WHERE service_name = 'ts-train-service'
GROUP BY span_name, span_status_code""",
        """SELECT CASE
  WHEN 1 = 1 THEN 'GET /api/v1/trainservice/trains/byName/{name}'
  ELSE span_name END AS span_name,
  span_status_code,
  COUNT(*) AS calls
FROM traces
WHERE service_name = 'ts-train-service'
  AND timestamp >= '2025-07-18T12:21:57Z'
  AND timestamp <= '2025-07-18T12:29:56Z'
GROUP BY span_name, span_status_code""",
    ],
)
def test_v30_unbound_operation_result_does_not_ground_locus(query: str) -> None:
    run = _observed_transition_run()
    trace = run.tool_calls[0].model_copy(
        update={
            "input": {"query": query},
            "output": QueryResult(
                query_id="q01",
                columns=["span_name", "span_status_code", "calls"],
                rows=[
                    [
                        "GET /api/v1/trainservice/trains/byName/{name}",
                        "STATUS_CODE_ERROR",
                        12,
                    ]
                ],
                elapsed_seconds=0,
            ).model_dump(mode="json"),
        }
    )
    assert run.diagnosis is not None
    evidence = Evidence(
        query_id="q01",
        claim="The operation has Error spans.",
        claim_types=[EvidenceClaimType.CAUSAL_LOCUS],
    )

    evaluation = evaluate_aegis_transfer_run(
        run.model_copy(
            update={
                "diagnosis": run.diagnosis.model_copy(update={"evidence": [evidence]}),
                "tool_calls": [trace],
                "tool_calls_requested": 1,
            }
        ),
        load_transfer_scorer_fixture(FORMAL_SCORER_FIXTURE),
    )

    assert evaluation.causal_locus_evidence_match is False
    assert evaluation.required_evidence_covered is False


def test_component_mechanism_is_not_reclassified_as_propagation_edge() -> None:
    fixture = load_transfer_scorer_fixture(FORMAL_SCORER_FIXTURE)
    component_run = _exception_run()
    assert component_run.diagnosis is not None
    component_run = component_run.model_copy(
        update={
            "diagnosis": component_run.diagnosis.model_copy(
                update={"impacted_component": "ts-travel-plan-service"}
            )
        }
    )
    edge_diagnosis = Diagnosis.model_validate(
        {
            **component_run.diagnosis.model_dump(mode="json"),
            "causal_scope": "dependency_edge",
            "causal_component": None,
            "edge_source": "ts-travel-plan-service",
            "edge_destination": "ts-train-service",
        }
    )
    edge_run = component_run.model_copy(update={"diagnosis": edge_diagnosis})

    component_evaluation = evaluate_aegis_transfer_run(component_run, fixture)
    edge_evaluation = evaluate_aegis_transfer_run(edge_run, fixture)

    assert component_evaluation.causal_locus_match is True
    assert component_evaluation.diagnosis_correct is True
    assert edge_evaluation.causal_locus_match is False
    assert edge_evaluation.diagnosis_correct is False


def test_formal_v30_accepts_case_normalized_source_predicates() -> None:
    query = (
        _exception_query()
        .replace(
            "service_name = 'ts-train-service'",
            "LOWER(service_name) = LOWER('TS-TRAIN-SERVICE')",
        )
        .replace(
            "span_status_code = 'STATUS_CODE_ERROR'",
            "UPPER(span_status_code) = 'STATUS_CODE_ERROR'",
        )
        .replace(
            "span_name LIKE '%retrieveByName%'",
            "LOWER(span_name) = LOWER('TrainController.retrieveByName')",
        )
        .replace(
            "level IN ('ERROR', 'SEVERE', 'FATAL')",
            "UPPER(level) IN ('ERROR', 'SEVERE', 'FATAL')",
        )
        .replace(
            "LOWER(line) LIKE '%exception%'",
            "LOWER(line) LIKE LOWER('%EXCEPTION%')",
        )
    )

    evaluation = evaluate_aegis_transfer_run(
        _exception_run(query=query), load_transfer_scorer_fixture(FORMAL_SCORER_FIXTURE)
    )

    assert evaluation.mechanism_evidence_match is True
    assert evaluation.success is True


@pytest.mark.parametrize(
    "query",
    [
        _exception_query().replace(
            "service_name = 'ts-train-service'",
            "LOWER(service_name) = 'ts-route-service'",
        ),
        _exception_query().replace(
            "span_status_code = 'STATUS_CODE_ERROR'",
            "UPPER(span_status_code) = 'STATUS_CODE_OK'",
        ),
        _exception_query().replace(
            "service_name = 'ts-train-service'",
            "UPPER(service_name) = 'ts-train-service'",
        ),
        _exception_query().replace(
            "span_status_code = 'STATUS_CODE_ERROR'",
            "LOWER(span_status_code) = 'STATUS_CODE_ERROR'",
        ),
    ],
)
def test_formal_v30_rejects_wrong_case_normalized_identity_or_status(query: str) -> None:
    evaluation = evaluate_aegis_transfer_run(
        _exception_run(query=query), load_transfer_scorer_fixture(FORMAL_SCORER_FIXTURE)
    )

    assert evaluation.mechanism_evidence_match is False
    assert evaluation.success is False


def test_formal_v30_rejects_case_mismatched_raw_operation_predicate() -> None:
    query = _exception_query().replace(
        "span_name LIKE '%retrieveByName%'",
        "span_name LIKE '%retrievebyname%'",
    )

    evaluation = evaluate_aegis_transfer_run(
        _exception_run(query=query), load_transfer_scorer_fixture(FORMAL_SCORER_FIXTURE)
    )

    assert evaluation.mechanism_evidence_match is False


@pytest.mark.parametrize(
    "query",
    [
        _exception_query().replace(
            "span_name LIKE '%retrieveByName%'",
            "span_name LIKE '%retrieveByName%foo'",
        ),
        _exception_query().replace(
            "LOWER(line) LIKE '%exception%'",
            "LOWER(line) LIKE '%exception%NullPointer%'",
        ),
        _exception_query().replace(
            "span_name LIKE '%retrieveByName%'",
            "span_name IN ('TrainController.retrieveByName', 'listTrains')",
        ),
    ],
)
def test_formal_v30_rejects_narrowed_mechanism_predicates(query: str) -> None:
    evaluation = evaluate_aegis_transfer_run(
        _exception_run(query=query), load_transfer_scorer_fixture(FORMAL_SCORER_FIXTURE)
    )

    assert evaluation.mechanism_evidence_match is False


def test_formal_v30_accepts_singleton_service_in_and_typed_timestamps() -> None:
    query = _exception_query().replace(
        "service_name = 'ts-train-service'",
        "service_name IN ('ts-train-service')",
    )
    for value in (
        "2025-07-18T12:21:57Z",
        "2025-07-18T12:25:57Z",
        "2025-07-18T12:29:56Z",
    ):
        query = query.replace(f"'{value}'", f"TIMESTAMP '{value}'")

    evaluation = evaluate_aegis_transfer_run(
        _exception_run(query=query), load_transfer_scorer_fixture(FORMAL_SCORER_FIXTURE)
    )

    assert evaluation.mechanism_evidence_match is True
    assert evaluation.success is True


def test_formal_v30_causal_operation_is_part_of_primary_correctness() -> None:
    fixture = load_transfer_scorer_fixture(FORMAL_SCORER_FIXTURE)

    for operation in (
        "retrieveByName",
        "TrainController.retrieveByName",
        "TrainController.retrieveByName()",
        "  traincontroller.RETRIEVEBYNAME  ",
        "GET /api/v1/trainservice/trains/byName/{name}",
    ):
        assert evaluate_aegis_transfer_run(
            _exception_run(operation=operation), fixture
        ).diagnosis_correct
    wrong = evaluate_aegis_transfer_run(_exception_run(operation="listTrains"), fixture)
    missing = evaluate_aegis_transfer_run(_exception_run(operation=None), fixture)

    assert wrong.causal_operation_match is False
    assert wrong.diagnosis_correct is False
    assert missing.causal_operation_match is False


def test_formal_v30_component_scope_rejects_edge_fields() -> None:
    run = _exception_run()
    assert run.diagnosis is not None
    run = run.model_copy(
        update={
            "diagnosis": run.diagnosis.model_copy(
                update={
                    "edge_source": "ts-travel-plan-service",
                    "edge_destination": "ts-train-service",
                }
            )
        }
    )
    evaluation = evaluate_aegis_transfer_run(
        run,
        load_transfer_scorer_fixture(FORMAL_SCORER_FIXTURE),
    )

    assert evaluation.causal_component_match is False
    assert evaluation.diagnosis_correct is False


def test_formal_v30_rejects_hardcoded_or_nonfilter_exception_evidence() -> None:
    fixture = load_transfer_scorer_fixture(FORMAL_SCORER_FIXTURE)
    hardcoded = _exception_query().replace(
        "COALESCE(SUM(e.error_span_count), 0) AS error_span_count",
        "12 AS error_span_count",
    )
    neutralized = _exception_query().replace(
        "service_name = 'ts-train-service'",
        "(service_name = 'ts-train-service' OR 1 = 1)",
        1,
    )
    dead_alias = (
        _exception_query()
        .replace(
            "span_name LIKE '%retrieveByName%'",
            "span_name LIKE '%failed%'",
        )
        .replace(
            "1 AS error_span_count",
            "1 AS retrieveByName_error_span_count",
            1,
        )
    )
    dead_aggregate = hardcoded.replace(
        "WITH periods AS (",
        "WITH decoy AS (SELECT SUM(1) AS error_span_count), periods AS (",
    )
    wrong_aggregate_source = _exception_query().replace(
        "SUM(e.error_span_count)",
        "SUM(e.exception_log_count)",
        1,
    )

    assert not evaluate_aegis_transfer_run(_exception_run(query=hardcoded), fixture).success
    assert not evaluate_aegis_transfer_run(_exception_run(query=neutralized), fixture).success
    assert not evaluate_aegis_transfer_run(_exception_run(query=dead_alias), fixture).success
    assert not evaluate_aegis_transfer_run(_exception_run(query=dead_aggregate), fixture).success
    assert not evaluate_aegis_transfer_run(
        _exception_run(query=wrong_aggregate_source), fixture
    ).success


def test_formal_v30_binds_filters_to_each_telemetry_select() -> None:
    fixture = load_transfer_scorer_fixture(FORMAL_SCORER_FIXTURE)
    dead_status_predicate = _exception_query().replace(
        "span_status_code = 'STATUS_CODE_ERROR'",
        "span_status_code = 'STATUS_CODE_UNSET' "
        "AND EXISTS (SELECT 1 WHERE span_status_code = 'STATUS_CODE_ERROR')",
    )
    unbounded_trace_scan = _exception_query().replace(
        "    AND timestamp >= '2025-07-18T12:21:57Z'\n    AND timestamp < '2025-07-18T12:29:56Z'\n",
        "",
        1,
    )
    multiplied_trace_rows = _exception_query().replace(
        "  FROM traces\n",
        "  FROM traces CROSS JOIN (SELECT 1 AS duplicate UNION ALL SELECT 2 AS duplicate) copies\n",
        1,
    )

    assert not evaluate_aegis_transfer_run(
        _exception_run(query=dead_status_predicate), fixture
    ).mechanism_evidence_match
    assert not evaluate_aegis_transfer_run(
        _exception_run(query=unbounded_trace_scan), fixture
    ).mechanism_evidence_match
    assert not evaluate_aegis_transfer_run(
        _exception_run(query=multiplied_trace_rows), fixture
    ).mechanism_evidence_match


def test_formal_v30_requires_aggregate_periods_to_follow_source_timestamps() -> None:
    fixture = load_transfer_scorer_fixture(FORMAL_SCORER_FIXTURE)
    mislabeled_period = _exception_query().replace("THEN 'normal'", "THEN 'abnormal'", 1)
    boundary_case = (
        _exception_query()
        .replace(
            "CASE\n"
            "           WHEN timestamp >= '2025-07-18T12:21:57Z'\n"
            "            AND timestamp < '2025-07-18T12:25:57Z' THEN 'normal'\n"
            "           WHEN timestamp >= '2025-07-18T12:25:57Z'\n"
            "            AND timestamp < '2025-07-18T12:29:56Z' THEN 'abnormal'\n"
            "         END",
            "CASE WHEN timestamp < '2025-07-18T12:25:57Z' THEN 'normal' ELSE 'abnormal' END",
        )
        .replace(
            "CASE\n"
            "           WHEN greptime_timestamp >= '2025-07-18T12:21:57Z'\n"
            "            AND greptime_timestamp < '2025-07-18T12:25:57Z' THEN 'normal'\n"
            "           WHEN greptime_timestamp >= '2025-07-18T12:25:57Z'\n"
            "            AND greptime_timestamp < '2025-07-18T12:29:56Z' THEN 'abnormal'\n"
            "         END",
            "CASE WHEN greptime_timestamp >= '2025-07-18T12:25:57Z' "
            "THEN 'abnormal' ELSE 'normal' END",
        )
    )

    assert not evaluate_aegis_transfer_run(
        _exception_run(query=mislabeled_period), fixture
    ).mechanism_evidence_match
    assert evaluate_aegis_transfer_run(
        _exception_run(query=boundary_case), fixture
    ).mechanism_evidence_match


def test_formal_v30_rejects_wrong_scope_window_and_incomplete_mechanism() -> None:
    fixture = load_transfer_scorer_fixture(FORMAL_SCORER_FIXTURE)
    mutations = (
        _exception_query().replace("ts-train-service", "ts-route-plan-service"),
        _exception_query().replace("STATUS_CODE_ERROR", "STATUS_CODE_UNSET"),
        _exception_query().replace("retrieveByName", "listTrains"),
        _exception_query().replace("%exception%", "%timeout%"),
        _exception_query().replace("2025-07-18T12:29:56Z", "2025-07-18T12:29:00Z"),
        f"{_exception_query()} LIMIT 2",
    )

    for query in mutations:
        assert not evaluate_aegis_transfer_run(_exception_run(query=query), fixture).success

    no_logs = _exception_result().model_copy(
        update={"rows": [["normal", 0, 0], ["abnormal", 12, 0]]}
    )
    baseline_not_clean = _exception_result().model_copy(
        update={"rows": [["normal", 1, 0], ["abnormal", 12, 11]]}
    )
    assert not evaluate_aegis_transfer_run(
        _exception_run(result=no_logs), fixture
    ).mechanism_evidence_match
    assert not evaluate_aegis_transfer_run(
        _exception_run(result=baseline_not_clean), fixture
    ).mechanism_evidence_match


def test_formal_v30_malformed_combined_result_fails_closed() -> None:
    malformed = QueryResult(
        query_id="q01",
        columns=["period", "error_span_count", "exception_log_count"],
        rows=[["normal"]],
        elapsed_seconds=0,
    )

    evaluation = evaluate_aegis_transfer_run(
        _exception_run(result=malformed),
        load_transfer_scorer_fixture(FORMAL_SCORER_FIXTURE),
    )

    assert evaluation.mechanism_evidence_match is False


def _single_table_exception_query(table: str) -> str:
    if table == "traces":
        source = """SELECT CASE
           WHEN timestamp >= '2025-07-18T12:21:57Z'
            AND timestamp < '2025-07-18T12:25:57Z' THEN 'normal'
           WHEN timestamp >= '2025-07-18T12:25:57Z'
            AND timestamp < '2025-07-18T12:29:56Z' THEN 'abnormal'
         END AS period,
         timestamp AS observed_at
  FROM traces
  WHERE service_name = 'ts-train-service'
    AND span_status_code = 'STATUS_CODE_ERROR'
    AND span_name LIKE '%retrieveByName%'
    AND timestamp >= '2025-07-18T12:21:57Z'
    AND timestamp < '2025-07-18T12:29:56Z'"""
    else:
        source = """SELECT CASE
           WHEN greptime_timestamp >= '2025-07-18T12:21:57Z'
            AND greptime_timestamp < '2025-07-18T12:25:57Z' THEN 'normal'
           WHEN greptime_timestamp >= '2025-07-18T12:25:57Z'
            AND greptime_timestamp < '2025-07-18T12:29:56Z' THEN 'abnormal'
         END AS period,
         greptime_timestamp AS observed_at
  FROM logs
  WHERE service_name = 'ts-train-service'
    AND level = 'ERROR'
    AND LOWER(line) LIKE '%exception%'
    AND greptime_timestamp >= '2025-07-18T12:21:57Z'
    AND greptime_timestamp < '2025-07-18T12:29:56Z'"""
    return f"""WITH periods AS (
  SELECT 'normal' AS period UNION ALL SELECT 'abnormal'
), observations AS (
  {source}
)
SELECT p.period, COUNT(o.observed_at) AS observation_count
FROM periods p
LEFT JOIN observations o ON o.period = p.period
GROUP BY p.period"""


def test_formal_v30_accepts_split_trace_and_log_aggregates() -> None:
    base = _exception_run()
    assert base.diagnosis is not None
    traces: list[ToolTrace] = []
    evidence: list[Evidence] = []
    for index, table in enumerate(("traces", "logs"), start=1):
        query_id = f"q0{index}"
        result = QueryResult(
            query_id=query_id,
            columns=["period", "observation_count"],
            rows=[["normal", 0], ["abnormal", 7]],
            elapsed_seconds=0,
        )
        traces.append(
            ToolTrace(
                tool_name="execute_sql",
                input={"query": _single_table_exception_query(table)},
                query_id=query_id,
                output=result.model_dump(mode="json"),
                database_load=DatabaseLoad(query_count=1, rows_returned=2),
            )
        )
        evidence.append(
            Evidence(
                query_id=query_id,
                claim=f"The {table} result proves its baseline-to-anomaly transition.",
                claim_types=[EvidenceClaimType.CAUSAL_LOCUS, EvidenceClaimType.FAULT_MECHANISM],
            )
        )
    run = base.model_copy(
        update={
            "diagnosis": base.diagnosis.model_copy(update={"evidence": evidence}),
            "tool_calls": traces,
            "tool_calls_requested": 2,
        }
    )

    evaluation = evaluate_aegis_transfer_run(
        run, load_transfer_scorer_fixture(FORMAL_SCORER_FIXTURE)
    )

    assert evaluation.mechanism_evidence_match is True
    assert evaluation.supporting_evidence_query_ids == ["q01", "q02"]
    assert evaluation.success is True


def test_formal_v30_malformed_split_aggregate_fails_closed() -> None:
    run = _exception_run()
    malformed = QueryResult(
        query_id="q01",
        columns=["period", "observation_count"],
        rows=[["normal"]],
        elapsed_seconds=0,
    )
    trace = run.tool_calls[0].model_copy(
        update={
            "input": {"query": _single_table_exception_query("traces")},
            "output": malformed.model_dump(mode="json"),
        }
    )
    run = run.model_copy(update={"tool_calls": [trace]})

    evaluation = evaluate_aegis_transfer_run(
        run, load_transfer_scorer_fixture(FORMAL_SCORER_FIXTURE)
    )

    assert evaluation.mechanism_evidence_match is False


def test_formal_v30_accepts_equivalent_filter_and_count_expressions() -> None:
    base = _exception_run()
    assert base.diagnosis is not None
    traces: list[ToolTrace] = []
    evidence: list[Evidence] = []
    for index, table in enumerate(("traces", "logs"), start=1):
        query_id = f"q0{index}"
        query = _single_table_exception_query(table).replace(
            "COUNT(o.observed_at) AS observation_count",
            "SUM(CASE WHEN o.observed_at IS NOT NULL THEN 1 ELSE 0 END) AS observation_count",
        )
        if table == "traces":
            query = query.replace(
                "span_name LIKE '%retrieveByName%'",
                "span_name = 'TrainController.retrieveByName'",
            ).replace(
                "span_status_code = 'STATUS_CODE_ERROR'",
                "span_status_code IN ('STATUS_CODE_ERROR')",
            )
        result = QueryResult(
            query_id=query_id,
            columns=["period", "observation_count"],
            rows=[["normal", 0], ["abnormal", 7]],
            elapsed_seconds=0,
        )
        traces.append(
            ToolTrace(
                tool_name="execute_sql",
                input={"query": query},
                query_id=query_id,
                output=result.model_dump(mode="json"),
                database_load=DatabaseLoad(query_count=1, rows_returned=2),
            )
        )
        evidence.append(
            Evidence(
                query_id=query_id,
                claim=f"The {table} result proves its baseline-to-anomaly transition.",
                claim_types=[EvidenceClaimType.CAUSAL_LOCUS, EvidenceClaimType.FAULT_MECHANISM],
            )
        )
    run = base.model_copy(
        update={
            "diagnosis": base.diagnosis.model_copy(update={"evidence": evidence}),
            "tool_calls": traces,
            "tool_calls_requested": 2,
        }
    )

    evaluation = evaluate_aegis_transfer_run(
        run, load_transfer_scorer_fixture(FORMAL_SCORER_FIXTURE)
    )

    assert evaluation.mechanism_evidence_match is True
    assert evaluation.success is True


def test_formal_v30_graph_entity_does_not_ground_locus_or_mechanism() -> None:
    run = _exception_run()
    assert run.diagnosis is not None
    result = QueryResult(
        query_id="q01",
        columns=["entity_type", "entity_id"],
        rows=[["service", "ts-train-service"]],
        elapsed_seconds=0,
    )
    run = run.model_copy(
        update={
            "diagnosis": run.diagnosis.model_copy(
                update={
                    "evidence": [
                        Evidence(
                            query_id="q01",
                            claim="The semantic entity identifies the affected component.",
                            claim_types=[
                                EvidenceClaimType.CAUSAL_LOCUS,
                                EvidenceClaimType.FAULT_MECHANISM,
                            ],
                        )
                    ]
                }
            ),
            "visibility": Visibility.SEMANTIC_GRAPH,
            "tool_calls": [
                ToolTrace(
                    tool_name="query_semantic_graph",
                    input={"view": "entities"},
                    query_id="q01",
                    output=result.model_dump(mode="json"),
                    database_load=DatabaseLoad(query_count=1, rows_returned=1),
                )
            ],
        }
    )

    evaluation = evaluate_aegis_transfer_run(
        run, load_transfer_scorer_fixture(FORMAL_SCORER_FIXTURE)
    )

    assert evaluation.causal_locus_evidence_match is False
    assert evaluation.mechanism_evidence_match is False
    assert evaluation.success is False
