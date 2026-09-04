from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from statistics import median

from agent_rca_bench.contracts import (
    AgentRun,
    ApiTransport,
    CausalScope,
    DatabaseLoad,
    EvidenceClaimType,
    ToolTrace,
)
from agent_rca_bench.datasets.openrca2_transfer import (
    SourceMechanismEvidence,
    TransferCaseSpec,
)
from agent_rca_bench.evaluation import component_matches
from agent_rca_bench.evidence import (
    NATIVE_EVIDENCE_OPERATIONS,
    is_evidence_sql,
    is_valid_evidence_trace,
)
from agent_rca_bench.report import _estimated_api_cost, _raw_input_breakdown
from agent_rca_bench.transfer_adjudication import (
    JUDGE_MODELS,
    HumanAdjudicationDecision,
    SemanticJudgeDecision,
    apply_semantic_adjudication,
    build_semantic_adjudication_queue,
    validate_semantic_adjudication_resolution,
)
from agent_rca_bench.transfer_scorer import (
    GROUNDING_VERIFIER_TOOLS,
    ClaimVerdict,
    TransferEvaluation,
    _accepted_mechanism_codes,
    _mechanism_verdict_from_trace,
    _source_declared_identities,
)

ARTIFACT_SCHEMA_VERSION = 2


def sanitize_transfer_run(
    run: AgentRun,
    evaluation: TransferEvaluation,
    case: TransferCaseSpec,
    database_load: DatabaseLoad | None = None,
) -> dict[str, object]:
    traces_by_query_id: dict[str, list[ToolTrace]] = {}
    for trace in run.tool_calls:
        if trace.query_id is not None:
            traces_by_query_id.setdefault(trace.query_id, []).append(trace)
    evidence = run.diagnosis.evidence if run.diagnosis is not None else []
    evidence_ids_unique = len({entry.query_id for entry in evidence}) == len(evidence)
    reference_ordinals = {
        query_id: ordinal
        for ordinal, query_id in enumerate(
            dict.fromkeys(entry.query_id for entry in evidence),
            start=1,
        )
    }
    mechanism_ids = set(evaluation.mechanism_evidence_query_ids)
    locus_ids = set(evaluation.causal_locus_evidence_query_ids)
    baseline_ids = set(evaluation.baseline_evidence_query_ids)
    anomaly_ids = set(evaluation.anomaly_evidence_query_ids)
    citations = []
    tool_calls = [
        {
            "ordinal": ordinal,
            **_query_summary(trace),
            "database_load": (
                trace.database_load.model_dump(mode="json")
                if trace.database_load is not None
                else None
            ),
            "mechanism_verdict": (
                _mechanism_verdict_from_trace(trace, case).model_dump(mode="json")
                if trace.tool_name == "execute_sql"
                else None
            ),
            "causal_locus_projection": _causal_locus_projection(trace, case),
        }
        for ordinal, trace in enumerate(run.tool_calls, start=1)
    ]
    for ordinal, entry in enumerate(evidence, start=1):
        matches = traces_by_query_id.get(entry.query_id, [])
        resolution = {0: "missing", 1: "unique"}.get(len(matches), "ambiguous")
        verdicts = [
            _mechanism_verdict_from_trace(trace, case).model_dump(mode="json")
            for trace in matches
            if trace.tool_name == "execute_sql"
        ]
        locus_projections = [
            projection
            for trace in matches
            if (projection := _causal_locus_projection(trace, case)) is not None
        ]
        execution_valid = bool(entry.claim.strip()) and is_valid_evidence_trace(matches)
        rejection_reasons = []
        if not evidence_ids_unique:
            rejection_reasons.append("citation reference is duplicated")
        if resolution != "unique":
            rejection_reasons.append(f"citation resolution is {resolution}")
        elif not entry.claim.strip():
            rejection_reasons.append("citation claim is empty")
        elif not execution_valid:
            rejection_reasons.append("cited query failed, was truncated, or has inconsistent IDs")
        if (
            EvidenceClaimType.FAULT_MECHANISM in entry.claim_types
            and entry.query_id not in mechanism_ids
        ):
            rejection_reasons.append("query does not satisfy the mechanism evidence predicate")
        if EvidenceClaimType.CAUSAL_LOCUS in entry.claim_types and entry.query_id not in locus_ids:
            rejection_reasons.append("query does not establish the incident-local causal locus")
        citations.append(
            {
                "ordinal": ordinal,
                "reference_ordinal": reference_ordinals[entry.query_id],
                "claim_present": bool(entry.claim.strip()),
                "claim_types": [claim.value for claim in entry.claim_types],
                "resolution_status": resolution,
                "matching_tool_call_ordinals": [
                    index
                    for index, trace in enumerate(run.tool_calls, start=1)
                    if trace.query_id == entry.query_id
                ],
                "execution_valid": execution_valid,
                "supports_causal_locus": entry.query_id in locus_ids,
                "supports_baseline_clear": entry.query_id in baseline_ids,
                "supports_anomaly_present": entry.query_id in anomaly_ids,
                "supports_fault_mechanism": entry.query_id in mechanism_ids,
                "query_summaries": [_query_summary(trace) for trace in matches],
                "mechanism_verdicts": verdicts,
                "causal_locus_projections": locus_projections,
                "rejection_codes": list(
                    dict.fromkeys(
                        code for verdict in verdicts for code in verdict["rejection_codes"]
                    )
                ),
                "rejection_reasons": rejection_reasons,
            }
        )
    diagnosis = None
    if run.diagnosis is not None:
        diagnosis = {
            "causal_scope": run.diagnosis.causal_scope.value,
            "causal_component": run.diagnosis.causal_component,
            "edge_source": run.diagnosis.edge_source,
            "edge_destination": run.diagnosis.edge_destination,
            "impacted_component": run.diagnosis.impacted_component,
            "causal_operation": run.diagnosis.causal_operation,
            "fault_category": run.diagnosis.fault_category.value,
            "mechanism_code": run.diagnosis.mechanism_code.value,
            "onset_time": run.diagnosis.onset_time,
            "confidence": run.diagnosis.confidence,
        }
    return {
        "visibility": run.visibility.value,
        "model": run.model,
        "runner": run.runner.value,
        "api_transport": run.api_transport.value if run.api_transport is not None else None,
        "reasoning_effort": run.reasoning_effort,
        "max_output_tokens": run.max_output_tokens,
        "diagnosis": diagnosis,
        "tool_calls": tool_calls,
        "citations": citations,
        "citation_references_unique": evidence_ids_unique,
        "evaluation": _public_evaluation(evaluation, citations),
        "execution": {
            "tool_calls_executed": len(run.tool_calls),
            "tool_calls_requested": run.tool_calls_requested,
            "tool_budget_exhausted": run.tool_budget_exhausted,
            "runner_error": run.error is not None,
            "invalid_rejected_tool_calls": sum(
                item.reason_code == "invalid" for item in run.rejected_tool_calls
            ),
            "rejected_tool_calls": dict(
                sorted(Counter(item.reason_code for item in run.rejected_tool_calls).items())
            ),
        },
        "usage": {
            "provider_visible_input_tokens": run.usage.input_tokens,
            "output_tokens": run.usage.output_tokens,
            "reasoning_output_tokens": run.usage.reasoning_tokens,
        },
        "database_load": database_load.model_dump(mode="json") if database_load else None,
    }


