from semantic_rca_bench.contracts import (
    AgentRun,
    AgentUsage,
    Diagnosis,
    FaultCategory,
    GroundTruth,
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
            affected_component="checkoutservice",
            fault_category=fault_category,
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


def test_causal_fault_category_takes_precedence_over_latency_symptom() -> None:
    result = evaluate(
        _run(FaultCategory.CPU, "CPU saturation causing latency degradation"),
        GroundTruth(affected_component="checkoutservice", fault_type="delay", inject_time=0),
    )

    assert result.affected_component_match
    assert result.predicted_fault_category == "cpu"
    assert result.expected_fault_category == "delay"
    assert not result.fault_type_match
    assert not result.fault_category_match
    assert not result.joint_match


def test_delay_mechanism_matches_delay_ground_truth() -> None:
    result = evaluate(
        _run(FaultCategory.DELAY, "delay"),
        GroundTruth(affected_component="checkoutservice", fault_type="delay", inject_time=0),
    )

    assert result.predicted_fault_category == "delay"
    assert result.fault_type_match
    assert result.fault_category_match
    assert result.joint_match


def test_exact_fault_type_is_independent_of_broad_category() -> None:
    result = evaluate(
        _run(FaultCategory.OTHER, "rateLimiting"),
        GroundTruth(
            affected_component="checkoutservice",
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
                ),
                ToolTrace(
                    tool_name="execute_sql",
                    input={"query": "SELECT * FROM checkout_latency"},
                    error="bad column",
                ),
            ],
            "tool_calls_requested": 4,
            "diagnosis": Diagnosis(
                affected_component="checkoutservice",
                fault_category=FaultCategory.DELAY,
                fault_type="delay",
                confidence=0.7,
                evidence=[{"query_id": "q03", "claim": "latency increased"}],
                explanation="test diagnosis",
            ),
        }
    )

    result = evaluate(
        run,
        GroundTruth(affected_component="checkoutservice", fault_type="delay", inject_time=0),
    )

    assert result.discovery_calls == 2
    assert result.discovery_calls_before_first_cited_query == 2
    assert result.semantic_calls == 1
    assert result.failed_calls == 1
    assert result.exact_repeated_calls == 1
    assert result.correct_completion_tool_calls == 4


def test_unscoreable_component_excludes_joint_accuracy() -> None:
    result = evaluate(
        _run(FaultCategory.OTHER, "redisUnavailable"),
        GroundTruth(
            affected_component="cart-64944cd445-8pbgx",
            component_scoreable=False,
            component_alternatives=["cart"],
            fault_type="redisUnavailable",
            inject_time=0,
        ),
    )

    assert result.affected_component_match is None
    assert result.joint_match is None
    assert result.fault_type_match
    assert result.correct_completion_tool_calls is None


def test_causal_dependency_remains_diagnostic_without_ground_truth_scoring() -> None:
    run = _run(FaultCategory.OTHER, "redisUnavailable").model_copy(
        update={
            "diagnosis": Diagnosis(
                affected_component="cart",
                causal_dependency="Valkey cart-store (Redis backend)",
                fault_category=FaultCategory.OTHER,
                fault_type="redisUnavailable",
                confidence=0.7,
                explanation="test diagnosis",
            )
        }
    )

    result = evaluate(
        run,
        GroundTruth(affected_component="cart", fault_type="redisUnavailable", inject_time=0),
    )

    assert run.diagnosis is not None
    assert run.diagnosis.causal_dependency == "Valkey cart-store (Redis backend)"
    assert "causal_dependency_match" not in result.model_dump()


def test_failed_run_is_scored_without_a_fabricated_diagnosis() -> None:
    run = _run(FaultCategory.CPU, "cpu").model_copy(
        update={"diagnosis": None, "error": "turn limit exhausted"}
    )

    result = evaluate(
        run,
        GroundTruth(affected_component="checkout", fault_type="cpu", inject_time=0),
    )

    assert not result.affected_component_match
    assert not result.fault_type_match
    assert not result.joint_match
    assert result.predicted_fault_type is None
    assert result.correct_completion_tool_calls is None


def test_v17_component_fields_are_read_but_v18_names_are_serialized() -> None:
    diagnosis = Diagnosis.model_validate(
        {
            "root_cause_component": "checkout",
            "fault_category": "cpu",
            "fault_type": "cpu",
            "confidence": 0.7,
            "explanation": "legacy report",
        }
    )
    truth = GroundTruth.model_validate(
        {"component": "checkout", "fault_type": "cpu", "inject_time": None}
    )

    assert diagnosis.affected_component == "checkout"
    assert truth.affected_component == "checkout"
    assert "root_cause_component" not in diagnosis.model_dump()
    assert "component" not in truth.model_dump()
