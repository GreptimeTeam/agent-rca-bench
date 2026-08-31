from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from collections.abc import Mapping
from pathlib import Path
from statistics import median

from semantic_rca_bench.contracts import (
    AgentRun,
    ApiTransport,
    CausalScope,
    DatabaseLoad,
    EvidenceClaimType,
    ToolTrace,
)
from semantic_rca_bench.datasets.openrca2_transfer import TransferCaseSpec
from semantic_rca_bench.evaluation import component_matches
from semantic_rca_bench.evidence import is_evidence_sql, is_valid_evidence_trace
from semantic_rca_bench.report import _estimated_api_cost, _raw_input_breakdown
from semantic_rca_bench.transfer_scorer import (
    ClaimVerdict,
    TransferEvaluation,
    _mechanism_verdict_from_trace,
)

ARTIFACT_SCHEMA_VERSION = 1


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
    if case.causal_scope is CausalScope.COMPONENT:
        locus_match = (
            diagnosis is not None
            and isinstance(diagnosis.get("causal_component"), str)
            and case.causal_component is not None
            and component_matches(str(diagnosis["causal_component"]), case.causal_component)
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
    mechanism_match = (
        diagnosis is not None and diagnosis.get("mechanism_code") == case.mechanism_code.value
    )
    allowed_operations = set(case.mechanism_evidence.allowed_operations)
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
    mechanism_evidence_match = baseline_present and anomaly_present
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
            verdict.baseline_clear or verdict.anomaly_present
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
    locus_evidence_match = bool(expected_locus_ordinals)
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
    required_evidence_covered = typed_evidence and mechanism_evidence_match and locus_evidence_match
    citations_execution_valid = (
        bool(citations)
        and references_unique
        and all(citation.get("execution_valid") is True for citation in citations)
    )
    efficiency_eligible = diagnosis_correct and required_evidence_covered and execution_reliability
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
    checks = {
        "runner contract mismatch": runner_contract_match,
        "causal scope mismatch": scope_match,
        "causal locus mismatch": locus_match,
        "fault category mismatch": category_match,
        "mechanism code mismatch": mechanism_match,
        "causal locus lacks incident-local evidence": locus_evidence_match,
        "baseline-clear evidence is missing": baseline_present,
        "anomalous mechanism evidence is missing": anomaly_present,
        "fault mechanism lacks complete transition evidence": mechanism_evidence_match,
        "required evidence claims are missing": typed_evidence,
        "citation integrity failed": citations_execution_valid,
        "execution reliability failed": execution_reliability,
    }
    evaluation = {
        "diagnosis_correct": diagnosis_correct,
        "causal_locus_match": locus_match,
        "causal_scope_match": scope_match,
        "fault_category_match": category_match,
        "mechanism_code_match": mechanism_match,
        "causal_operation_match": causal_operation_match,
        "causal_locus_evidence_match": locus_evidence_match,
        "baseline_evidence_match": baseline_present,
        "anomaly_evidence_match": anomaly_present,
        "mechanism_evidence_match": mechanism_evidence_match,
        "required_evidence_covered": required_evidence_covered,
        "citations_execution_valid": citations_execution_valid,
        "execution_reliability": execution_reliability,
        "efficiency_eligible": efficiency_eligible,
        "auditable_completion": efficiency_eligible and citations_execution_valid,
        "success": efficiency_eligible and citations_execution_valid,
        "cited_evidence_count": len(citations),
        "valid_evidence_count": sum(
            citation.get("execution_valid") is True for citation in citations
        ),
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
        "failure_reasons": [reason for reason, passed in checks.items() if not passed],
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


def build_measurement_artifact(
    private_report: dict[str, object],
    protocol_path: Path,
) -> dict[str, object]:
    from semantic_rca_bench.transfer_formal import validate_private_report
    from semantic_rca_bench.transfer_protocol import load_transfer_protocol, sha256_file

    protocol, selection, _ = load_transfer_protocol(protocol_path)
    validate_private_report(
        private_report,
        protocol,
        protocol_path,
        selection,
        require_complete=True,
    )
    if private_report.get("phase") != "measurement":
        raise ValueError("development pilot cannot be exported as a measurement artifact")
    source_by_case = {
        str(_mapping(source, "case")["opaque_case_id"]): source
        for source in _mapping_list(private_report, "source_audits")
    }
    specs = {case.opaque_case_id: case for case in selection.selected_cases}
    models = {model.model: model for model in protocol.models}
    public_runs = []
    for item in _mapping_list(private_report, "runs"):
        case = specs[str(item["case_id"])]
        run = AgentRun.model_validate(item.get("run"))
        evaluation = TransferEvaluation.model_validate(item.get("evaluation"))
        load = DatabaseLoad.model_validate(item.get("database_load"))
        public = sanitize_transfer_run(run, evaluation, case, load)
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
    model_reports = _model_reports(public_runs, [model.model for model in protocol.models])
    payload = {
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "artifact_type": "semantic-rca-openrca2-transfer-measurement",
        "publication_status": (
            "sanitized measurement result; contains no provider payloads or raw telemetry rows"
        ),
        "license": {
            "benchmark_code": "Apache-2.0",
            "source_dataset": (
                "dataset card says Apache-2.0; paper says CC-BY-SA-4.0; telemetry rows "
                "are not redistributed"
            ),
            "artifact_schema_and_derived_aggregates": "Apache-2.0",
        },
        "benchmark_protocol": private_report["benchmark_protocol"],
        "formal_protocol": private_report["formal_protocol"],
        "pilot_gate": private_report["pilot_gate"],
        "sources": public_sources,
        "runs": public_runs,
        "model_reports": model_reports,
        "inference": _public_inference(protocol.inference),
        "sanitization": {
            "excluded": [
                "provider responses, reasoning text, signatures, and provider tool-call IDs",
                "run IDs, query IDs, elapsed wall time, ports, endpoints, and local paths",
                "raw telemetry rows and provider error text",
                "free-form citation claims and diagnosis explanations",
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
    from semantic_rca_bench.transfer_protocol import (
        formal_schedule,
        load_transfer_protocol,
        sha256_file,
    )

    protocol, selection, _ = load_transfer_protocol(protocol_path)
    if (
        artifact.get("artifact_schema_version") != ARTIFACT_SCHEMA_VERSION
        or artifact.get("artifact_type") != "semantic-rca-openrca2-transfer-measurement"
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
        validate_public_transfer_run(
            _mapping(item, "run"),
            specs[str(item["case_id"])],
            expected_model=model.model,
            expected_transport=model.api_transport,
            expected_reasoning_effort=model.reasoning_effort,
            expected_max_output_tokens=model.max_output_tokens,
            max_tool_calls=protocol.max_tool_calls,
        )
    expected_reports = _model_reports(runs, [model.model for model in protocol.models])
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
        "semantic_coverage": audit.get("semantic_coverage"),
        "no_model_gates": audit.get("no_model_gates"),
    }


def _public_inference(inference: object) -> dict[str, object]:
    return {
        "estimand": "semantic_graph - raw within the same model and case",
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
    }


def _model_reports(runs: list[Mapping[str, object]], model_names: list[str]) -> dict[str, object]:
    reports: dict[str, object] = {}
    primary_p_values: list[tuple[str, str, float]] = []
    for model in model_names:
        model_runs = [item for item in runs if item["model"] == model]
        pairs: dict[tuple[str, int], dict[str, Mapping[str, object]]] = defaultdict(dict)
        for item in model_runs:
            pairs[(str(item["case_id"]), int(item["repetition"]))][str(item["visibility"])] = item
        pair_deltas: dict[str, list[dict[str, object]]] = defaultdict(list)
        eligibility_disposition = Counter()
        for (case_id, repetition), values in sorted(pairs.items()):
            if set(values) != {"raw", "semantic_graph"}:
                continue
            raw = _mapping(values["raw"], "run")
            graph = _mapping(values["semantic_graph"], "run")
            raw_eval = _mapping(raw, "evaluation")
            graph_eval = _mapping(graph, "evaluation")
            eligible = (
                raw_eval.get("efficiency_eligible") is True
                and graph_eval.get("efficiency_eligible") is True
            )
            raw_eligible = raw_eval.get("efficiency_eligible") is True
            graph_eligible = graph_eval.get("efficiency_eligible") is True
            eligibility_status = (
                "both"
                if raw_eligible and graph_eligible
                else "raw_only"
                if raw_eligible
                else "semantic_graph_only"
                if graph_eligible
                else "neither"
            )
            eligibility_disposition[eligibility_status] += 1
            delta = {
                "case_id": case_id,
                "repetition": repetition,
                "eligible": eligible,
                "eligibility_status": eligibility_status,
                "rows_returned": (
                    int(_mapping(graph, "database_load")["rows_returned"])
                    - int(_mapping(raw, "database_load")["rows_returned"])
                    if eligible
                    else None
                ),
                "correct_completion_tool_calls": (
                    int(graph_eval["correct_completion_tool_calls"])
                    - int(raw_eval["correct_completion_tool_calls"])
                    if eligible
                    else None
                ),
            }
            pair_deltas[case_id].append(delta)
        case_effects = []
        for case_id, deltas in sorted(pair_deltas.items()):
            eligible = [item for item in deltas if item["eligible"]]
            case_effects.append(
                {
                    "case_id": case_id,
                    "eligible_repetitions": len(eligible),
                    "rows_returned": _median_or_none([item["rows_returned"] for item in eligible]),
                    "correct_completion_tool_calls": _median_or_none(
                        [item["correct_completion_tool_calls"] for item in eligible]
                    ),
                }
            )
        metrics = {}
        for metric in ("rows_returned", "correct_completion_tool_calls"):
            values = [item[metric] for item in case_effects if item[metric] is not None]
            p_value = _sign_test(values)
            metrics[metric] = {
                "eligible_cases": len(values),
                "case_median_delta": _median_or_none(values),
                "negative_cases": sum(value < 0 for value in values),
                "tied_cases": sum(value == 0 for value in values),
                "positive_cases": sum(value > 0 for value in values),
                "sign_test_two_sided_p": p_value,
                "holm_adjusted_p": None,
                "multiplicity_family_size": 12,
            }
            if p_value is not None:
                primary_p_values.append((model, metric, p_value))
        reports[model] = {
            "runs": len(model_runs),
            "valid_completion": {
                visibility: sum(
                    _mapping(_mapping(item, "run"), "evaluation").get("auditable_completion")
                    is True
                    for item in model_runs
                    if item["visibility"] == visibility
                )
                for visibility in ("raw", "semantic_graph")
            },
            "efficiency_eligibility": {
                "by_treatment": {
                    visibility: sum(
                        _mapping(_mapping(item, "run"), "evaluation").get("efficiency_eligible")
                        is True
                        for item in model_runs
                        if item["visibility"] == visibility
                    )
                    for visibility in ("raw", "semantic_graph")
                },
                "paired_disposition": {
                    status: eligibility_disposition[status]
                    for status in ("both", "raw_only", "semantic_graph_only", "neither")
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
            "primary_metrics": metrics,
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
    _apply_holm(reports, primary_p_values, family_size=12)
    return reports


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
) -> None:
    previous = 0.0
    for rank, (model, metric, value) in enumerate(sorted(values, key=lambda item: item[2])):
        adjusted = min(1.0, max(previous, value * (family_size - rank)))
        previous = adjusted
        reports[model]["primary_metrics"][metric]["holm_adjusted_p"] = adjusted


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
    if output is not None:
        result_payload = {
            "columns": output.get("columns"),
            "rows": rows,
            "truncated": output.get("truncated"),
        }
        result = {
            "columns": output.get("columns"),
            "row_count": len(rows) if isinstance(rows, list) else None,
            "truncated": output.get("truncated"),
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
        if case.causal_scope is CausalScope.COMPONENT:
            return {"scope": "component", "component": case.causal_component}
        return {
            "scope": "dependency_edge",
            "edge_source": case.edge_source,
            "edge_destination": case.edge_destination,
        }
    return None


def _public_locus_projection_matches(
    projection: Mapping[str, object], case: TransferCaseSpec
) -> bool:
    if case.causal_scope is CausalScope.COMPONENT:
        return projection.get("scope") == "component" and component_matches(
            str(projection.get("component")), str(case.causal_component)
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
    for key in (
        "supporting_evidence_query_ids",
        "causal_locus_evidence_query_ids",
        "baseline_evidence_query_ids",
        "anomaly_evidence_query_ids",
        "mechanism_evidence_query_ids",
    ):
        value.pop(key, None)
    value["supporting_evidence_ordinals"] = [
        item["ordinal"]
        for item in citations
        if item["supports_causal_locus"] or item["supports_fault_mechanism"]
    ]
    value["causal_locus_evidence_ordinals"] = [
        item["ordinal"] for item in citations if item["supports_causal_locus"]
    ]
    value["baseline_evidence_ordinals"] = [
        item["ordinal"] for item in citations if item["supports_baseline_clear"]
    ]
    value["anomaly_evidence_ordinals"] = [
        item["ordinal"] for item in citations if item["supports_anomaly_present"]
    ]
    value["mechanism_evidence_ordinals"] = [
        item["ordinal"] for item in citations if item["supports_fault_mechanism"]
    ]
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
    }:
        raise ValueError("public query summary exposes an unsupported tool")
    if not isinstance(summary.get("input"), Mapping) or not isinstance(summary.get("error"), bool):
        raise ValueError("public query summary input or error flag is malformed")
    _validate_public_input(str(summary["tool_name"]), summary["input"])
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


def _validate_public_input(tool_name: str, value: Mapping[str, object]) -> None:
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
    if summary.get("tool_name") not in {"execute_sql", "query_semantic_graph"}:
        return False
    if summary.get("error") is not False:
        return False
    result = summary.get("result")
    if not isinstance(result, Mapping) or result.get("truncated") is not False:
        return False
    if summary.get("tool_name") == "execute_sql":
        input_value = summary.get("input")
        if not isinstance(input_value, Mapping):
            return False
        query = str(input_value.get("query") or input_value.get("sql") or "")
        return is_evidence_sql(query)
    return True


def _valid_public_locus_shape(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    if value.get("scope") == "component":
        return set(value) == {"scope", "component"} and isinstance(value.get("component"), str)
    if value.get("scope") == "dependency_edge":
        return set(value) == {"scope", "edge_source", "edge_destination"} and all(
            isinstance(value.get(key), str) for key in ("edge_source", "edge_destination")
        )
    return False


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