def validate_public_transfer_run(
    payload: Mapping[str, object],
    case: TransferCaseSpec,
    *,
    expected_model: str,
    expected_transport: ApiTransport,
    expected_reasoning_effort: str | None,
    expected_max_output_tokens: int,
    max_tool_calls: int,
) -> dict[str, object]:
    _reject_private_fields(payload)
    citations = _mapping_list(payload, "citations")
    tool_calls = _mapping_list(payload, "tool_calls")
    if [call.get("ordinal") for call in tool_calls] != list(range(1, len(tool_calls) + 1)):
        raise ValueError("public transfer tool-call ordinals are not contiguous")
    for call in tool_calls:
        _validate_query_summary(call, allow_annotations=True)
    if [citation.get("ordinal") for citation in citations] != list(range(1, len(citations) + 1)):
        raise ValueError("public transfer citation ordinals are not contiguous")
    references = [citation.get("reference_ordinal") for citation in citations]
    if not all(
        isinstance(item, int) and not isinstance(item, bool) and item > 0 for item in references
    ):
        raise ValueError("public transfer citation references are malformed")
    references_unique = len(set(references)) == len(references)
    if payload.get("citation_references_unique") is not references_unique:
        raise ValueError("public transfer citation reference uniqueness drifted")
    for citation in citations:
        summaries = _mapping_list(citation, "query_summaries")
        ordinals = citation.get("matching_tool_call_ordinals")
        if not isinstance(ordinals, list) or not all(
            isinstance(item, int) and not isinstance(item, bool) and 1 <= item <= len(tool_calls)
            for item in ordinals
        ):
            raise ValueError("public transfer citation call ordinals are malformed")
        resolution = citation.get("resolution_status")
        expected_count = {"missing": 0, "unique": 1}.get(str(resolution))
        if expected_count is not None and len(summaries) != expected_count:
            raise ValueError("public transfer citation resolution is inconsistent")
        if resolution == "ambiguous" and len(summaries) < 2:
            raise ValueError("ambiguous public transfer citation lacks matching calls")
        if ordinals != [
            int(summary["ordinal"]) for summary in tool_calls if int(summary["ordinal"]) in ordinals
        ]:
            raise ValueError("public transfer citation call ordinals are inconsistent")
        comparable = [
            {
                key: value
                for key, value in tool_calls[index - 1].items()
                if key
                not in {
                    "ordinal",
                    "database_load",
                    "mechanism_verdict",
                    "causal_locus_projection",
                }
            }
            for index in ordinals
        ]
        if summaries != comparable:
            raise ValueError("public transfer citation summaries differ from tool calls")
        mechanism_verdicts = [
            tool_calls[index - 1]["mechanism_verdict"]
            for index in ordinals
            if tool_calls[index - 1]["mechanism_verdict"] is not None
        ]
        if _mapping_list(citation, "mechanism_verdicts") != mechanism_verdicts:
            raise ValueError("public transfer citation mechanism verdicts drifted")
        expected_rejection_codes = list(
            dict.fromkeys(
                code
                for verdict in mechanism_verdicts
                for code in _string_list(verdict.get("rejection_codes"))
            )
        )
        if _string_list(citation.get("rejection_codes")) != expected_rejection_codes:
            raise ValueError("public transfer citation rejection codes drifted")
        locus_projections = [
            tool_calls[index - 1]["causal_locus_projection"]
            for index in ordinals
            if tool_calls[index - 1]["causal_locus_projection"] is not None
        ]
        if _mapping_list(citation, "causal_locus_projections") != locus_projections:
            raise ValueError("public transfer citation locus projections drifted")
        expected_execution_valid = (
            citation.get("claim_present") is True
            and resolution == "unique"
            and _public_query_execution_valid(summaries[0])
        )
        if citation.get("execution_valid") is not expected_execution_valid:
            raise ValueError("public transfer citation execution validity drifted")
        for summary in summaries:
            _validate_query_summary(summary)
    diagnosis = payload.get("diagnosis")
    diagnosis = diagnosis if isinstance(diagnosis, Mapping) else None
    scope_match = diagnosis is not None and diagnosis.get("causal_scope") == case.causal_scope.value
    if case.causal_scope.uses_causal_component:
        locus_match = (
            diagnosis is not None
            and isinstance(diagnosis.get("causal_component"), str)
            and case.causal_component is not None
            and component_matches(
                str(diagnosis["causal_component"]),
                case.causal_component,
                source_identities=_source_declared_identities(case),
            )
            and diagnosis.get("edge_source") is None
            and diagnosis.get("edge_destination") is None
        )
    else:
        locus_match = (
            diagnosis is not None
            and isinstance(diagnosis.get("edge_source"), str)
            and isinstance(diagnosis.get("edge_destination"), str)
            and component_matches(str(diagnosis["edge_source"]), str(case.edge_source))
            and component_matches(str(diagnosis["edge_destination"]), str(case.edge_destination))
            and diagnosis.get("causal_component") is None
        )
    category_match = (
        diagnosis is not None and diagnosis.get("fault_category") == case.fault_category.value
    )
    mechanism_match = diagnosis is not None and diagnosis.get("mechanism_code") in {
        code.value for code in _accepted_mechanism_codes(case)
    }
    # Mirrors `transfer_scorer`: the deterministic verifier reads SQL, so a run
    # whose evidence is entirely in another query language is not estimable
    # rather than failed. The two paths must agree or the public artifact stops
    # rescoring.
    verifiable_citations = sum(
        citation.get("execution_valid") is True
        and any(
            summary.get("tool_name") in GROUNDING_VERIFIER_TOOLS
            for summary in _mapping_list(citation, "query_summaries")
        )
        for citation in citations
    )
    auditable = case.mechanism_evidence is not None and verifiable_citations > 0
    grounding_not_estimable_reason = (
        None
        if auditable
        else (
            "no_deterministic_oracle"
            if case.mechanism_evidence is None
            else "grounding_verifier_language_unsupported"
        )
    )
    allowed_operations = (
        set(case.mechanism_evidence.allowed_operations) if case.mechanism_evidence else set()
    )
    causal_operation_match = (
        None
        if not allowed_operations
        else (
            diagnosis is not None
            and isinstance(diagnosis.get("causal_operation"), str)
            and str(diagnosis["causal_operation"]).strip() in allowed_operations
        )
    )
    verdicts = [
        ClaimVerdict.model_validate(projection)
        for citation in citations
        if citation.get("execution_valid") is True
        for projection in _mapping_list(citation, "mechanism_verdicts")
    ]
    baseline_present = any(verdict.baseline_clear for verdict in verdicts)
    anomaly_present = any(verdict.anomaly_present for verdict in verdicts)
    direct_present = any(verdict.direct_mechanism for verdict in verdicts)
    mechanism_evidence_match = (
        direct_present or (baseline_present and anomaly_present) if auditable else None
    )
    expected_baseline_ordinals = [
        int(citation["ordinal"])
        for citation in citations
        if citation.get("execution_valid") is True
        and any(
            ClaimVerdict.model_validate(projection).baseline_clear
            for projection in _mapping_list(citation, "mechanism_verdicts")
        )
    ]
    expected_anomaly_ordinals = [
        int(citation["ordinal"])
        for citation in citations
        if citation.get("execution_valid") is True
        and any(
            ClaimVerdict.model_validate(projection).anomaly_present
            for projection in _mapping_list(citation, "mechanism_verdicts")
        )
    ]
    expected_mechanism_ordinals = [
        int(citation["ordinal"])
        for citation in citations
        if mechanism_evidence_match
        and citation.get("execution_valid") is True
        and any(
            verdict.baseline_clear or verdict.anomaly_present or verdict.direct_mechanism
            for verdict in (
                ClaimVerdict.model_validate(projection)
                for projection in _mapping_list(citation, "mechanism_verdicts")
            )
        )
    ]
    expected_locus_ordinals = [
        int(citation["ordinal"])
        for citation in citations
        if citation.get("execution_valid") is True
        and any(
            _public_locus_projection_matches(projection, case)
            for projection in _mapping_list(citation, "causal_locus_projections")
        )
    ]
    locus_evidence_match = bool(expected_locus_ordinals) if auditable else None
    for citation in citations:
        ordinal = int(citation["ordinal"])
        if citation.get("supports_baseline_clear") is not (ordinal in expected_baseline_ordinals):
            raise ValueError("public transfer baseline support annotation drifted")
        if citation.get("supports_anomaly_present") is not (ordinal in expected_anomaly_ordinals):
            raise ValueError("public transfer anomaly support annotation drifted")
        if citation.get("supports_fault_mechanism") is not (ordinal in expected_mechanism_ordinals):
            raise ValueError("public transfer mechanism support annotation drifted")
        if citation.get("supports_causal_locus") is not (ordinal in expected_locus_ordinals):
            raise ValueError("public transfer locus support annotation drifted")
    typed_evidence = bool(citations) and all(
        (claim_types := _string_list(citation.get("claim_types")))
        and len(claim_types) == len(set(claim_types))
        for citation in citations
    )
    execution = _mapping(payload, "execution")
    runner_contract_match = (
        payload.get("runner") == "api"
        and payload.get("model") == expected_model
        and payload.get("api_transport") == expected_transport.value
        and payload.get("reasoning_effort") == expected_reasoning_effort
        and payload.get("max_output_tokens") == expected_max_output_tokens
    )
    execution_reliability = (
        runner_contract_match
        and _strict_int(execution.get("tool_calls_executed")) is not None
        and int(execution["tool_calls_executed"]) <= max_tool_calls
        and execution.get("runner_error") is False
        and execution.get("tool_budget_exhausted") is False
        and execution.get("invalid_rejected_tool_calls") == 0
        and len(tool_calls) == execution.get("tool_calls_executed")
    )
    diagnosis_correct = all((scope_match, locus_match, category_match, mechanism_match))
    required_evidence_covered = (
        (typed_evidence and mechanism_evidence_match and locus_evidence_match)
        if auditable
        else None
    )
    citations_execution_valid = (
        bool(citations)
        and references_unique
        and all(citation.get("execution_valid") is True for citation in citations)
    )
    valid_evidence_count = sum(citation.get("execution_valid") is True for citation in citations)
    has_execution_valid_citation = valid_evidence_count > 0
    efficiency_eligible = (
        diagnosis_correct and has_execution_valid_citation and execution_reliability
    )
    adjudication_reason_codes = list(
        dict.fromkeys(
            code
            for verdict in verdicts
            for code in (
                *verdict.baseline_rejection_codes,
                *verdict.anomaly_rejection_codes,
            )
        )
    )
    semantic_adjudication_required = (
        diagnosis_correct
        and typed_evidence
        and citations_execution_valid
        and execution_reliability
        # Only a measured failure earns adjudication; a not-estimable
        # grounding must not queue a correct run.
        and required_evidence_covered is False
    )
    supporting_ordinals = [
        int(citation["ordinal"])
        for citation in citations
        if int(citation["ordinal"]) in {*expected_locus_ordinals, *expected_mechanism_ordinals}
    ]
    supporting_call_ordinals = [
        int(call_ordinal)
        for citation in citations
        if int(citation["ordinal"]) in supporting_ordinals
        for call_ordinal in citation["matching_tool_call_ordinals"]
    ]
    support_call_ordinal = (
        max(supporting_call_ordinals)
        if required_evidence_covered and supporting_call_ordinals
        else None
    )
    calls_through_evidence = (
        tool_calls[:support_call_ordinal] if support_call_ordinal is not None else None
    )
    rows_through_evidence = (
        sum(
            int(_mapping(call, "database_load")["rows_returned"]) for call in calls_through_evidence
        )
        if calls_through_evidence is not None
        and all(isinstance(call.get("database_load"), Mapping) for call in calls_through_evidence)
        else None
    )
    headline_checks = {
        "runner contract mismatch": runner_contract_match,
        "causal scope mismatch": scope_match,
        "causal locus mismatch": locus_match,
        "fault category mismatch": category_match,
        "mechanism code mismatch": mechanism_match,
        "no execution-valid citation": has_execution_valid_citation,
        "execution reliability failed": execution_reliability,
    }
    evidence_checks = (
        {
            "causal locus lacks incident-local evidence": locus_evidence_match,
            "baseline-clear evidence is missing": baseline_present or direct_present,
            "anomalous mechanism evidence is missing": anomaly_present or direct_present,
            "fault mechanism lacks complete transition evidence": mechanism_evidence_match,
            "required evidence claims are missing": typed_evidence,
            "citation integrity failed": citations_execution_valid,
        }
        if auditable
        else {
            "required evidence claims are missing": typed_evidence,
            "citation integrity failed": citations_execution_valid,
        }
    )
    evaluation = {
        "diagnosis_correct": diagnosis_correct,
        "causal_locus_match": locus_match,
        "causal_scope_match": scope_match,
        "fault_category_match": category_match,
        "mechanism_code_match": mechanism_match,
        "causal_operation_match": causal_operation_match,
        "causal_locus_evidence_match": locus_evidence_match,
        "baseline_evidence_match": baseline_present if auditable else None,
        "anomaly_evidence_match": anomaly_present if auditable else None,
        "mechanism_evidence_match": mechanism_evidence_match,
        "required_evidence_covered": required_evidence_covered,
        "grounding_not_estimable_reason": grounding_not_estimable_reason,
        "citations_execution_valid": citations_execution_valid,
        "execution_reliability": execution_reliability,
        "efficiency_eligible": efficiency_eligible,
        "auditable_completion": efficiency_eligible and citations_execution_valid,
        "success": efficiency_eligible and citations_execution_valid,
        "cited_evidence_count": len(citations),
        "valid_evidence_count": valid_evidence_count,
        "claim_grounding": {
            "causal_locus": {
                "required": True,
                "grounded": locus_evidence_match,
                "supporting_evidence_ordinals": expected_locus_ordinals,
            },
            "fault_mechanism": {
                "required": True,
                "grounded": mechanism_evidence_match,
                "supporting_evidence_ordinals": expected_mechanism_ordinals,
            },
        },
        "failure_reasons": [reason for reason, passed in headline_checks.items() if not passed],
        "evidence_audit_failure_reasons": [
            reason for reason, passed in evidence_checks.items() if not passed
        ],
        "semantic_adjudication_required": semantic_adjudication_required,
        "semantic_adjudication_reason_codes": (
            adjudication_reason_codes if semantic_adjudication_required else []
        ),
        "correct_completion_tool_calls": len(tool_calls) if efficiency_eligible else None,
        "tool_calls_through_required_evidence": support_call_ordinal,
        "rows_returned_through_required_evidence": rows_through_evidence,
        "supporting_evidence_ordinals": supporting_ordinals,
        "causal_locus_evidence_ordinals": expected_locus_ordinals,
        "baseline_evidence_ordinals": expected_baseline_ordinals,
        "anomaly_evidence_ordinals": expected_anomaly_ordinals,
        "mechanism_evidence_ordinals": expected_mechanism_ordinals,
    }
    stored = _mapping(payload, "evaluation")
    if stored != evaluation:
        raise ValueError("public transfer evaluation does not deterministically rescore")
    return evaluation


def canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _public_adjudication_resolution(
    resolution: Mapping[str, object],
) -> dict[str, object]:
    public_resolutions = []
    for item in _mapping_list(resolution, "resolutions"):
        public_item = dict(item)
        public_item["judge_decisions"] = [
            {
                **dict(decision),
                "rationale": "redacted from public artifact",
                "failed_requirements": [],
            }
            for decision in _mapping_list(item, "judge_decisions")
        ]
        human = item.get("human_decision")
        if human is not None:
            public_item["human_decision"] = {
                **dict(_mapping(item, "human_decision")),
                "rationale": "redacted from public artifact",
            }
        public_resolutions.append(public_item)
    return {
        "schema_version": resolution.get("schema_version"),
        "queue_sha256": resolution.get("queue_sha256"),
        "resolutions": public_resolutions,
    }


def build_measurement_artifact(
    private_report: dict[str, object],
    protocol_path: Path,
    adjudication_resolution: Mapping[str, object] | None = None,
) -> dict[str, object]:
    from agent_rca_bench.transfer_formal import validate_private_report
    from agent_rca_bench.transfer_protocol import load_transfer_protocol, sha256_file

    protocol, selection = load_transfer_protocol(protocol_path)
    validate_private_report(
        private_report,
        protocol,
        protocol_path,
        selection,
        require_complete=True,
    )
    source_by_case = {
        str(_mapping(source, "case")["opaque_case_id"]): source
        for source in _mapping_list(private_report, "source_audits")
    }
    specs = {case.opaque_case_id: case for case in selection.selected_cases}
    models = {model.model: model for model in protocol.models}
    adjudication_enabled = protocol.semantic_adjudication.enabled
    if adjudication_enabled:
        queue = build_semantic_adjudication_queue(private_report, protocol, selection)
        queue_candidates = _mapping_list(queue, "candidates")
        if queue_candidates and adjudication_resolution is None:
            raise ValueError("measurement report requires a complete semantic adjudication")
        if adjudication_resolution is None:
            adjudication_resolution = {
                "schema_version": 1,
                "queue_sha256": canonical_sha256(queue),
                "resolutions": [],
            }
        validated_adjudication = validate_semantic_adjudication_resolution(
            queue, adjudication_resolution
        )
        public_resolution = _public_adjudication_resolution(validated_adjudication)
        resolutions_by_cell = {
            int(_mapping(item, "private_binding")["cell_index"]): item
            for item in _mapping_list(public_resolution, "resolutions")
        }
    else:
        if adjudication_resolution is not None:
            raise ValueError("semantic adjudication is disabled by the transfer protocol")
        queue_candidates = []
        public_resolution = None
        resolutions_by_cell = {}
    public_runs = []
    for item in _mapping_list(private_report, "runs"):
        case = specs[str(item["case_id"])]
        run = AgentRun.model_validate(item.get("run"))
        evaluation = TransferEvaluation.model_validate(item.get("evaluation"))
        load = DatabaseLoad.model_validate(item.get("database_load"))
        public = sanitize_transfer_run(run, evaluation, case, load)
        resolution = resolutions_by_cell.get(int(item["cell_index"]))
        if not adjudication_enabled:
            effective_evaluation = evaluation
            public_adjudication = {"status": "disabled"}
            supporting_ordinals = None
        elif resolution is None:
            effective_evaluation = evaluation
            public_adjudication = {"status": "not-required"}
            supporting_ordinals: list[int] | None = None
        else:
            supporting_ordinals = [
                int(value) for value in resolution["supporting_evidence_ordinals"]
            ]
            effective_evaluation = apply_semantic_adjudication(
                run,
                evaluation,
                evidence_sufficient=resolution["evidence_sufficient"] is True,
                supporting_evidence_ordinals=supporting_ordinals,
            )
            public_adjudication = {
                key: value for key, value in resolution.items() if key != "private_binding"
            }
            public_adjudication["status"] = (
                "accepted" if resolution["evidence_sufficient"] is True else "rejected"
            )
        public["adjudication"] = public_adjudication
        if adjudication_enabled:
            public["adjudicated_sensitivity"] = _public_adjudicated_sensitivity(
                effective_evaluation,
                public["citations"],
                supporting_evidence_ordinals=(
                    supporting_ordinals
                    if resolution is not None and resolution["evidence_sufficient"] is True
                    else None
                ),
            )
        model = models[str(item["model"])]
        raw_run = run.model_dump(mode="json")
        uncached, cache_read, cache_creation, complete = _raw_input_breakdown(raw_run)
        public["usage"] = {
            **_mapping(public, "usage"),
            "uncached_input_tokens": uncached if complete else None,
            "cache_read_input_tokens": cache_read if complete else None,
            "cache_creation_input_tokens": cache_creation if complete else None,
            "cache_breakdown_complete": complete,
            "estimated_cost": _estimated_api_cost(
                raw_run,
                _mapping(private_report, "pricing_snapshot")[model.model],
            ),
            "cost_currency": _mapping(private_report, "pricing_snapshot")[model.model].get(
                "currency"
            ),
        }
        validate_public_transfer_run(
            public,
            case,
            expected_model=model.model,
            expected_transport=model.api_transport,
            expected_reasoning_effort=model.reasoning_effort,
            expected_max_output_tokens=model.max_output_tokens,
            max_tool_calls=protocol.max_tool_calls,
        )
        public_runs.append(
            {
                **{
                    key: item[key]
                    for key in (
                        "cell_index",
                        "case_index",
                        "case_id",
                        "model_index",
                        "model",
                        "provider",
                        "api_transport",
                        "prompt_cache",
                        "max_output_tokens",
                        "reasoning_effort",
                        "repetition",
                        "position",
                        "visibility",
                    )
                },
                "run": public,
            }
        )
    public_sources = [
        _public_source(source_by_case[case.opaque_case_id], case)
        for case in selection.selected_cases
    ]
    model_reports = _model_reports(
        public_runs,
        [model.model for model in protocol.models],
        family_size=protocol.inference.holm_family_size,
        families=protocol.inference.confirmatory_families,
    )
    payload = {
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "artifact_type": "semantic-rca-transfer-measurement",
        "publication_status": (
            "sanitized measurement result; contains no provider payloads or raw telemetry rows"
        ),
        "license": {
            "benchmark_code": "Apache-2.0",
            "source_datasets": _source_dataset_licenses(selection),
            "artifact_schema_and_derived_aggregates": "Apache-2.0",
        },
        "benchmark_protocol": private_report["benchmark_protocol"],
        "formal_protocol": private_report["formal_protocol"],
        "sources": public_sources,
        "runs": public_runs,
        "semantic_adjudication": {
            "enabled": adjudication_enabled,
            "queue_sha256": (
                validated_adjudication["queue_sha256"] if adjudication_enabled else None
            ),
            "candidate_count": len(queue_candidates),
            "accepted_count": sum(
                item.get("evidence_sufficient") is True
                for item in (
                    _mapping_list(public_resolution, "resolutions") if adjudication_enabled else []
                )
            ),
            "rejected_count": sum(
                item.get("evidence_sufficient") is False
                for item in (
                    _mapping_list(public_resolution, "resolutions") if adjudication_enabled else []
                )
            ),
            "by_treatment": {
                visibility: {
                    status: sum(
                        item["visibility"] == visibility
                        and _mapping(_mapping(item, "run"), "adjudication").get("status") == status
                        for item in public_runs
                    )
                    for status in ("accepted", "rejected")
                }
                for visibility in ("raw", "semantic_graph")
            },
            "decision_rule": protocol.semantic_adjudication.decision_rule,
        },
        "model_reports": model_reports,
        "inference": _public_inference(protocol.inference),
        "sanitization": {
            "excluded": [
                "provider responses, reasoning text, signatures, and provider tool-call IDs",
                "run IDs, query IDs, elapsed wall time, ports, endpoints, and local paths",
                "raw telemetry rows and provider error text",
                "free-form citation claims and diagnosis explanations",
                "judge and human-adjudicator rationales",
            ],
            "included": [
                "structured diagnosis fields",
                "all tool inputs, row counts, truncation flags, and result hashes",
                "citation-to-tool-call resolution and deterministic scorer projections",
                "no-model normalized edge sets and aggregate mechanism evidence",
                "token, database-load, reliability, and case-level effect fields",
            ],
        },
        "bindings": {
            "protocol_fixture_sha256": sha256_file(protocol_path),
            "private_report_semantic_sha256": canonical_sha256(private_report),
            "semantic_adjudication_resolution_sha256": (
                canonical_sha256(public_resolution) if adjudication_enabled else None
            ),
        },
    }
    artifact = {
        **payload,
        "integrity": {
            "semantic_payload_sha256": canonical_sha256(payload),
            "hash_scope": "all artifact fields except integrity",
        },
    }
    validate_measurement_artifact(artifact, protocol_path)
    return artifact


