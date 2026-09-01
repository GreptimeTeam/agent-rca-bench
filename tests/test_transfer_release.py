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

from semantic_rca_bench.contracts import (
    ApiTransport,
    DatabaseLoad,
    Evidence,
    EvidenceClaimType,
    ToolTrace,
)
from semantic_rca_bench.transfer_adjudication import apply_semantic_adjudication
from semantic_rca_bench.transfer_release import (
    _apply_holm,
    _median_or_none,
    _model_reports,
    _public_adjudicated_sensitivity,
    _public_adjudication_resolution,
    _sign_test,
    _validate_public_adjudication,
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


def test_sanitizer_does_not_treat_table_metadata_as_query_rows() -> None:
    case = _case(6)
    run = _run(case, _delay_query(case), _delay_result(case))
    metadata = ToolTrace(
        tool_name="describe_table",
        input={"table": "traces"},
        output={"table": "traces", "columns": [{"name": "trace_id"}]},
    )
    run = run.model_copy(
        update={
            "tool_calls": [metadata, *run.tool_calls],
            "tool_calls_requested": 2,
        }
    )
    payload = sanitize_transfer_run(run, _evaluate(run, case), case)

    assert payload["tool_calls"][0]["result"] is None
    _validate(payload)


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


def test_public_adjudication_replays_without_overwriting_deterministic_score() -> None:
    case = _case(7)
    query = _metric_query(case).replace(
        "GROUP BY phase",
        "AND greptime_value >= 0.5 GROUP BY phase",
    )
    run = _run(case, query, _metric_result(case))
    deterministic = _evaluate(run, case)
    assert deterministic.semantic_adjudication_required is True
    effective = apply_semantic_adjudication(
        run,
        deterministic,
        evidence_sufficient=True,
        supporting_evidence_ordinals=[1],
    )
    payload = sanitize_transfer_run(run, deterministic, case)
    candidate = "a" * 64
    payload["adjudication"] = {
        "candidate_sha256": candidate,
        "evidence_sufficient": True,
        "supporting_evidence_ordinals": [1],
        "basis": "unanimous-judge-sufficient",
        "judge_decisions": [
            {
                "candidate_sha256": candidate,
                "judge_model": model,
                "evidence_sufficient": True,
                "supporting_evidence_ordinals": [1],
                "rationale": "The cited result is sufficient.",
                "failed_requirements": [],
            }
            for model in ("claude-sonnet-5", "deepseek-v4-flash")
        ],
        "human_decision": None,
        "status": "accepted",
    }
    payload["adjudicated_sensitivity"] = _public_adjudicated_sensitivity(
        effective,
        payload["citations"],
        supporting_evidence_ordinals=[1],
    )

    public_deterministic = _validate(payload, index=7)
    _validate_public_adjudication(payload, public_deterministic)
    assert public_deterministic["efficiency_eligible"] is True
    assert public_deterministic["required_evidence_covered"] is False
    assert payload["adjudicated_sensitivity"]["efficiency_eligible"] is True
    for key in (
        "causal_locus_evidence_match",
        "baseline_evidence_match",
        "anomaly_evidence_match",
        "mechanism_evidence_match",
        "claim_grounding",
    ):
        assert key not in payload["adjudicated_sensitivity"]

    payload["adjudicated_sensitivity"]["rows_returned_through_required_evidence"] += 1
    with pytest.raises(ValueError, match="does not replay"):
        _validate_public_adjudication(payload, public_deterministic)


def test_public_adjudication_redacts_free_text() -> None:
    resolution = {
        "schema_version": 1,
        "queue_sha256": "q" * 64,
        "resolutions": [
            {
                "candidate_sha256": "a" * 64,
                "private_binding": {"cell_index": 1, "case_id": "case"},
                "evidence_sufficient": False,
                "supporting_evidence_ordinals": [],
                "basis": "human-tiebreak",
                "judge_decisions": [
                    {
                        "candidate_sha256": "a" * 64,
                        "judge_model": "claude-sonnet-5",
                        "evidence_sufficient": False,
                        "supporting_evidence_ordinals": [],
                        "rationale": "Raw telemetry value was 1981.",
                        "failed_requirements": ["Raw telemetry value was 1981."],
                    }
                ],
                "human_decision": {
                    "candidate_sha256": "a" * 64,
                    "evidence_sufficient": False,
                    "supporting_evidence_ordinals": [],
                    "rationale": "Raw telemetry value was 1981.",
                },
            }
        ],
    }

    public = _public_adjudication_resolution(resolution)
    encoded = str(public)

    assert "1981" not in encoded
    assert public["resolutions"][0]["judge_decisions"][0]["failed_requirements"] == []


def test_primary_metrics_do_not_depend_on_evidence_adjudication() -> None:
    case = _case(7)
    query = _metric_query(case).replace(
        "GROUP BY phase",
        "AND greptime_value >= 0.5 GROUP BY phase",
    )
    run = _run(case, query, _metric_result(case))
    deterministic = _evaluate(run, case)
    effective = apply_semantic_adjudication(
        run,
        deterministic,
        evidence_sufficient=True,
        supporting_evidence_ordinals=[1],
    )
    load = DatabaseLoad(query_count=1, rows_returned=2)
    public = sanitize_transfer_run(run, deterministic, case, load)
    public["adjudicated_sensitivity"] = _public_adjudicated_sensitivity(
        effective,
        public["citations"],
        supporting_evidence_ordinals=[1],
    )
    items = [
        {
            "case_id": case.opaque_case_id,
            "model": "test-model",
            "repetition": 0,
            "visibility": visibility,
            "run": {**copy.deepcopy(public), "visibility": visibility},
        }
        for visibility in ("raw", "semantic_graph")
    ]

    report = _model_reports(items, ["test-model"], family_size=2)["test-model"]

    assert report["primary_metrics"]["rows_returned"]["eligible_cases"] == 1
    assert report["evidence_quality"]["paired_disposition"]["neither"] == 1


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
