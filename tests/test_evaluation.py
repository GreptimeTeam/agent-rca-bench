import pytest

from semantic_rca_bench.contracts import (
    AgentRun,
    AgentUsage,
    Diagnosis,
    FaultCategory,
    GroundTruth,
    MechanismCode,
    QueryResult,
    RejectedToolCall,
    ToolTrace,
    Visibility,
)
from semantic_rca_bench.evaluation import (
    component_matches,
    evaluate,
    fault_type_matches,
    ground_truth_fault_category,
)


def _run(fault_category: FaultCategory, fault_type: str) -> AgentRun:
    return AgentRun(
        run_id="run-1",
        visibility=Visibility.RAW,
        model="test-model",
        diagnosis=Diagnosis(
            causal_scope="component",
            causal_component="checkoutservice",
            fault_category=fault_category,
            mechanism_code=MechanismCode.UNKNOWN,
            fault_type=fault_type,
            confidence=0.7,
            explanation="test diagnosis",
            evidence=[],
        ),
        tool_calls=[],
        usage=AgentUsage(),
        elapsed_seconds=1,
        responses=[],
    )


def _query_result(*, query_id: str = "q01", truncated: bool = False) -> dict[str, object]:
    return QueryResult(
        query_id=query_id,
        columns=["latency"],
        rows=[[3.5]],
        elapsed_seconds=0.1,
        truncated=truncated,
    ).model_dump(mode="json")


def _sql_trace(
    query: str = "SELECT * FROM checkout_latency",
    *,
    output_query_id: str = "q01",
    truncated: bool = False,
    error: str | None = None,
) -> ToolTrace:
    return ToolTrace(
        tool_name="execute_sql",
        input={"query": query},
        query_id="q01",
        output=_query_result(query_id=output_query_id, truncated=truncated),
        error=error,
    )


def _run_with_evidence(
    traces: list[ToolTrace],
    *,
    claim: str = "latency increased",
    **run_updates: object,
) -> AgentRun:
    diagnosis = Diagnosis(
        causal_scope="component",
        causal_component="checkoutservice",
        fault_category=FaultCategory.DELAY,
        mechanism_code=MechanismCode.CALL_PATH_DELAY,
        fault_type="delay",
        confidence=0.7,
        evidence=[{"query_id": "q01", "claim": claim}],
        explanation="test diagnosis",
    )
    return _run(FaultCategory.DELAY, "delay").model_copy(
        update={"diagnosis": diagnosis, "tool_calls": traces, **run_updates}
    )


def test_causal_fault_category_takes_precedence_over_latency_symptom() -> None:
    result = evaluate(
        _run(FaultCategory.CPU, "CPU saturation causing latency degradation"),
        GroundTruth(causal_component="checkoutservice", fault_type="delay", inject_time=0),
    )

    assert result.causal_component_match
    assert result.predicted_fault_category == "cpu"
    assert result.expected_fault_category == "delay"
    assert not result.fault_type_match
    assert not result.fault_category_match
    assert not result.joint_match


def test_delay_mechanism_matches_delay_ground_truth() -> None:
    result = evaluate(
        _run(FaultCategory.DELAY, "delay"),
        GroundTruth(causal_component="checkoutservice", fault_type="delay", inject_time=0),
    )

    assert result.predicted_fault_category == "delay"
    assert result.fault_type_match
    assert result.fault_category_match
    assert result.joint_match


def test_exact_fault_type_is_independent_of_broad_category() -> None:
    result = evaluate(
        _run(FaultCategory.OTHER, "rateLimiting"),
        GroundTruth(
            causal_component="checkoutservice",
            fault_type="httpError5xx",
            fault_category=FaultCategory.OTHER,
            inject_time=0,
        ),
    )

    assert result.fault_category_match
    assert not result.fault_type_match
    assert not result.joint_match


def test_fault_type_normalizes_case_and_separators_only() -> None:
    assert fault_type_matches("HTTP-error_5xx", "httpError5xx")
    assert not fault_type_matches("httpError5xx", "rateLimiting")


def test_native_ground_truth_alias_is_canonicalized() -> None:
    assert ground_truth_fault_category("mem") == "memory"
    assert ground_truth_fault_category("loss") == "loss"


def test_component_allows_parenthetical_qualifier_only() -> None:
    assert component_matches("checkoutservice (pod network egress path)", "checkoutservice")
    assert component_matches("payment service (PaymentService/Charge)", "payment")
    assert component_matches("checkout", "checkoutservice")
    assert component_matches("geo service pod (geo-85ff, deployment geo)", "geo")
    assert not component_matches("checkoutservice dependency", "checkoutservice")
    assert not component_matches("frontend/checkoutservice", "checkoutservice")