def validate_measurement_artifact(
    artifact: Mapping[str, object],
    protocol_path: Path,
) -> None:
    from agent_rca_bench.transfer_protocol import (
        formal_schedule,
        load_transfer_protocol,
        sha256_file,
    )

    protocol, selection = load_transfer_protocol(protocol_path)
    if (
        artifact.get("artifact_schema_version") != ARTIFACT_SCHEMA_VERSION
        or artifact.get("artifact_type") != "semantic-rca-transfer-measurement"
        or artifact.get("formal_protocol") != json.loads(protocol_path.read_text())
        or _mapping(artifact, "bindings").get("protocol_fixture_sha256")
        != sha256_file(protocol_path)
    ):
        raise ValueError("unsupported or drifted transfer measurement artifact")
    sources = _mapping_list(artifact, "sources")
    if [source.get("opaque_case_id") for source in sources] != [
        case.opaque_case_id for case in selection.selected_cases
    ]:
        raise ValueError("public transfer source roster drifted")
    runs = _mapping_list(artifact, "runs")
    schedule = formal_schedule(protocol, selection)
    if len(runs) != len(schedule):
        raise ValueError("public transfer artifact is incomplete")
    specs = {case.opaque_case_id: case for case in selection.selected_cases}
    models = {model.model: model for model in protocol.models}
    for expected, item in zip(schedule, runs, strict=True):
        if any(item.get(key) != value for key, value in expected.items()):
            raise ValueError("public transfer schedule drifted")
        model = models[str(item["model"])]
        deterministic = validate_public_transfer_run(
            _mapping(item, "run"),
            specs[str(item["case_id"])],
            expected_model=model.model,
            expected_transport=model.api_transport,
            expected_reasoning_effort=model.reasoning_effort,
            expected_max_output_tokens=model.max_output_tokens,
            max_tool_calls=protocol.max_tool_calls,
        )
        if protocol.semantic_adjudication.enabled:
            _validate_public_adjudication(_mapping(item, "run"), deterministic)
        elif _mapping(_mapping(item, "run"), "adjudication").get("status") != "disabled":
            raise ValueError("public transfer run adjudication status drifted")
    adjudication = _mapping(artifact, "semantic_adjudication")
    if not protocol.semantic_adjudication.enabled:
        if (
            adjudication
            != {
                "enabled": False,
                "queue_sha256": None,
                "candidate_count": 0,
                "accepted_count": 0,
                "rejected_count": 0,
                "by_treatment": {
                    "raw": {"accepted": 0, "rejected": 0},
                    "semantic_graph": {"accepted": 0, "rejected": 0},
                },
                "decision_rule": protocol.semantic_adjudication.decision_rule,
            }
            or _mapping(artifact, "bindings").get("semantic_adjudication_resolution_sha256")
            is not None
        ):
            raise ValueError("disabled semantic adjudication summary drifted")
        expected_reports = _model_reports(
            runs,
            [model.model for model in protocol.models],
            family_size=protocol.inference.holm_family_size,
            families=protocol.inference.confirmatory_families,
        )
        if artifact.get("model_reports") != expected_reports:
            raise ValueError("public transfer model reports drifted")
        if artifact.get("inference") != _public_inference(protocol.inference):
            raise ValueError("public transfer inference contract drifted")
        return
    candidate_runs = [
        _mapping(item, "run")
        for item in runs
        if _mapping(_mapping(item, "run"), "adjudication").get("status") in {"accepted", "rejected"}
    ]
    public_resolutions = sorted(
        [
            {
                **{
                    key: value
                    for key, value in _mapping(run, "adjudication").items()
                    if key != "status"
                },
                "private_binding": {
                    "cell_index": item["cell_index"],
                    "case_id": item["case_id"],
                },
            }
            for item in runs
            if (run := _mapping(item, "run"))
            and _mapping(run, "adjudication").get("status") in {"accepted", "rejected"}
        ],
        key=lambda item: str(item["candidate_sha256"]),
    )
    replayed_resolution = {
        "schema_version": 1,
        "queue_sha256": adjudication.get("queue_sha256"),
        "resolutions": public_resolutions,
    }
    if (
        adjudication.get("candidate_count") != len(candidate_runs)
        or adjudication.get("accepted_count")
        != sum(_mapping(run, "adjudication").get("status") == "accepted" for run in candidate_runs)
        or adjudication.get("rejected_count")
        != sum(_mapping(run, "adjudication").get("status") == "rejected" for run in candidate_runs)
        or adjudication.get("decision_rule") != protocol.semantic_adjudication.decision_rule
        or adjudication.get("by_treatment")
        != {
            visibility: {
                status: sum(
                    item["visibility"] == visibility
                    and _mapping(_mapping(item, "run"), "adjudication").get("status") == status
                    for item in runs
                )
                for status in ("accepted", "rejected")
            }
            for visibility in ("raw", "semantic_graph")
        }
        or _mapping(artifact, "bindings").get("semantic_adjudication_resolution_sha256")
        != canonical_sha256(replayed_resolution)
    ):
        raise ValueError("public semantic adjudication summary drifted")
    expected_reports = _model_reports(
        runs,
        [model.model for model in protocol.models],
        family_size=protocol.inference.holm_family_size,
        families=protocol.inference.confirmatory_families,
    )
    if artifact.get("model_reports") != expected_reports:
        raise ValueError("public transfer model reports drifted")
    if artifact.get("inference") != _public_inference(protocol.inference):
        raise ValueError("public transfer inference contract drifted")
    payload = {key: value for key, value in artifact.items() if key != "integrity"}
    if _mapping(artifact, "integrity").get("semantic_payload_sha256") != canonical_sha256(payload):
        raise ValueError("public transfer artifact payload hash drifted")
    _reject_private_fields(artifact)


