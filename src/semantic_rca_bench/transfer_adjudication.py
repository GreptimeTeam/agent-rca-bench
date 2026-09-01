from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from semantic_rca_bench.contracts import AgentRun
from semantic_rca_bench.datasets.openrca2_transfer import (
    TransferCaseSpec,
    TransferSelectionFixture,
)
from semantic_rca_bench.evidence import is_valid_evidence_trace
from semantic_rca_bench.transfer_formal import canonical_sha256
from semantic_rca_bench.transfer_protocol import TransferProtocolFixture
from semantic_rca_bench.transfer_scorer import (
    TransferEvaluation,
    evaluate_transfer_run,
)

JUDGE_MODELS = ("claude-sonnet-5", "deepseek-v4-flash")


class SemanticJudgeDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_sha256: str
    judge_model: Literal["claude-sonnet-5", "deepseek-v4-flash"]
    evidence_sufficient: bool
    supporting_evidence_ordinals: tuple[int, ...] = ()
    rationale: str
    failed_requirements: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_support(self) -> SemanticJudgeDecision:
        if self.evidence_sufficient != bool(self.supporting_evidence_ordinals):
            raise ValueError("sufficient judge decisions require supporting evidence ordinals")
        if any(ordinal < 1 for ordinal in self.supporting_evidence_ordinals):
            raise ValueError("supporting evidence ordinals must be positive")
        return self


class HumanAdjudicationDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_sha256: str
    evidence_sufficient: bool
    supporting_evidence_ordinals: tuple[int, ...] = ()
    rationale: str

    @model_validator(mode="after")
    def validate_support(self) -> HumanAdjudicationDecision:
        if self.evidence_sufficient != bool(self.supporting_evidence_ordinals):
            raise ValueError("sufficient human decisions require supporting evidence ordinals")
        if any(ordinal < 1 for ordinal in self.supporting_evidence_ordinals):
            raise ValueError("supporting evidence ordinals must be positive")
        return self


def build_semantic_adjudication_queue(
    report: Mapping[str, object],
    protocol: TransferProtocolFixture,
    selection: TransferSelectionFixture,
) -> dict[str, object]:
    cases = {case.opaque_case_id: case for case in selection.selected_cases}
    models = {model.model: model for model in protocol.models}
    candidates = []
    for item in _mapping_list(report, "runs"):
        case = cases.get(str(item.get("case_id")))
        model = models.get(str(item.get("model")))
        if case is None or model is None:
            raise ValueError("adjudication input is outside the bound protocol")
        run = AgentRun.model_validate(item.get("run"))
        evaluation = evaluate_transfer_run(
            run,
            case,
            expected_model=model.model,
            expected_transport=model.api_transport,
            expected_reasoning_effort=model.reasoning_effort,
            expected_max_output_tokens=model.max_output_tokens,
            max_tool_calls=protocol.max_tool_calls,
        )
        if not evaluation.semantic_adjudication_required:
            continue
        packet = _blind_packet(run, case)
        private_binding = {
            "cell_index": item.get("cell_index"),
            "case_id": item.get("case_id"),
        }
        candidates.append(
            {
                "candidate_sha256": canonical_sha256(
                    {"private_binding": private_binding, "judge_input": packet}
                ),
                "private_binding": private_binding,
                "deterministic_context": {
                    "rejection_codes": evaluation.semantic_adjudication_reason_codes,
                },
                "judge_input": packet,
            }
        )
    candidates.sort(key=lambda item: str(item["candidate_sha256"]))
    return {
        "schema_version": 1,
        "publication_status": "private adjudication queue; contains raw telemetry rows",
        "policy": {
            "trigger": (
                "diagnosis-correct, execution-reliable run with unique execution-valid "
                "citations but incomplete deterministic claim grounding"
            ),
            "judge_visibility": (
                "model, explicit treatment label, run order, and aggregate benchmark outcomes "
                "omitted; treatment remains inferable from tools and queries; deterministic "
                "verdicts are omitted to avoid anchoring; the cited query surface remains visible "
                "because it is evidence provenance"
            ),
            "hard_gates_remain_deterministic": True,
            "decision_rule": (
                "two independent model-family judges must both find the cited results sufficient; "
                "disagreement requires a human decision under the same rubric"
            ),
            "judge_models": list(JUDGE_MODELS),
        },
        "candidates": candidates,
    }


