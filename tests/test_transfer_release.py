from __future__ import annotations

import copy

import pytest
from test_transfer_scorer import (
    _case,
    _delay_query,
    _delay_result,
    _evaluate,
    _metric_query,
    _metric_result,
    _run,
)

from semantic_rca_bench.contracts import ApiTransport, Evidence, EvidenceClaimType, ToolTrace
from semantic_rca_bench.transfer_release import (
    _apply_holm,
    _median_or_none,
    _sign_test,
    sanitize_transfer_run,
    validate_public_transfer_run,
)


def _validate(payload: dict[str, object], index: int = 6) -> dict[str, object]:
    return validate_public_transfer_run(
        payload,
        _case(index),
        expected_model="test-model",
        expected_transport=ApiTransport.OPENAI_RESPONSES,
        expected_reasoning_effort="high",
        expected_max_output_tokens=16384,
        max_tool_calls=48,
    )


def test_sanitized_run_deterministically_rescores_without_raw_rows() -> None:
    case = _case(6)
    run = _run(case, _delay_query(case), _delay_result(case))
    evaluation = _evaluate(run, case)

    payload = sanitize_transfer_run(run, evaluation, case)
    public_evaluation = _validate(payload)

    assert public_evaluation["success"] is True
    citation = payload["citations"][0]
    assert citation["resolution_status"] == "unique"
    assert citation["mechanism_verdicts"]
    assert citation["mechanism_verdicts"][0]["baseline_clear"] is True
    assert citation["mechanism_verdicts"][0]["anomaly_present"] is True
    assert citation["supports_baseline_clear"] is True
    assert citation["supports_anomaly_present"] is True
    assert payload["evaluation"]["baseline_evidence_ordinals"] == [1]
    assert payload["evaluation"]["anomaly_evidence_ordinals"] == [1]
    summary = citation["query_summaries"][0]
    assert summary["result"]["row_count"] == 3
    assert "rows" not in summary["result"]
    assert "query_id" not in str(payload)


def test_sanitizer_preserves_rejected_citation_without_invalidating_efficiency() -> None:
    case = _case(6)
    run = _run(case, _delay_query(case), _delay_result(case))
    assert run.diagnosis is not None
    bad_trace = ToolTrace(
        tool_name="execute_sql",
        input={"query": "SELECT * FROM traces LIMIT 1"},
        query_id="q2",
        output={
            "query_id": "q2",
            "columns": ["service_name"],
            "rows": [["shipping"]],
            "elapsed_seconds": 0,
            "truncated": True,
        },
    )
    run = run.model_copy(
        update={
            "diagnosis": run.diagnosis.model_copy(
                update={
                    "evidence": [
                        *run.diagnosis.evidence,
                        Evidence(
                            query_id="q2",
                            claim="This extra query was truncated.",
                            claim_types=[EvidenceClaimType.EXCLUSION],
                        ),
                    ]
                }
            ),
            "tool_calls": [*run.tool_calls, bad_trace],
            "tool_calls_requested": 2,
        }
    )
    evaluation = _evaluate(run, case)

    payload = sanitize_transfer_run(run, evaluation, case)
    public_evaluation = _validate(payload)

    assert evaluation.efficiency_eligible is True
    assert public_evaluation["efficiency_eligible"] is True
    assert public_evaluation["auditable_completion"] is False
    rejected = payload["citations"][1]
    assert rejected["execution_valid"] is False
    assert rejected["query_summaries"][0]["input"] == bad_trace.input
    assert rejected["rejection_reasons"]


def test_sanitizer_preserves_per_claim_rejection_reason() -> None:
    case = _case(7)
    query = _metric_query(case).replace(
        "GROUP BY phase",
        "AND greptime_value >= 0.5 GROUP BY phase",
    )
    run = _run(case, query, _metric_result(case))

    payload = sanitize_transfer_run(run, _evaluate(run, case), case)

    verdict = payload["citations"][0]["mechanism_verdicts"][0]
    assert verdict["baseline_clear"] is False
    assert verdict["anomaly_present"] is True
    assert verdict["baseline_rejection_codes"] == ["value_filtered_for_universal_claim"]