def _public_source(audit: Mapping[str, object], case: TransferCaseSpec) -> dict[str, object]:
    source = _mapping(audit, "source")
    equality = _mapping(audit, "edge_equality")
    mechanism = _mapping(audit, "mechanism_evidence")
    ingestion = _mapping(audit, "ingestion")
    return {
        "opaque_case_id": case.opaque_case_id,
        "source_case": case.source_case,
        "system": case.system,
        "dataset_revision": audit.get("dataset_revision"),
        "source_files_sha256": case.source_files_sha256,
        "normal_window": list(case.normal_window),
        "abnormal_window": list(case.abnormal_window),
        "causal_scope": case.causal_scope.value,
        "causal_component": case.causal_component,
        "edge_source": case.edge_source,
        "edge_destination": case.edge_destination,
        "fault_category": case.fault_category.value,
        "mechanism_code": case.mechanism_code.value,
        "accepted_mechanism_codes": sorted(code.value for code in _accepted_mechanism_codes(case)),
        "source_row_counts": source.get("source_row_counts"),
        "metric_representation": source.get("metric_representation"),
        "ingestion": {
            key: ingestion.get(key)
            for key in (
                "database_counts",
                "expected_protocol_counts",
                "protocol_row_counts_match",
                "metric_data_points_accepted",
                "metric_data_points_rejected",
                "trace_spans_rejected",
                "source_protocol_counts",
                "protocol_rejections_zero",
                "id_remapping",
                "source_identity",
            )
        },
        "edge_equality": {
            "window_contract": equality.get("window_contract"),
            "period_raw_replay": _strip_query_runtime(equality.get("period_raw_replay")),
            "period_graph_replay": _strip_query_runtime(equality.get("period_graph_replay")),
            "graph_window_strategy_proof": equality.get("graph_window_strategy_proof"),
            "normalized_raw_edges": equality.get("normalized_raw_edges"),
            "normalized_graph_edges": equality.get("normalized_graph_edges"),
            "raw_edge_set_sha256": equality.get("raw_edge_set_sha256"),
            "graph_edge_set_sha256": equality.get("graph_edge_set_sha256"),
            "exact_edge_set_equality": equality.get("exact_edge_set_equality"),
        },
        "mechanism_evidence": {
            "predicate": mechanism.get("predicate"),
            "query": mechanism.get("query"),
            "normalized_result": mechanism.get("normalized_result"),
            "expected_result": mechanism.get("expected_result"),
            "scope_preserving_predicates": source.get("scope_preserving_predicates"),
            "identity_equivalent_predicates": source.get("identity_equivalent_predicates"),
            "evidence_match": mechanism.get("evidence_match"),
            "pass": mechanism.get("pass"),
        },
        "oracle": _oracle_projection(case.mechanism_evidence),
        "semantic_coverage": audit.get("semantic_coverage"),
        "no_model_gates": audit.get("no_model_gates"),
    }


def _public_inference(inference: object) -> dict[str, object]:
    return {
        # Every pre-declared family, not just the semantic-layer one. Publishing
        # a single estimand would hide the comparison the second family answers.
        "confirmatory_families": [
            {
                "goal": family.goal,
                "status": family.status,
                "estimand": (
                    f"{family.treatment.value} - {family.baseline.value} "
                    "within the same model and case"
                ),
                "metrics": list(family.metrics),
            }
            for family in inference.confirmatory_families
        ],
        "independent_unit": "case",
        "repetitions": "descriptive; paired run deltas are reduced to a case median",
        "primary_metrics": list(inference.primary_metrics),
        "holm_family_size": inference.holm_family_size,
        "null_metric_meaning": inference.null_metric_meaning,
        "zero_call_delta_meaning": inference.tied_calls_meaning,
        "non_significant_meaning": "insufficient evidence; not evidence of equivalence",
        "directional_non_significant_reporting": (
            inference.direction_consistent_non_significant_meaning
        ),
        "cross_model_pooling": inference.correctness_pooled_across_models,
        "headline_eligibility": inference.headline_eligibility,
        "evidence_sufficiency_role": inference.evidence_sufficiency_role,
    }


def _family_field(family: object, name: str) -> object:
    """Reads a confirmatory family from the protocol fixture or a plain mapping.

    The families are declared once, in the protocol. Keeping a second copy in
    code let the two drift silently.
    """
    value = family[name] if isinstance(family, Mapping) else getattr(family, name)
    return value.value if hasattr(value, "value") else value


def _model_reports(
    runs: list[Mapping[str, object]],
    model_names: list[str],
    *,
    family_size: int,
    families: Sequence[object],
) -> dict[str, object]:
    """Per-model effects, one pre-declared confirmatory family at a time.

    Each family is Holm-corrected on its own. Pooling them would make one
    goal's significance depend on how many tests the other goal ran.
    """
    treatments = (
        tuple(dict.fromkeys(str(item["visibility"]) for item in runs)) or _GREPTIMEDB_TREATMENTS
    )
    active = [
        family
        for family in families
        if {_family_field(family, "baseline"), _family_field(family, "treatment")}
        <= set(treatments)
    ]
    if not active:
        raise ValueError("no confirmatory family matches the treatments present in the runs")
    reports: dict[str, object] = {}
    family_p_values: dict[str, list[tuple[str, str, float]]] = {
        str(_family_field(family, "goal")): [] for family in active
    }
    for model in model_names:
        model_runs = [item for item in runs if item["model"] == model]
        effects: dict[str, object] = {}
        deterministic: dict[str, object] = {}
        for family in active:
            goal = str(_family_field(family, "goal"))
            report, p_values = _effect_report(
                model_runs,
                evaluation_key="evaluation",
                family_size=family_size,
                baseline=str(_family_field(family, "baseline")),
                treatment=str(_family_field(family, "treatment")),
                metrics=tuple(_family_field(family, "metrics")),
                treatments=treatments,
            )
            family_p_values[goal].extend((model, metric, value) for metric, value in p_values)
            effects[goal] = {
                "comparison": (
                    f"{_family_field(family, 'treatment')} - {_family_field(family, 'baseline')}"
                ),
                **report,
            }
            deterministic = report
        reports[model] = {
            "runs": len(model_runs),
            "confirmatory_families": effects,
            **deterministic,
            "evidence_quality": _evidence_quality_report(model_runs),
            "reliability": {
                "runner_errors": sum(
                    _mapping(_mapping(item, "run"), "execution").get("runner_error") is True
                    for item in model_runs
                ),
                "budget_exhaustions": sum(
                    _mapping(_mapping(item, "run"), "execution").get("tool_budget_exhausted")
                    is True
                    for item in model_runs
                ),
                "failed_database_queries": sum(
                    int(
                        _mapping(_mapping(item, "run"), "database_load").get(
                            "failed_query_count", 0
                        )
                    )
                    for item in model_runs
                ),
            },
            "graph_usage": {
                "semantic_graph_runs": sum(
                    item["visibility"] == "semantic_graph" for item in model_runs
                ),
                "runs_using_query_semantic_graph": sum(
                    item["visibility"] == "semantic_graph"
                    and any(
                        call.get("tool_name") == "query_semantic_graph"
                        for call in _mapping_list(_mapping(item, "run"), "tool_calls")
                    )
                    for item in model_runs
                ),
            },
            "usage": _usage_summary(model_runs),
        }
    for goal, values in family_p_values.items():
        _apply_holm(reports, values, family_size=family_size, goal=goal)
    return reports


def _evidence_quality_report(model_runs: list[Mapping[str, object]]) -> dict[str, object]:
    """The deterministic evidence audit, which only covers the GreptimeDB arms.

    The verifier reads SQL, so the split arm reports `None` rather than a
    verdict. Counting it here would divide the pair total by the wrong number
    of treatments and read as an evidence failure.
    """
    model_runs = [item for item in model_runs if item["visibility"] in _GREPTIMEDB_TREATMENTS]
    by_treatment = {
        visibility: sum(
            _mapping(_mapping(item, "run"), "evaluation").get("required_evidence_covered") is True
            for item in model_runs
            if item["visibility"] == visibility
        )
        for visibility in ("raw", "semantic_graph")
    }
    pairs: dict[tuple[str, int], set[str]] = defaultdict(set)
    for item in model_runs:
        evaluation = _mapping(_mapping(item, "run"), "evaluation")
        if evaluation.get("required_evidence_covered") is True:
            pairs[(str(item["case_id"]), int(item["repetition"]))].add(str(item["visibility"]))
    disposition = Counter()
    for values in pairs.values():
        disposition[
            "both"
            if values == {"raw", "semantic_graph"}
            else "raw_only"
            if values == {"raw"}
            else "semantic_graph_only"
        ] += 1
    total_pairs = len(model_runs) // len(_GREPTIMEDB_TREATMENTS)
    disposition["neither"] = total_pairs - sum(disposition.values())
    return {
        "role": "secondary deterministic evidence-sufficiency audit",
        "required_evidence_covered_by_treatment": by_treatment,
        "paired_disposition": {
            status: disposition[status]
            for status in ("both", "raw_only", "semantic_graph_only", "neither")
        },
    }


_GREPTIMEDB_TREATMENTS = ("raw", "semantic_graph")

# One statement per source that contributed a case. Naming only the first would
# publish derived facts from a dataset whose terms the artifact never declares.
SOURCE_DATASET_LICENSES = {
    "openrca2": (
        "OpenRCA2 ops-lite: dataset card says Apache-2.0, paper says CC-BY-SA-4.0; "
        "telemetry rows are not redistributed"
    ),
    "rca100": (
        "RCA100 v1.1: CC BY-NC-SA 4.0; telemetry rows are not redistributed and the "
        "dataset paper arXiv:2606.29193 must be attributed with derived facts"
    ),
}


def _source_dataset_licenses(selection: object) -> dict[str, str]:
    licenses = {}
    for source in selection.sources:
        if not source.case_ids:
            continue
        statement = SOURCE_DATASET_LICENSES.get(source.adapter)
        if statement is None:
            raise ValueError(f"no published license statement for adapter {source.adapter}")
        licenses[source.adapter] = statement
    return licenses


# Every argument a native investigation tool accepts. The values are the model's
# own query text and bounds, so they are published in full.
NATIVE_QUERY_INPUT_KEYS = (
    "operation",
    "query",
    "match",
    "metric",
    "label",
    "tag",
    "trace_id",
    "time",
    "start",
    "end",
    "step",
    "direction",
    "max_items",
)

_ALL_PAIR_METRICS = (
    "rows_returned",
    "correct_completion_tool_calls",
    "provider_visible_input_tokens",
    "reported_total_tokens",
)


