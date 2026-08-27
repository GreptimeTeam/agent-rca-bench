from semantic_rca_bench.contracts import (
    AgentRun,
    AgentUsage,
    Diagnosis,
    FaultCategory,
    GroundTruth,
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
            root_cause_component="checkoutservice",
            fault_category=fault_category,
            fault_type=fault_type,
            confidence=0.7,
            explanation="test diagnosis",
        ),
        tool_calls=[],
        usage=AgentUsage(),
        elapsed_seconds=1,
        responses=[],
    )


def test_causal_fault_category_takes_precedence_over_latency_symptom() -> None:
    result = evaluate(
        _run(FaultCategory.CPU, "CPU saturation causing latency degradation"),
        GroundTruth(component="checkoutservice", fault_type="delay", inject_time=0),
    )

    assert result.component_match
    assert result.predicted_fault_category == "cpu"
    assert result.expected_fault_category == "delay"
    assert not result.fault_type_match
    assert not result.fault_category_match
    assert not result.joint_match


def test_delay_mechanism_matches_delay_ground_truth() -> None:
    result = evaluate(
        _run(FaultCategory.DELAY, "delay"),
        GroundTruth(component="checkoutservice", fault_type="delay", inject_time=0),
    )

    assert result.predicted_fault_category == "delay"
    assert result.fault_type_match
    assert result.fault_category_match
    assert result.joint_match


def test_exact_fault_type_is_independent_of_broad_category() -> None:
    result = evaluate(
        _run(FaultCategory.OTHER, "rateLimiting"),
        GroundTruth(
            component="checkoutservice",
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
    assert not component_matches("checkoutservice dependency", "checkoutservice")
    assert not component_matches("frontend/checkoutservice", "checkoutservice")
