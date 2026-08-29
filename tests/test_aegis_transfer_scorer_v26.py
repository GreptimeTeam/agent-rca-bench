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
            query_id="q01",
            claim="The Graph contains the directed caller-to-callee edge.",
            claim_types=[EvidenceClaimType.CAUSAL_SCOPE],
        ),
        Evidence(
            query_id="q02",
            claim="The paired-span start gap crosses the threshold after onset.",
            claim_types=[EvidenceClaimType.FAULT_MECHANISM],
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
        run_id="v26-synthetic",
        visibility=Visibility.SEMANTIC_GRAPH,
        model="deepseek-v4-flash",
        runner=AgentRunner.API,
        diagnosis=Diagnosis(
            affected_component="ts-route-plan-service",
            causal_dependency="ts-travel2-service",
            causal_scope=CausalScope.DEPENDENCY_EDGE,
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


def test_v26_scores_structured_diagnosis_evidence_and_execution_separately() -> None:
    evaluation = evaluate_aegis_transfer_run(
        _run(), load_transfer_scorer_fixture(CALIBRATION_SCORER_FIXTURE)
    )

    assert evaluation.diagnosis_correct is True
    assert evaluation.causal_scope_evidence_match is True
    assert evaluation.mechanism_evidence_match is True
    assert evaluation.required_evidence_covered is True
    assert evaluation.citation_integrity is True
    assert evaluation.execution_reliability is True
    assert evaluation.efficiency_eligible is True
    assert evaluation.auditable_completion is True
    assert evaluation.success is True


def test_v26_free_text_fault_type_does_not_drive_structured_correctness() -> None:
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


def test_v26_wrong_structured_mechanism_fails_even_with_correct_free_text() -> None:
    evaluation = evaluate_aegis_transfer_run(
        _run(mechanism_code=MechanismCode.CONNECTION_FAILURE),
        load_transfer_scorer_fixture(CALIBRATION_SCORER_FIXTURE),
    )

    assert evaluation.mechanism_code_match is False
    assert evaluation.diagnosis_correct is False
    assert evaluation.efficiency_eligible is False


def test_v26_invalid_extra_citation_is_reliability_not_diagnosis_failure() -> None:
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


def test_v26_accepts_threshold_proof_without_canonical_source_counts() -> None:
    evaluation = evaluate_aegis_transfer_run(
        _run(mechanism_result=_start_gap_result()),
        load_transfer_scorer_fixture(CALIBRATION_SCORER_FIXTURE),
    )

    assert evaluation.mechanism_evidence_match is True


def test_v26_rejects_singleton_wrong_parent_and_server_duration_evidence() -> None:
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


def test_v26_does_not_double_count_repeated_singleton_evidence() -> None:
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


def test_v26_accepts_raw_paired_timestamps_without_prescribed_aggregation() -> None:
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
def test_v26_malformed_start_gap_rows_fail_closed(query: str, result: QueryResult) -> None:
    evaluation = evaluate_aegis_transfer_run(
        _run(query=query, mechanism_result=result),
        load_transfer_scorer_fixture(CALIBRATION_SCORER_FIXTURE),
    )

    assert evaluation.mechanism_evidence_match is False


def test_v26_invalid_optional_minimum_fails_closed() -> None:
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


def test_v26_rejects_result_aliases_not_derived_from_start_gap() -> None:
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


def test_v26_excludes_time_bins_that_cross_non_minute_window_boundaries() -> None:
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


def test_v26_rejects_narrowed_window_and_reversed_start_gap() -> None:
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
    dependency: str | None = None,
) -> AgentRun:
    evidence = Evidence(
        query_id="q01",
        claim=(
            "The operation has no baseline errors and repeated error spans and "
            "exception logs after onset."
        ),
        claim_types=[EvidenceClaimType.CAUSAL_SCOPE, EvidenceClaimType.FAULT_MECHANISM],
    )
    output = result or _exception_result()
    return AgentRun(
        run_id="v26-formal-synthetic",
        visibility=Visibility.RAW,
        model="deepseek-v4-pro",
        runner=AgentRunner.API,
        diagnosis=Diagnosis(
            affected_component="ts-train-service",
            causal_dependency=dependency,
            causal_scope=CausalScope.COMPONENT,
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


def test_formal_v26_accepts_component_scoped_exception_transition() -> None:
    evaluation = evaluate_aegis_transfer_run(
        _exception_run(), load_transfer_scorer_fixture(FORMAL_SCORER_FIXTURE)
    )

    assert evaluation.diagnosis_correct is True
    assert evaluation.mechanism_evidence_match is True
    assert evaluation.efficiency_eligible is True
    assert evaluation.success is True


def test_formal_v26_accepts_case_normalized_source_predicates() -> None:
    query = (
        _exception_query()
        .replace(
            "span_name LIKE '%retrieveByName%'",
            "LOWER(span_name) LIKE '%retrievebyname%'",
        )
        .replace(
            "level IN ('ERROR', 'SEVERE', 'FATAL')",
            "UPPER(level) IN ('ERROR', 'SEVERE', 'FATAL')",
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
            "LOWER(service_name) = 'ts-train-service'",
            1,
        ),
        _exception_query().replace(
            "span_status_code = 'STATUS_CODE_ERROR'",
            "UPPER(span_status_code) = 'STATUS_CODE_ERROR'",
        ),
    ],
)
def test_formal_v26_does_not_case_fold_source_identity_or_status(query: str) -> None:
    evaluation = evaluate_aegis_transfer_run(
        _exception_run(query=query), load_transfer_scorer_fixture(FORMAL_SCORER_FIXTURE)
    )

    assert evaluation.mechanism_evidence_match is False
    assert evaluation.success is False


def test_formal_v26_causal_operation_is_part_of_primary_correctness() -> None:
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


def test_formal_v26_component_scope_requires_null_dependency() -> None:
    evaluation = evaluate_aegis_transfer_run(
        _exception_run(dependency="ts-route-plan-service"),
        load_transfer_scorer_fixture(FORMAL_SCORER_FIXTURE),
    )

    assert evaluation.causal_dependency_match is False
    assert evaluation.diagnosis_correct is False


def test_formal_v26_rejects_hardcoded_or_nonfilter_exception_evidence() -> None:
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


def test_formal_v26_binds_filters_to_each_telemetry_select() -> None:
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


def test_formal_v26_requires_aggregate_periods_to_follow_source_timestamps() -> None:
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


def test_formal_v26_rejects_wrong_scope_window_and_incomplete_mechanism() -> None:
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


def test_formal_v26_malformed_combined_result_fails_closed() -> None:
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


def test_formal_v26_accepts_split_trace_and_log_aggregates() -> None:
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
                claim_types=[EvidenceClaimType.CAUSAL_SCOPE, EvidenceClaimType.FAULT_MECHANISM],
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


def test_formal_v26_malformed_split_aggregate_fails_closed() -> None:
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


def test_formal_v26_accepts_equivalent_filter_and_count_expressions() -> None:
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
                claim_types=[EvidenceClaimType.CAUSAL_SCOPE, EvidenceClaimType.FAULT_MECHANISM],
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


def test_formal_v26_graph_entity_does_not_substitute_for_mechanism_evidence() -> None:
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
                                EvidenceClaimType.CAUSAL_SCOPE,
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

    assert evaluation.causal_scope_evidence_match is True
    assert evaluation.mechanism_evidence_match is False
    assert evaluation.success is False