def resolve_semantic_adjudication(
    queue: Mapping[str, object],
    judge_decisions: list[SemanticJudgeDecision],
    human_decisions: list[HumanAdjudicationDecision] | None = None,
) -> dict[str, object]:
    candidate_items = _mapping_list(queue, "candidates")
    candidate_hashes = [str(candidate.get("candidate_sha256")) for candidate in candidate_items]
    if len(set(candidate_hashes)) != len(candidate_hashes):
        raise ValueError("adjudication queue contains duplicate candidate hashes")
    candidates = dict(zip(candidate_hashes, candidate_items, strict=True))
    decisions_by_candidate: dict[str, list[SemanticJudgeDecision]] = {}
    for decision in judge_decisions:
        if decision.candidate_sha256 not in candidates:
            raise ValueError("judge decision does not match an adjudication candidate")
        decisions_by_candidate.setdefault(decision.candidate_sha256, []).append(decision)
    humans = {decision.candidate_sha256: decision for decision in human_decisions or []}
    if len(humans) != len(human_decisions or []):
        raise ValueError("human adjudication contains duplicate candidates")
    if set(humans) - set(candidates):
        raise ValueError("human decision does not match an adjudication candidate")
    resolutions = []
    for candidate_sha256, candidate in candidates.items():
        decisions = decisions_by_candidate.get(candidate_sha256, [])
        if len(decisions) != len(JUDGE_MODELS) or {
            decision.judge_model for decision in decisions
        } != set(JUDGE_MODELS):
            raise ValueError("candidate lacks the frozen independent judge decisions")
        observed = {decision.evidence_sufficient for decision in decisions}
        human = humans.get(candidate_sha256)
        if len(observed) == 1:
            if human is not None:
                raise ValueError("human decision is allowed only for judge disagreement")
            sufficient = observed.pop()
            basis = "unanimous-judge-sufficient" if sufficient else "unanimous-judge-insufficient"
            supporting_ordinals = tuple(
                sorted(
                    {
                        ordinal
                        for decision in decisions
                        for ordinal in decision.supporting_evidence_ordinals
                    }
                )
            )
        else:
            if human is None:
                raise ValueError("judge disagreement requires human adjudication")
            sufficient = human.evidence_sufficient
            basis = "human-tiebreak"
            supporting_ordinals = human.supporting_evidence_ordinals
        judge_input = _mapping(candidate, "judge_input")
        evidence_count = len(_mapping_list(judge_input, "evidence"))
        if any(ordinal > evidence_count for ordinal in supporting_ordinals):
            raise ValueError("adjudication cites an unknown evidence ordinal")
        resolutions.append(
            {
                "candidate_sha256": candidate_sha256,
                "private_binding": dict(_mapping(candidate, "private_binding")),
                "evidence_sufficient": sufficient,
                "supporting_evidence_ordinals": list(supporting_ordinals),
                "basis": basis,
                "judge_decisions": [
                    decision.model_dump(mode="json")
                    for decision in sorted(decisions, key=lambda item: item.judge_model)
                ],
                "human_decision": human.model_dump(mode="json") if human is not None else None,
            }
        )
    return {
        "schema_version": 1,
        "queue_sha256": canonical_sha256(queue),
        "resolutions": resolutions,
    }


def validate_semantic_adjudication_resolution(
    queue: Mapping[str, object],
    resolution: Mapping[str, object],
) -> dict[str, object]:
    if resolution.get("schema_version") != 1:
        raise ValueError("unsupported semantic adjudication resolution")
    decisions: list[SemanticJudgeDecision] = []
    humans: list[HumanAdjudicationDecision] = []
    for item in _mapping_list(resolution, "resolutions"):
        decisions.extend(
            SemanticJudgeDecision.model_validate(decision)
            for decision in _mapping_list(item, "judge_decisions")
        )
        human = item.get("human_decision")
        if human is not None:
            if not isinstance(human, Mapping):
                raise ValueError("human adjudication decision must be an object")
            humans.append(HumanAdjudicationDecision.model_validate(human))
    expected = resolve_semantic_adjudication(queue, decisions, humans)
    if dict(resolution) != expected:
        raise ValueError("semantic adjudication resolution does not replay")
    return expected