def _metric_delta(
    metric: str,
    baseline_run: Mapping[str, object],
    treatment_run: Mapping[str, object],
    baseline_eval: Mapping[str, object],
    treatment_eval: Mapping[str, object],
) -> int | None:
    if metric == "rows_returned":
        rows = [
            _mapping(run, "database_load").get("rows_returned")
            for run in (baseline_run, treatment_run)
        ]
        # Not applicable in the split arm, where a returned row is not a
        # database row; a null there must stay null rather than become a zero.
        if any(value is None for value in rows):
            return None
        return int(rows[1]) - int(rows[0])
    if metric == "correct_completion_tool_calls":
        return int(treatment_eval["correct_completion_tool_calls"]) - int(
            baseline_eval["correct_completion_tool_calls"]
        )
    if metric == "provider_visible_input_tokens":
        return int(
            _mapping(treatment_run, "usage").get("provider_visible_input_tokens", 0) or 0
        ) - int(_mapping(baseline_run, "usage").get("provider_visible_input_tokens", 0) or 0)
    if metric == "reported_total_tokens":
        return _reported_total_tokens(treatment_run) - _reported_total_tokens(baseline_run)
    raise ValueError(f"unknown paired metric: {metric}")


def _metric_baseline(
    metric: str,
    baseline_run: Mapping[str, object],
    baseline_eval: Mapping[str, object],
) -> int | None:
    """The baseline arm's own value, so a delta can be read as a proportion.

    Same source fields and same null rules as `_metric_delta`, because a
    percentage built from a differently-selected denominator would not describe
    the delta it is dividing.
    """
    if metric == "rows_returned":
        rows = _mapping(baseline_run, "database_load").get("rows_returned")
        return None if rows is None else int(rows)
    if metric == "correct_completion_tool_calls":
        return int(baseline_eval["correct_completion_tool_calls"])
    if metric == "provider_visible_input_tokens":
        return int(_mapping(baseline_run, "usage").get("provider_visible_input_tokens", 0) or 0)
    if metric == "reported_total_tokens":
        return _reported_total_tokens(baseline_run)
    raise ValueError(f"unknown paired metric: {metric}")


def _relative_change(delta: int | float | None, baseline: int | None) -> float | None:
    """`delta / baseline`, or None when the baseline gives no scale to divide by."""
    if delta is None or baseline is None or baseline == 0:
        return None
    return delta / baseline


def _effect_report(
    model_runs: list[Mapping[str, object]],
    *,
    evaluation_key: str,
    family_size: int,
    baseline: str,
    treatment: str,
    metrics: tuple[str, ...],
    treatments: tuple[str, ...],
) -> tuple[dict[str, object], list[tuple[str, float]]]:
    """One paired comparison, `treatment - baseline`, over a model's runs.

    The pair is named rather than inferred. Requiring the cell to hold exactly
    two treatments silently dropped every pair once a third arm existed, which
    reported "no estimable pairs" instead of failing.
    """
    pairs: dict[tuple[str, int], dict[str, Mapping[str, object]]] = defaultdict(dict)
    for item in model_runs:
        pairs[(str(item["case_id"]), int(item["repetition"]))][str(item["visibility"])] = item
    pair_deltas: dict[str, list[dict[str, object]]] = defaultdict(list)
    eligibility_disposition = Counter()
    for (case_id, repetition), values in sorted(pairs.items()):
        missing = {baseline, treatment} - set(values)
        if missing:
            raise ValueError(f"transfer pair {case_id}/{repetition} is missing {sorted(missing)}")
        raw = _mapping(values[baseline], "run")
        graph = _mapping(values[treatment], "run")
        raw_eval = _mapping(raw, evaluation_key)
        graph_eval = _mapping(graph, evaluation_key)
        raw_eligible = raw_eval.get("efficiency_eligible") is True
        graph_eligible = graph_eval.get("efficiency_eligible") is True
        eligible = raw_eligible and graph_eligible
        status = (
            "both"
            if eligible
            else f"{baseline}_only"
            if raw_eligible
            else f"{treatment}_only"
            if graph_eligible
            else "neither"
        )
        eligibility_disposition[status] += 1
        pair_deltas[case_id].append(
            {
                "case_id": case_id,
                "repetition": repetition,
                "eligible": eligible,
                "eligibility_status": status,
                **{
                    metric: (
                        _metric_delta(metric, raw, graph, raw_eval, graph_eval)
                        if eligible
                        else None
                    )
                    for metric in _ALL_PAIR_METRICS
                },
                **{
                    f"{metric}_relative": (
                        _relative_change(
                            _metric_delta(metric, raw, graph, raw_eval, graph_eval),
                            _metric_baseline(metric, raw, raw_eval),
                        )
                        if eligible
                        else None
                    )
                    for metric in _ALL_PAIR_METRICS
                },
            }
        )
    case_effects = []
    for case_id, deltas in sorted(pair_deltas.items()):
        eligible = [item for item in deltas if item["eligible"]]
        case_effects.append(
            {
                "case_id": case_id,
                "eligible_repetitions": len(eligible),
                **{
                    metric: _median_or_none([item[metric] for item in eligible])
                    for metric in _ALL_PAIR_METRICS
                },
                # The proportion is reduced per pair and then per case, never as
                # a ratio of two separately-taken medians, which is a different
                # quantity that no pair produced.
                **{
                    f"{metric}_relative": _median_or_none(
                        [item[f"{metric}_relative"] for item in eligible]
                    )
                    for metric in _ALL_PAIR_METRICS
                },
            }
        )
    metric_reports = {}
    p_values = []
    for metric in metrics:
        values = [item[metric] for item in case_effects if item[metric] is not None]
        p_value = _sign_test(values)
        metric_reports[metric] = {
            "eligible_cases": len(values),
            "case_median_delta": _median_or_none(values),
            "negative_cases": sum(value < 0 for value in values),
            "tied_cases": sum(value == 0 for value in values),
            "positive_cases": sum(value > 0 for value in values),
            "sign_test_two_sided_p": p_value,
            "holm_adjusted_p": None,
            "multiplicity_family_size": family_size,
        }
        if p_value is not None:
            p_values.append((metric, p_value))
    return (
        {
            "diagnosis_correct": {
                visibility: sum(
                    _mapping(_mapping(item, "run"), evaluation_key).get("diagnosis_correct") is True
                    for item in model_runs
                    if item["visibility"] == visibility
                )
                for visibility in treatments
            },
            "valid_completion": {
                visibility: sum(
                    _mapping(_mapping(item, "run"), evaluation_key).get("auditable_completion")
                    is True
                    for item in model_runs
                    if item["visibility"] == visibility
                )
                for visibility in treatments
            },
            "efficiency_eligibility": {
                "by_treatment": {
                    visibility: sum(
                        _mapping(_mapping(item, "run"), evaluation_key).get("efficiency_eligible")
                        is True
                        for item in model_runs
                        if item["visibility"] == visibility
                    )
                    for visibility in treatments
                },
                "paired_disposition": {
                    status: eligibility_disposition[status]
                    for status in ("both", f"{baseline}_only", f"{treatment}_only", "neither")
                },
                "claim_rejection_codes": {
                    visibility: dict(
                        sorted(
                            Counter(
                                code
                                for item in model_runs
                                if item["visibility"] == visibility
                                for citation in _mapping_list(_mapping(item, "run"), "citations")
                                for code in _claim_rejection_codes(citation)
                            ).items()
                        )
                    )
                    for visibility in ("raw", "semantic_graph")
                },
            },
            "case_effects": case_effects,
            "primary_metrics": metric_reports,
            "descriptive_metrics": {
                "reported_total_tokens": _direction_summary(
                    [
                        item["reported_total_tokens"]
                        for item in case_effects
                        if item["reported_total_tokens"] is not None
                    ]
                )
            },
        },
        p_values,
    )


def _reported_total_tokens(run: Mapping[str, object]) -> int:
    usage = _mapping(run, "usage")
    return int(usage.get("provider_visible_input_tokens", 0) or 0) + int(
        usage.get("output_tokens", 0) or 0
    )


def _direction_summary(values: list[int | float]) -> dict[str, object]:
    return {
        "eligible_cases": len(values),
        "case_median_delta": _median_or_none(values),
        "negative_cases": sum(value < 0 for value in values),
        "tied_cases": sum(value == 0 for value in values),
        "positive_cases": sum(value > 0 for value in values),
        "inference_role": "exploratory provider-accounted metric",
    }


def _usage_summary(runs: list[Mapping[str, object]]) -> dict[str, object]:
    usage = [_mapping(_mapping(item, "run"), "usage") for item in runs]
    token_fields = (
        "provider_visible_input_tokens",
        "uncached_input_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
    )
    totals = {
        field: (
            sum(int(item[field]) for item in usage)
            if all(isinstance(item.get(field), int) for item in usage)
            else None
        )
        for field in token_fields
    }
    costs = [item.get("estimated_cost") for item in usage]
    currencies = {item.get("cost_currency") for item in usage}
    return {
        **totals,
        "cache_breakdown_complete": all(
            item.get("cache_breakdown_complete") is True for item in usage
        ),
        "estimated_cost": (
            sum(float(value) for value in costs)
            if costs and all(isinstance(value, (int, float)) for value in costs)
            else None
        ),
        "cost_currency": next(iter(currencies)) if len(currencies) == 1 else None,
    }


def _sign_test(values: list[float]) -> float | None:
    nonzero = [value for value in values if value != 0]
    if not nonzero:
        return None
    successes = min(sum(value < 0 for value in nonzero), sum(value > 0 for value in nonzero))
    n = len(nonzero)
    probability = 2 * sum(math.comb(n, index) for index in range(successes + 1)) / 2**n
    return min(1.0, probability)