def test_evaluation_records_discovery_and_completion_efficiency() -> None:
    cited_result = QueryResult(
        query_id="q03",
        columns=["service", "latency"],
        rows=[["checkoutservice", 3.5]],
        elapsed_seconds=0.1,
    )
    run = _run(FaultCategory.DELAY, "delay").model_copy(
        update={
            "tool_calls": [
                ToolTrace(
                    tool_name="execute_sql",
                    input={"query": "SHOW TABLES"},
                    query_id="q01",
                ),
                ToolTrace(
                    tool_name="search_table_semantics",
                    input={"query": "latency"},
                    query_id="q02",
                ),
                ToolTrace(
                    tool_name="execute_sql",
                    input={"query": "SELECT * FROM checkout_latency"},
                    query_id="q03",
                    output=cited_result.model_dump(mode="json"),
                ),
                ToolTrace(
                    tool_name="execute_sql",
                    input={"query": "SELECT * FROM checkout_latency"},
                    error="bad column",
                ),
            ],
            "tool_calls_requested": 4,
            "diagnosis": Diagnosis(
                causal_scope="component",
                causal_component="checkoutservice",
                fault_category=FaultCategory.DELAY,
                mechanism_code=MechanismCode.CALL_PATH_DELAY,
                fault_type="delay",
                confidence=0.7,
                evidence=[{"query_id": "q03", "claim": "latency increased"}],
                explanation="test diagnosis",
            ),
        }
    )

    result = evaluate(
        run,
        GroundTruth(causal_component="checkoutservice", fault_type="delay", inject_time=0),
    )

    assert result.discovery_calls == 2
    assert result.discovery_calls_before_first_cited_query == 2
    assert result.semantic_calls == 1
    assert result.failed_calls == 1
    assert result.exact_repeated_calls == 1
    assert result.valid_evidence_count == 1
    assert result.correct_completion_tool_calls == 4


def test_correct_diagnosis_without_evidence_is_not_a_valid_completion() -> None:
    result = evaluate(
        _run(FaultCategory.DELAY, "delay"),
        GroundTruth(causal_component="checkoutservice", fault_type="delay", inject_time=0),
    )

    assert result.joint_match
    assert result.cited_evidence_count == 0
    assert result.valid_evidence_count == 0
    assert result.correct_completion_tool_calls is None


@pytest.mark.parametrize(
    "traces, claim",
    [
        ([_sql_trace(truncated=True)], "latency increased"),
        (
            [
                ToolTrace(
                    tool_name="search_table_semantics",
                    input={"query": "latency"},
                    query_id="q01",
                    output=_query_result(),
                )
            ],
            "the catalog matched a table",
        ),
        ([_sql_trace(error="query failed")], "latency increased"),
        ([_sql_trace()], "   "),
        ([_sql_trace("SHOW TABLES")], "the table exists"),
        ([_sql_trace(output_query_id="different")], "latency increased"),
        (
            [
                _sql_trace(),
                _sql_trace("SELECT max(latency) FROM checkout_latency"),
            ],
            "latency increased",
        ),
    ],
    ids=(
        "truncated",
        "semantic-metadata-tool",
        "failed-query",
        "blank-claim",
        "metadata-only",
        "output-id-mismatch",
        "duplicate-query-id",
    ),
)
def test_invalid_evidence_cannot_unlock_completion_efficiency(traces, claim) -> None:
    result = evaluate(
        _run_with_evidence(traces, claim=claim),
        GroundTruth(causal_component="checkoutservice", fault_type="delay", inject_time=0),
    )

    assert result.joint_match
    assert result.cited_evidence_count == 1
    assert result.valid_evidence_count == 0
    assert result.correct_completion_tool_calls is None


@pytest.mark.parametrize(
    "query",
    [
        "SHOW FULL TABLES",
        "SELECT table_name FROM information_schema.tables",
        "SELECT * FROM pg_catalog.pg_tables",
        "SELECT * FROM greptime_private.semantic_table_metadata",
        "SELECT 1",
        "SELECT * FROM checkout_latency; SELECT * FROM payment_latency",
    ],
)
def test_metadata_or_non_table_sql_is_not_incident_evidence(query) -> None:
    result = evaluate(
        _run_with_evidence([_sql_trace(query)], claim="query returned a row"),
        GroundTruth(causal_component="checkoutservice", fault_type="delay", inject_time=0),
    )

    assert result.valid_evidence_count == 0
    assert result.correct_completion_tool_calls is None