def apply_semantic_adjudication(
    run: AgentRun,
    evaluation: TransferEvaluation,
    *,
    evidence_sufficient: bool,
    supporting_evidence_ordinals: list[int] | tuple[int, ...] = (),
) -> TransferEvaluation:
    if not evaluation.semantic_adjudication_required or not evidence_sufficient:
        return evaluation
    diagnosis = run.diagnosis
    if diagnosis is None:
        raise ValueError("adjudication candidate has no diagnosis")
    if not supporting_evidence_ordinals:
        raise ValueError("accepted adjudication has no supporting evidence")
    if any(
        ordinal < 1 or ordinal > len(diagnosis.evidence) for ordinal in supporting_evidence_ordinals
    ):
        raise ValueError("accepted adjudication cites an unknown evidence ordinal")
    cited_ids = list(
        dict.fromkeys(
            diagnosis.evidence[ordinal - 1].query_id for ordinal in supporting_evidence_ordinals
        )
    )
    trace_indexes = {
        trace.query_id: index
        for index, trace in enumerate(run.tool_calls)
        if trace.query_id is not None
    }
    support_indexes = [
        trace_indexes[query_id] for query_id in cited_ids if query_id in trace_indexes
    ]
    if not support_indexes:
        raise ValueError("accepted adjudication has no cited execution")
    support_index = max(support_indexes)
    calls = run.tool_calls[: support_index + 1]
    if not all(trace.database_load is not None for trace in calls):
        raise ValueError("accepted adjudication lacks database-load accounting")
    rows = sum(trace.database_load.rows_returned for trace in calls if trace.database_load)
    return evaluation.model_copy(
        update={
            "required_evidence_covered": True,
            "efficiency_eligible": True,
            "auditable_completion": True,
            "success": True,
            "supporting_evidence_query_ids": cited_ids,
            "failure_reasons": [],
            "semantic_adjudication_required": False,
            "correct_completion_tool_calls": len(run.tool_calls),
            "tool_calls_through_required_evidence": support_index + 1,
            "rows_returned_through_required_evidence": rows,
        }
    )


def _blind_packet(
    run: AgentRun,
    case: TransferCaseSpec,
) -> dict[str, object]:
    diagnosis = run.diagnosis
    if diagnosis is None:
        raise ValueError("adjudication candidate has no diagnosis")
    traces_by_query_id = {
        trace.query_id: trace
        for trace in run.tool_calls
        if trace.query_id is not None and is_valid_evidence_trace([trace])
    }
    evidence = []
    for ordinal, citation in enumerate(diagnosis.evidence, start=1):
        trace = traces_by_query_id.get(citation.query_id)
        if trace is None:
            continue
        evidence.append(
            {
                "evidence_ordinal": ordinal,
                "claim_types": [claim.value for claim in citation.claim_types],
                "tool_name": trace.tool_name,
                "tool_input": trace.input,
                "tool_result": trace.output,
            }
        )
    return {
        "required_claim": {
            "causal_scope": case.causal_scope.value,
            "causal_component": case.causal_component,
            "edge_source": case.edge_source,
            "edge_destination": case.edge_destination,
            "fault_category": case.fault_category.value,
            "mechanism_code": case.mechanism_code.value,
            "allowed_operations": list(case.mechanism_evidence.allowed_operations),
            "normal_window": list(case.normal_window),
            "abnormal_window": list(case.abnormal_window),
            "threshold": case.mechanism_evidence.threshold,
            "minimum_anomalous_observations": (
                case.mechanism_evidence.minimum_anomalous_observations
            ),
        },
        "submitted_diagnosis": {
            "causal_scope": diagnosis.causal_scope.value,
            "causal_component": diagnosis.causal_component,
            "edge_source": diagnosis.edge_source,
            "edge_destination": diagnosis.edge_destination,
            "fault_category": diagnosis.fault_category.value,
            "mechanism_code": diagnosis.mechanism_code.value,
            "causal_operation": diagnosis.causal_operation,
        },
        "evidence": evidence,
    }


def _mapping_list(value: Mapping[str, object], key: str) -> list[Mapping[str, object]]:
    items = value.get(key)
    if not isinstance(items, list) or not all(isinstance(item, Mapping) for item in items):
        raise ValueError(f"{key} must be a list of objects")
    return items


def _mapping(value: Mapping[str, object], key: str) -> Mapping[str, object]:
    item = value.get(key)
    if not isinstance(item, Mapping):
        raise ValueError(f"{key} must be an object")
    return item