def test_ambiguous_citation_is_explicit_and_fails_closed() -> None:
    case = _case(6)
    run = _run(case, _delay_query(case), _delay_result(case))
    run = run.model_copy(update={"tool_calls": [*run.tool_calls, run.tool_calls[0]]})
    evaluation = _evaluate(run, case)

    payload = sanitize_transfer_run(run, evaluation, case)
    public_evaluation = _validate(payload)

    assert payload["citations"][0]["resolution_status"] == "ambiguous"
    assert len(payload["citations"][0]["query_summaries"]) == 2
    assert public_evaluation["efficiency_eligible"] is False


def test_public_validator_rejects_local_paths() -> None:
    case = _case(6)
    run = _run(case, _delay_query(case), _delay_result(case))
    evaluation = _evaluate(run, case)
    payload = sanitize_transfer_run(run, evaluation, case)
    payload["citations"][0]["query_summaries"][0]["input"]["query"] = (
        "SELECT '/Users/dennis/private'"
    )

    try:
        _validate(payload)
    except ValueError as error:
        assert "local or tenant-specific" in str(error)
    else:
        raise AssertionError("public sanitizer must reject local paths")


def test_public_validator_binds_citation_projection_to_the_tool_call() -> None:
    case = _case(6)
    run = _run(case, _delay_query(case), _delay_result(case))
    payload = sanitize_transfer_run(run, _evaluate(run, case), case)
    payload["citations"][0]["mechanism_verdicts"][0]["anomaly_present"] = False

    with pytest.raises(ValueError, match="mechanism verdicts drifted"):
        _validate(payload)


def test_public_validator_binds_claim_atom_annotations_to_the_verdict() -> None:
    case = _case(6)
    run = _run(case, _delay_query(case), _delay_result(case))
    payload = sanitize_transfer_run(run, _evaluate(run, case), case)
    payload["citations"][0]["supports_baseline_clear"] = False

    with pytest.raises(ValueError, match="baseline support annotation drifted"):
        _validate(payload)


def test_public_validator_recomputes_primary_efficiency_fields() -> None:
    case = _case(6)
    run = _run(case, _delay_query(case), _delay_result(case))
    payload = sanitize_transfer_run(run, _evaluate(run, case), case)
    tampered = copy.deepcopy(payload)
    tampered["evaluation"]["correct_completion_tool_calls"] = 99

    with pytest.raises(ValueError, match="does not deterministically rescore"):
        _validate(tampered)


def test_case_median_does_not_treat_repetitions_as_independent_cases() -> None:
    assert _median_or_none([-10, 2]) == -4
    assert _median_or_none([-4, -3, -2]) == -3
    assert _median_or_none([]) is None


def test_sign_test_excludes_ties_and_reports_unestimable_all_ties() -> None:
    assert _sign_test([-1, 0, 1]) == 1
    assert _sign_test([0, 0]) is None
    assert _sign_test([-3, -2, 0]) == 0.5


def test_holm_adjustment_uses_the_fixed_twelve_hypothesis_family() -> None:
    reports = {
        "model-a": {
            "primary_metrics": {
                "rows_returned": {"holm_adjusted_p": None},
                "correct_completion_tool_calls": {"holm_adjusted_p": None},
            }
        },
        "model-b": {
            "primary_metrics": {
                "rows_returned": {"holm_adjusted_p": None},
                "correct_completion_tool_calls": {"holm_adjusted_p": None},
            }
        },
    }

    _apply_holm(
        reports,
        [
            ("model-a", "rows_returned", 0.01),
            ("model-b", "correct_completion_tool_calls", 0.02),
        ],
        family_size=12,
    )

    assert reports["model-a"]["primary_metrics"]["rows_returned"]["holm_adjusted_p"] == 0.12
    assert (
        reports["model-b"]["primary_metrics"]["correct_completion_tool_calls"]["holm_adjusted_p"]
        == 0.22
    )
    assert (
        reports["model-a"]["primary_metrics"]["correct_completion_tool_calls"]["holm_adjusted_p"]
        is None
    )