@pytest.mark.parametrize("run_state", [{"error": "runner failed"}, {"tool_budget_exhausted": True}])
def test_runner_failure_or_budget_hit_cannot_unlock_completion_efficiency(run_state) -> None:
    result = evaluate(
        _run_with_evidence([_sql_trace()], **run_state),
        GroundTruth(causal_component="checkoutservice", fault_type="delay", inject_time=0),
    )

    assert result.joint_match
    assert result.valid_evidence_count == 1
    assert result.correct_completion_tool_calls is None


def test_successful_graph_query_is_execution_valid_evidence() -> None:
    diagnosis = Diagnosis(
        causal_scope="component",
        causal_component="frontend",
        impacted_component="search",
        fault_category=FaultCategory.DELAY,
        mechanism_code=MechanismCode.CALL_PATH_DELAY,
        fault_type="delay",
        confidence=0.7,
        evidence=[{"query_id": "q01", "claim": "search returned errors"}],
        explanation="test diagnosis",
    )
    run = _run(FaultCategory.DELAY, "delay").model_copy(
        update={
            "diagnosis": diagnosis,
            "tool_calls": [
                ToolTrace(
                    tool_name="query_semantic_graph",
                    input={"view": "relationships", "src_id": "frontend"},
                    query_id="q01",
                    output=_query_result(),
                )
            ],
        }
    )

    result = evaluate(
        run,
        GroundTruth(causal_component="frontend", fault_type="delay", inject_time=0),
    )

    assert result.valid_evidence_count == 1
    assert result.valid_completion is True
    assert result.correct_completion_tool_calls == 1


def test_parenthesized_select_is_execution_valid_evidence() -> None:
    result = evaluate(
        _run_with_evidence([_sql_trace("(SELECT * FROM checkout_latency)")]),
        GroundTruth(causal_component="checkoutservice", fault_type="delay", inject_time=0),
    )

    assert result.valid_evidence_count == 1
    assert result.valid_completion is True


def test_final_output_superseded_call_is_not_a_failed_call() -> None:
    run = _run_with_evidence([_sql_trace()]).model_copy(
        update={
            "rejected_tool_calls": [
                RejectedToolCall(
                    tool_name="execute_sql",
                    input={"query": "SELECT 1"},
                    error="final output won",
                    reason_code="superseded_by_final_output",
                )
            ]
        }
    )

    result = evaluate(
        run,
        GroundTruth(causal_component="checkoutservice", fault_type="delay", inject_time=0),
    )

    assert result.valid_completion is True
    assert result.failed_calls == 0


def test_unscoreable_component_excludes_joint_accuracy() -> None:
    result = evaluate(
        _run(FaultCategory.OTHER, "redisUnavailable"),
        GroundTruth(
            causal_component="cart-64944cd445-8pbgx",
            component_scoreable=False,
            component_alternatives=["cart"],
            fault_type="redisUnavailable",
            inject_time=0,
        ),
    )

    assert result.causal_component_match is None
    assert result.joint_match is None
    assert result.fault_type_match
    assert result.correct_completion_tool_calls is None


def test_impacted_component_remains_descriptive_without_ground_truth_scoring() -> None:
    run = _run(FaultCategory.OTHER, "redisUnavailable").model_copy(
        update={
            "diagnosis": Diagnosis(
                causal_scope="component",
                causal_component="cart",
                impacted_component="Valkey cart-store (Redis backend)",
                fault_category=FaultCategory.OTHER,
                mechanism_code=MechanismCode.DEPENDENCY_UNAVAILABLE,
                fault_type="redisUnavailable",
                confidence=0.7,
                explanation="test diagnosis",
            )
        }
    )

    result = evaluate(
        run,
        GroundTruth(causal_component="cart", fault_type="redisUnavailable", inject_time=0),
    )

    assert run.diagnosis is not None
    assert run.diagnosis.impacted_component == "Valkey cart-store (Redis backend)"
    assert "impacted_component_match" not in result.model_dump()


def test_failed_run_is_scored_without_a_fabricated_diagnosis() -> None:
    run = _run(FaultCategory.CPU, "cpu").model_copy(
        update={"diagnosis": None, "error": "turn limit exhausted"}
    )

    result = evaluate(
        run,
        GroundTruth(causal_component="checkout", fault_type="cpu", inject_time=0),
    )

    assert not result.causal_component_match
    assert not result.fault_type_match
    assert not result.joint_match
    assert result.predicted_fault_type is None
    assert result.correct_completion_tool_calls is None
