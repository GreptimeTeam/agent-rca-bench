import pytest

from semantic_rca_bench.contracts import (
    AgentRun,
    AgentRunner,
    AgentUsage,
    DatabaseLoad,
    Diagnosis,
    Evidence,
    EvidenceClaimType,
    QueryResult,
    ToolTrace,
    Visibility,
)
from semantic_rca_bench.transfer_adjudication import (
    HumanAdjudicationDecision,
    SemanticJudgeDecision,
    apply_semantic_adjudication,
    build_semantic_adjudication_queue,
    resolve_semantic_adjudication,
)
from semantic_rca_bench.transfer_protocol import load_transfer_protocol
from semantic_rca_bench.transfer_scorer import evaluate_transfer_run


def test_adjudication_queue_preserves_evidence_but_blinds_run_identity() -> None:
    protocol, selection = load_transfer_protocol()
    case = selection.selected_cases[0]
    model = protocol.models[0]
    result = QueryResult(
        query_id="q1",
        columns=["phase", "maximum"],
        rows=[["normal", 0], ["abnormal", 6]],
        elapsed_seconds=0,
    )
    diagnosis = Diagnosis(
        causal_scope=case.causal_scope,
        causal_component=case.causal_component,
        fault_category=case.fault_category,
        mechanism_code=case.mechanism_code,
        fault_type=case.source_fault_type,
        confidence=1,
        evidence=[
            Evidence(
                query_id="q1",
                claim="The result supports the submitted diagnosis.",
                claim_types=[
                    EvidenceClaimType.CAUSAL_LOCUS,
                    EvidenceClaimType.FAULT_MECHANISM,
                ],
            )
        ],
        explanation="The cited result contains the observed transition.",
    )
    run = AgentRun(
        run_id="private-run-id",
        visibility=Visibility.RAW,
        model=model.model,
        runner=AgentRunner.API,
        api_transport=model.api_transport,
        reasoning_effort=model.reasoning_effort,
        max_output_tokens=model.max_output_tokens,
        diagnosis=diagnosis,
        tool_calls=[
            ToolTrace(
                tool_name="execute_sql",
                input={"query": "SELECT phase, maximum FROM unrelated_metric"},
                query_id="q1",
                output=result.model_dump(mode="json"),
                database_load=DatabaseLoad(query_count=1, rows_returned=2),
            )
        ],
        tool_calls_requested=1,
        usage=AgentUsage(),
        elapsed_seconds=0,
        responses=[],
    )
    report = {
        "runs": [
            {
                "cell_index": 7,
                "case_id": case.opaque_case_id,
                "model": model.model,
                "run": run.model_dump(mode="json"),
            }
        ]
    }

    queue = build_semantic_adjudication_queue(report, protocol, selection)

    assert queue["publication_status"] == (
        "private adjudication queue; contains raw telemetry rows"
    )
    assert len(queue["candidates"]) == 1
    candidate = queue["candidates"][0]
    assert candidate["private_binding"] == {
        "cell_index": 7,
        "case_id": case.opaque_case_id,
    }
    judge_input = candidate["judge_input"]
    assert not {"model", "visibility", "treatment", "cell_index", "case_id"} & set(judge_input)
    assert "deterministic_rejection_codes" not in judge_input
    assert "deterministic_verdict" not in judge_input["evidence"][0]
    assert candidate["deterministic_context"]["rejection_codes"]
    assert judge_input["evidence"][0]["tool_result"]["rows"] == result.rows


def test_adjudication_queue_excludes_incorrect_diagnosis() -> None:
    protocol, selection = load_transfer_protocol()
    case = selection.selected_cases[0]
    model = protocol.models[0]
    run = AgentRun(
        run_id="wrong-diagnosis",
        visibility=Visibility.RAW,
        model=model.model,
        runner=AgentRunner.API,
        api_transport=model.api_transport,
        reasoning_effort=model.reasoning_effort,
        max_output_tokens=model.max_output_tokens,
        diagnosis=None,
        tool_calls=[],
        tool_calls_requested=0,
        usage=AgentUsage(),
        elapsed_seconds=0,
        responses=[],
    )
    report = {
        "runs": [
            {
                "cell_index": 8,
                "case_id": case.opaque_case_id,
                "model": model.model,
                "run": run.model_dump(mode="json"),
            }
        ]
    }

    assert build_semantic_adjudication_queue(report, protocol, selection)["candidates"] == []


def test_adjudication_requires_two_judges_and_human_tiebreak() -> None:
    queue = {
        "candidates": [
            {
                "candidate_sha256": "a" * 64,
                "private_binding": {"cell_index": 1, "case_id": "case"},
                "judge_input": {"evidence": [{}]},
            }
        ]
    }
    decisions = [
        SemanticJudgeDecision(
            candidate_sha256="a" * 64,
            judge_model="claude-sonnet-5",
            evidence_sufficient=True,
            supporting_evidence_ordinals=(1,),
            rationale="The cited results cover the claim.",
        ),
        SemanticJudgeDecision(
            candidate_sha256="a" * 64,
            judge_model="deepseek-v4-flash",
            evidence_sufficient=False,
            rationale="The edge lineage is incomplete.",
            failed_requirements=("edge_lineage",),
        ),
    ]

    with pytest.raises(ValueError, match="requires human adjudication"):
        resolve_semantic_adjudication(queue, decisions)

    resolved = resolve_semantic_adjudication(
        queue,
        decisions,
        [
            HumanAdjudicationDecision(
                candidate_sha256="a" * 64,
                evidence_sufficient=False,
                rationale="No cited result proves the directed parent-child edge.",
            )
        ],
    )

    assert resolved["resolutions"][0]["evidence_sufficient"] is False
    assert resolved["resolutions"][0]["basis"] == "human-tiebreak"