def _apply_holm(
    reports: dict[str, object],
    values: list[tuple[str, str, float]],
    *,
    family_size: int,
    goal: str,
) -> None:
    """Holm-corrects one family in place.

    `family_size` is the number of hypotheses `m`. More computed tests than `m`
    would drive `family_size - rank` to zero or below, and the running maximum
    would then silently return an uncorrected p rather than a conservative one.
    """
    if len(values) > family_size:
        raise ValueError(f"family {goal} has {len(values)} tests but declares m={family_size}")
    previous = 0.0
    for rank, (model, metric, value) in enumerate(sorted(values, key=lambda item: item[2])):
        adjusted = min(1.0, max(previous, value * (family_size - rank)))
        previous = adjusted
        family = reports[model]["confirmatory_families"][goal]
        family["primary_metrics"][metric]["holm_adjusted_p"] = adjusted


def _median_or_none(values: list[object]) -> float | int | None:
    numeric = [float(value) for value in values if isinstance(value, (int, float))]
    if not numeric:
        return None
    result = median(numeric)
    return int(result) if result.is_integer() else result


def _claim_rejection_codes(citation: Mapping[str, object]) -> list[str]:
    codes = []
    for verdict in _mapping_list(citation, "mechanism_verdicts"):
        codes.extend(
            f"baseline:{code}" for code in _string_list(verdict.get("baseline_rejection_codes"))
        )
        codes.extend(
            f"anomaly:{code}" for code in _string_list(verdict.get("anomaly_rejection_codes"))
        )
    return list(dict.fromkeys(codes))


def _strip_query_runtime(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _strip_query_runtime(item)
            for key, item in value.items()
            if key not in {"query_id", "elapsed_seconds", "result", "raw_edge_result"}
        }
    if isinstance(value, list):
        return [_strip_query_runtime(item) for item in value]
    return value


def _query_summary(trace: ToolTrace) -> dict[str, object]:
    output = trace.output if isinstance(trace.output, Mapping) else None
    rows = output.get("rows") if output is not None else None
    result = None
    columns = output.get("columns") if output is not None else None
    truncated = output.get("truncated") if output is not None else None
    if isinstance(columns, list) and isinstance(rows, list) and isinstance(truncated, bool):
        result_payload = {
            "columns": columns,
            "rows": rows,
            "truncated": truncated,
        }
        result = {
            "columns": columns,
            "row_count": len(rows),
            "truncated": truncated,
            "sha256": canonical_sha256(result_payload),
        }
    return {
        "tool_name": trace.tool_name,
        "input": _public_input(trace.tool_name, trace.input),
        "error": trace.error is not None,
        "result": result,
    }


def _public_input(tool_name: str, value: Mapping[str, object]) -> dict[str, object]:
    if tool_name == "execute_sql":
        return {key: value[key] for key in ("query", "sql") if isinstance(value.get(key), str)}
    if tool_name == "query_semantic_graph":
        allowed = {
            "view",
            "src_type",
            "src_id",
            "dst_type",
            "dst_id",
            "rel_type",
            "provenance",
            "entity_type",
            "entity_id",
            "limit",
        }
        return {key: item for key, item in value.items() if key in allowed}
    if tool_name in NATIVE_EVIDENCE_OPERATIONS:
        # Model-authored PromQL, LogQL and TraceQL, published like the SQL text
        # of `execute_sql`: without it a split citation could not be reviewed.
        return {
            key: value[key]
            for key in NATIVE_QUERY_INPUT_KEYS
            if isinstance(value.get(key), str)
            or (isinstance(value.get(key), int) and not isinstance(value.get(key), bool))
        }
    if tool_name == "describe_table":
        return {"table": value["table"]} if isinstance(value.get("table"), str) else {}
    if tool_name == "search_table_semantics":
        return {
            key: value[key]
            for key in ("query", "signal_type", "limit")
            if isinstance(value.get(key), (str, int)) and not isinstance(value.get(key), bool)
        }
    return {}


def _causal_locus_projection(
    trace: ToolTrace,
    case: TransferCaseSpec,
) -> dict[str, object] | None:
    verdict = _mechanism_verdict_from_trace(trace, case)
    if verdict.anomaly_present:
        if case.causal_scope.uses_causal_component:
            return {"scope": case.causal_scope.value, "component": case.causal_component}
        return {
            "scope": "dependency_edge",
            "edge_source": case.edge_source,
            "edge_destination": case.edge_destination,
        }
    return None


def _oracle_projection(evidence: SourceMechanismEvidence | None) -> dict[str, object] | None:
    if evidence is None:
        return None
    return {
        "predicate": evidence.predicate,
        "source_table": evidence.source_table,
        "value_column": evidence.value_column,
        "threshold": evidence.threshold,
        "allowed_operations": list(evidence.allowed_operations),
        "normal_samples": evidence.normal.get("count"),
        "abnormal_samples": evidence.abnormal.get("count"),
    }


def _public_locus_projection_matches(
    projection: Mapping[str, object], case: TransferCaseSpec
) -> bool:
    if case.causal_scope.uses_causal_component:
        return projection.get("scope") == case.causal_scope.value and component_matches(
            str(projection.get("component")),
            str(case.causal_component),
            source_identities=_source_declared_identities(case),
        )
    return (
        projection.get("scope") == "dependency_edge"
        and component_matches(str(projection.get("edge_source")), str(case.edge_source))
        and component_matches(str(projection.get("edge_destination")), str(case.edge_destination))
    )


def _public_evaluation(
    evaluation: TransferEvaluation,
    citations: list[dict[str, object]],
) -> dict[str, object]:
    value = evaluation.model_dump(mode="json")
    value["semantic_adjudication_reason_codes"] = (
        list(
            dict.fromkeys(
                code
                for citation in citations
                if citation["execution_valid"] is True
                for verdict in citation["mechanism_verdicts"]
                for code in (
                    *verdict["baseline_rejection_codes"],
                    *verdict["anomaly_rejection_codes"],
                )
            )
        )
        if value["semantic_adjudication_required"] is True
        else []
    )
    for key in (
        "supporting_evidence_query_ids",
        "causal_locus_evidence_query_ids",
        "baseline_evidence_query_ids",
        "anomaly_evidence_query_ids",
        "mechanism_evidence_query_ids",
    ):
        value.pop(key, None)
    ordinal_fields = {
        "supporting_evidence_ordinals": [
            item["ordinal"]
            for item in citations
            if item["supports_causal_locus"] or item["supports_fault_mechanism"]
        ],
        "causal_locus_evidence_ordinals": [
            item["ordinal"] for item in citations if item["supports_causal_locus"]
        ],
        "baseline_evidence_ordinals": [
            item["ordinal"] for item in citations if item["supports_baseline_clear"]
        ],
        "anomaly_evidence_ordinals": [
            item["ordinal"] for item in citations if item["supports_anomaly_present"]
        ],
        "mechanism_evidence_ordinals": [
            item["ordinal"] for item in citations if item["supports_fault_mechanism"]
        ],
    }
    value.update(ordinal_fields)
    grounding = value.get("claim_grounding")
    if isinstance(grounding, dict):
        for claim, item in grounding.items():
            if not isinstance(item, dict):
                continue
            item.pop("supporting_query_ids", None)
            flag = (
                "supports_causal_locus" if claim == "causal_locus" else "supports_fault_mechanism"
            )
            item["supporting_evidence_ordinals"] = [
                citation["ordinal"] for citation in citations if citation[flag]
            ]
    return value


def _public_adjudicated_sensitivity(
    evaluation: TransferEvaluation,
    citations: list[dict[str, object]],
    *,
    supporting_evidence_ordinals: list[int] | None = None,
) -> dict[str, object]:
    deterministic = _public_evaluation(evaluation, citations)
    return _adjudicated_sensitivity_projection(
        deterministic,
        supporting_evidence_ordinals=supporting_evidence_ordinals,
    )


def _adjudicated_sensitivity_projection(
    evaluation: Mapping[str, object],
    *,
    supporting_evidence_ordinals: list[int] | None = None,
) -> dict[str, object]:
    return {
        "evidence_sufficient": evaluation.get("required_evidence_covered") is True,
        "efficiency_eligible": evaluation.get("efficiency_eligible") is True,
        "auditable_completion": evaluation.get("auditable_completion") is True,
        "success": evaluation.get("success") is True,
        "correct_completion_tool_calls": evaluation.get("correct_completion_tool_calls"),
        "tool_calls_through_required_evidence": evaluation.get(
            "tool_calls_through_required_evidence"
        ),
        "rows_returned_through_required_evidence": evaluation.get(
            "rows_returned_through_required_evidence"
        ),
        "supporting_evidence_ordinals": (
            list(supporting_evidence_ordinals)
            if supporting_evidence_ordinals is not None
            else evaluation.get("supporting_evidence_ordinals")
        ),
    }