def test_adjudication_rejects_malformed_queue_as_value_error() -> None:
    queue = {
        "candidates": [
            {
                "candidate_sha256": "a" * 64,
                "private_binding": {"cell_index": 1, "case_id": "case"},
                "judge_input": {},
            }
        ]
    }
    decisions = [
        SemanticJudgeDecision(
            candidate_sha256="a" * 64,
            judge_model=model,
            evidence_sufficient=False,
            rationale="The evidence is insufficient.",
        )
        for model in ("claude-sonnet-5", "deepseek-v4-flash")
    ]

    with pytest.raises(ValueError, match="evidence must be a list"):
        resolve_semantic_adjudication(queue, decisions)


def test_adjudication_rejects_duplicate_candidate_hashes() -> None:
    candidate = {
        "candidate_sha256": "a" * 64,
        "private_binding": {"cell_index": 1, "case_id": "case"},
        "judge_input": {"evidence": []},
    }

    with pytest.raises(ValueError, match="duplicate candidate hashes"):
        resolve_semantic_adjudication({"candidates": [candidate, candidate]}, [])


def test_adjudication_rejects_unknown_human_before_resolving_candidates() -> None:
    queue = {"candidates": []}
    human = HumanAdjudicationDecision(
        candidate_sha256="a" * 64,
        evidence_sufficient=False,
        rationale="Unknown candidate.",
    )

    with pytest.raises(ValueError, match="does not match"):
        resolve_semantic_adjudication(queue, [], [human])


def test_accepted_adjudication_changes_only_semantic_grounding() -> None:
    protocol, selection = load_transfer_protocol()
    case = selection.selected_cases[0]
    model = protocol.models[0]
    result = QueryResult(
        query_id="q1",
        columns=["phase", "maximum"],
        rows=[["normal", 0], ["abnormal", 6]],
        elapsed_seconds=0,
    )
    diagnosis = Diagnosis(
        causal_scope=case.causal_scope,
        causal_component=case.causal_component,
        fault_category=case.fault_category,
        mechanism_code=case.mechanism_code,
        fault_type=case.source_fault_type,
        confidence=1,
        evidence=[
            Evidence(
                query_id="q1",
                claim="The result supports the mechanism.",
                claim_types=[EvidenceClaimType.FAULT_MECHANISM],
            )
        ],
        explanation="The cited result supports the diagnosis.",
    )
    run = AgentRun(
        run_id="candidate",
        visibility=Visibility.RAW,
        model=model.model,
        runner=AgentRunner.API,
        api_transport=model.api_transport,
        reasoning_effort=model.reasoning_effort,
        max_output_tokens=model.max_output_tokens,
        diagnosis=diagnosis,
        tool_calls=[
            ToolTrace(
                tool_name="execute_sql",
                input={"query": "SELECT phase, maximum FROM unrelated_metric"},
                query_id="q1",
                output=result.model_dump(mode="json"),
                database_load=DatabaseLoad(query_count=1, rows_returned=2),
            )
        ],
        tool_calls_requested=1,
        usage=AgentUsage(),
        elapsed_seconds=0,
        responses=[],
    )
    deterministic = evaluate_transfer_run(
        run,
        case,
        expected_model=model.model,
        expected_transport=model.api_transport,
        expected_reasoning_effort=model.reasoning_effort,
        expected_max_output_tokens=model.max_output_tokens,
        max_tool_calls=protocol.max_tool_calls,
    )

    accepted = apply_semantic_adjudication(
        run,
        deterministic,
        evidence_sufficient=True,
        supporting_evidence_ordinals=[1],
    )

    assert deterministic.semantic_adjudication_required is True
    assert deterministic.efficiency_eligible is True
    assert deterministic.required_evidence_covered is False
    assert accepted.diagnosis_correct is True
    assert accepted.citations_execution_valid is True
    assert accepted.execution_reliability is True
    assert accepted.required_evidence_covered is True
    assert accepted.efficiency_eligible is True
    assert accepted.required_evidence_covered is True
    assert accepted.correct_completion_tool_calls == 1
    assert accepted.causal_locus_evidence_match is deterministic.causal_locus_evidence_match
    assert accepted.baseline_evidence_match is deterministic.baseline_evidence_match
    assert accepted.anomaly_evidence_match is deterministic.anomaly_evidence_match
    assert accepted.mechanism_evidence_match is deterministic.mechanism_evidence_match
    assert accepted.claim_grounding == deterministic.claim_grounding