def _validate_public_adjudication(
    payload: Mapping[str, object],
    deterministic: Mapping[str, object],
) -> None:
    adjudication = _mapping(payload, "adjudication")
    effective = _mapping(payload, "adjudicated_sensitivity")
    status = adjudication.get("status")
    if status == "not-required":
        if deterministic.get(
            "semantic_adjudication_required"
        ) is True or effective != _adjudicated_sensitivity_projection(deterministic):
            raise ValueError("public non-candidate adjudication drifted")
        return
    if status not in {"accepted", "rejected"}:
        raise ValueError("public semantic adjudication status is invalid")
    if deterministic.get("semantic_adjudication_required") is not True:
        raise ValueError("public semantic adjudication bypasses its deterministic trigger")
    candidate = adjudication.get("candidate_sha256")
    if not isinstance(candidate, str) or len(candidate) != 64:
        raise ValueError("public semantic adjudication candidate hash is malformed")
    decisions = [
        SemanticJudgeDecision.model_validate(item)
        for item in _mapping_list(adjudication, "judge_decisions")
    ]
    if len(decisions) != 2 or {item.judge_model for item in decisions} != set(JUDGE_MODELS):
        raise ValueError("public semantic adjudication lacks both frozen judges")
    if any(item.candidate_sha256 != candidate for item in decisions):
        raise ValueError("public judge decision candidate binding drifted")
    observed = {item.evidence_sufficient for item in decisions}
    human_value = adjudication.get("human_decision")
    if human_value is not None and not isinstance(human_value, Mapping):
        raise ValueError("public human adjudication decision is malformed")
    human = (
        HumanAdjudicationDecision.model_validate(human_value)
        if isinstance(human_value, Mapping)
        else None
    )
    if len(observed) == 1:
        if human is not None:
            raise ValueError("public unanimous adjudication has a human tiebreak")
        sufficient = next(iter(observed))
        basis = "unanimous-judge-sufficient" if sufficient else "unanimous-judge-insufficient"
        support = sorted(
            {ordinal for decision in decisions for ordinal in decision.supporting_evidence_ordinals}
        )
    else:
        if human is None or human.candidate_sha256 != candidate:
            raise ValueError("public judge disagreement lacks its human tiebreak")
        sufficient = human.evidence_sufficient
        basis = "human-tiebreak"
        support = list(human.supporting_evidence_ordinals)
    if (
        adjudication.get("basis") != basis
        or adjudication.get("evidence_sufficient") is not sufficient
        or adjudication.get("supporting_evidence_ordinals") != support
        or status != ("accepted" if sufficient else "rejected")
    ):
        raise ValueError("public semantic adjudication decision does not replay")
    citations = _mapping_list(payload, "citations")
    if any(
        ordinal < 1
        or ordinal > len(citations)
        or citations[ordinal - 1].get("execution_valid") is not True
        for ordinal in support
    ):
        raise ValueError("public semantic adjudication cites invalid evidence")
    expected = _adjudicated_sensitivity_projection(deterministic)
    if sufficient:
        call_ordinals = [
            int(call_ordinal)
            for ordinal in support
            for call_ordinal in citations[ordinal - 1]["matching_tool_call_ordinals"]
        ]
        if not call_ordinals:
            raise ValueError("public accepted adjudication has no cited execution")
        support_call = max(call_ordinals)
        calls = _mapping_list(payload, "tool_calls")[:support_call]
        if not all(isinstance(call.get("database_load"), Mapping) for call in calls):
            raise ValueError("public accepted adjudication lacks database-load accounting")
        rows = sum(int(_mapping(call, "database_load")["rows_returned"]) for call in calls)
        expected.update(
            {
                "evidence_sufficient": True,
                "efficiency_eligible": True,
                "auditable_completion": True,
                "success": True,
                "correct_completion_tool_calls": len(_mapping_list(payload, "tool_calls")),
                "tool_calls_through_required_evidence": support_call,
                "rows_returned_through_required_evidence": rows,
            }
        )
        expected["supporting_evidence_ordinals"] = support
    if effective != expected:
        raise ValueError("public adjudicated evaluation does not replay")


def _validate_query_summary(
    summary: Mapping[str, object], *, allow_annotations: bool = False
) -> None:
    expected = {"tool_name", "input", "error", "result"}
    if allow_annotations:
        expected |= {
            "ordinal",
            "database_load",
            "mechanism_verdict",
            "causal_locus_projection",
        }
    if set(summary) != expected:
        raise ValueError("public query summary has an unexpected shape")
    if summary.get("tool_name") not in {
        "execute_sql",
        "describe_table",
        "search_table_semantics",
        "query_semantic_graph",
        *NATIVE_EVIDENCE_OPERATIONS,
    }:
        raise ValueError("public query summary exposes an unsupported tool")
    if not isinstance(summary.get("input"), Mapping) or not isinstance(summary.get("error"), bool):
        raise ValueError("public query summary input or error flag is malformed")
    _validate_public_input(
        str(summary["tool_name"]),
        summary["input"],
        errored=summary["error"] is True,
    )
    result = summary.get("result")
    if result is not None:
        if not isinstance(result, Mapping) or set(result) != {
            "columns",
            "row_count",
            "truncated",
            "sha256",
        }:
            raise ValueError("public query result summary is malformed")
        if not isinstance(result.get("sha256"), str) or len(str(result["sha256"])) != 64:
            raise ValueError("public query result hash is malformed")
        if (
            not isinstance(result.get("columns"), list)
            or not all(isinstance(column, str) for column in result["columns"])
            or _strict_int(result.get("row_count")) is None
            or int(result["row_count"]) < 0
            or not isinstance(result.get("truncated"), bool)
        ):
            raise ValueError("public query result metadata is malformed")
    if allow_annotations:
        load = summary.get("database_load")
        if load is not None:
            DatabaseLoad.model_validate(load)
        verdict = summary.get("mechanism_verdict")
        if verdict is not None:
            if not isinstance(verdict, Mapping):
                raise ValueError("public mechanism verdict is malformed")
            ClaimVerdict.model_validate(verdict)
        locus = summary.get("causal_locus_projection")
        if locus is not None and not _valid_public_locus_shape(locus):
            raise ValueError("public causal locus projection is malformed")


def _validate_public_input(
    tool_name: str,
    value: Mapping[str, object],
    *,
    errored: bool,
) -> None:
    if tool_name in NATIVE_EVIDENCE_OPERATIONS:
        if (
            not set(value) <= set(NATIVE_QUERY_INPUT_KEYS)
            or not all(
                isinstance(item, str) or _strict_int(item) is not None for item in value.values()
            )
            or (not errored and not isinstance(value.get("operation"), str))
        ):
            raise ValueError("public native query input is malformed")
        return
    if tool_name == "execute_sql":
        if set(value) not in ({"query"}, {"sql"}) or not all(
            isinstance(item, str) and item.strip() for item in value.values()
        ):
            raise ValueError("public SQL input is malformed")
        return
    if tool_name == "query_semantic_graph":
        allowed = {
            "view",
            "src_type",
            "src_id",
            "dst_type",
            "dst_id",
            "rel_type",
            "provenance",
            "entity_type",
            "entity_id",
            "limit",
        }
        if not value or not set(value) <= allowed:
            raise ValueError("public Semantic Graph input is malformed")
        return
    if tool_name == "describe_table":
        if set(value) != {"table"} or not isinstance(value.get("table"), str):
            raise ValueError("public table description input is malformed")
        return
    if tool_name == "search_table_semantics" and (
        not value
        or not set(value) <= {"query", "signal_type", "limit"}
        or not isinstance(value.get("query"), str)
    ):
        raise ValueError("public semantic search input is malformed")


def _public_query_execution_valid(summary: Mapping[str, object]) -> bool:
    tool_name = summary.get("tool_name")
    if tool_name not in {"execute_sql", "query_semantic_graph", *NATIVE_EVIDENCE_OPERATIONS}:
        return False
    if summary.get("error") is not False:
        return False
    result = summary.get("result")
    if not isinstance(result, Mapping) or result.get("truncated") is not False:
        return False
    if tool_name in NATIVE_EVIDENCE_OPERATIONS:
        # Same rule the private scorer applies: a discovery operation reports
        # what exists, not what happened, so it cannot carry a causal claim.
        input_value = summary.get("input")
        operation = str(input_value.get("operation")) if isinstance(input_value, Mapping) else ""
        return operation in NATIVE_EVIDENCE_OPERATIONS[str(tool_name)]
    if tool_name == "execute_sql":
        input_value = summary.get("input")
        if not isinstance(input_value, Mapping):
            return False
        query = str(input_value.get("query") or input_value.get("sql") or "")
        return is_evidence_sql(query)
    return True


def _valid_public_locus_shape(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    try:
        scope = CausalScope(str(value.get("scope")))
    except ValueError:
        return False
    # Every single-entity scope projects the same shape, so a scope added to the
    # ontology stays publishable without another edit here.
    if scope.uses_causal_component:
        return set(value) == {"scope", "component"} and isinstance(value.get("component"), str)
    return set(value) == {"scope", "edge_source", "edge_destination"} and all(
        isinstance(value.get(key), str) for key in ("edge_source", "edge_destination")
    )


def _reject_private_fields(value: object) -> None:
    forbidden = {
        "responses",
        "rows",
        "query_id",
        "run_id",
        "elapsed_seconds",
        "error_text",
        "local_path",
        "endpoint",
    }
    if isinstance(value, Mapping):
        overlap = forbidden & set(value)
        if overlap:
            raise ValueError(f"public transfer artifact contains private fields: {sorted(overlap)}")
        for item in value.values():
            _reject_private_fields(item)
    elif isinstance(value, list):
        for item in value:
            _reject_private_fields(item)
    elif isinstance(value, str) and (
        "/Users/" in value
        or "/private/tmp/" in value
        or ".maas.aliyuncs.com" in value
        or "localhost:" in value
        or "127.0.0.1:" in value
    ):
        raise ValueError("public transfer artifact contains a local or tenant-specific value")


def _mapping(value: Mapping[str, object], key: str) -> Mapping[str, object]:
    item = value.get(key)
    if not isinstance(item, Mapping):
        raise ValueError(f"public transfer field is not an object: {key}")
    return item


def _mapping_list(value: Mapping[str, object], key: str) -> list[Mapping[str, object]]:
    items = value.get(key)
    if not isinstance(items, list) or not all(isinstance(item, Mapping) for item in items):
        raise ValueError(f"public transfer field is not an object list: {key}")
    return list(items)


def _string_list(value: object) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _strict_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
