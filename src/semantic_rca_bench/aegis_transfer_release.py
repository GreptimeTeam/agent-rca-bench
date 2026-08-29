from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from statistics import median

from semantic_rca_bench.aegis_transfer_formal import (
    formal_source_semantic_sha256,
    period_graph_replay_semantics,
    period_raw_replay_semantics,
    validate_formal_report,
)
from semantic_rca_bench.aegis_transfer_protocol import (
    DEFAULT_PROTOCOL_FIXTURE,
    AegisTransferProtocolFixture,
    evaluate_transfer_protocol_run,
    load_transfer_protocol_fixture,
)
from semantic_rca_bench.aegis_transfer_scorer import (
    AegisTransferEvaluation,
    AegisTransferScorerFixture,
    load_transfer_scorer_fixture,
    sha256_file,
    source_transfer_audit_sha256,
)
from semantic_rca_bench.contracts import AgentRun
from semantic_rca_bench.evaluation import is_valid_evidence_trace
from semantic_rca_bench.report import MODEL_PRICING, _estimated_api_cost, _raw_input_breakdown

ARTIFACT_SCHEMA_VERSION = 1
DEFAULT_MEASUREMENT_SCORER_FIXTURE = Path("fixtures/reference/aegis-transfer-v26-scorer.json")


def build_measurement_artifact(
    run_report: dict[str, object],
    source_audit: dict[str, object],
    scorer_audit: dict[str, object],
    protocol_audit: dict[str, object],
    scorer_fixture: AegisTransferScorerFixture,
    scorer_fixture_path: Path,
    protocol_fixture: AegisTransferProtocolFixture,
    protocol_fixture_path: Path,
    *,
    private_input_sha256: Mapping[str, str] | None = None,
) -> dict[str, object]:
    _validate_measurement_bindings(
        run_report,
        source_audit,
        scorer_audit,
        protocol_audit,
        scorer_fixture,
        scorer_fixture_path,
        protocol_fixture,
        protocol_fixture_path,
    )
    source = _source_payload(source_audit)
    pricing_snapshot = _mapping(run_report, "pricing_snapshot")
    runs = _measurement_run_payloads(
        run_report,
        scorer_fixture,
        protocol_fixture,
        pricing_snapshot,
    )
    model_reports = {
        model.model: _measurement_model_summary(
            [run for run in runs if run["model"] == model.model],
            model.model,
            pricing=_mapping(pricing_snapshot, model.model),
        )
        for model in protocol_fixture.models
    }
    payload = {
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "artifact_type": "aegis-transfer-measurement-result",
        "publication_status": (
            "sanitized measurement result; contains no source telemetry rows or provider payloads"
        ),
        "analysis_role": "measurement",
        "license": {
            **_mapping(source_audit, "license"),
            "artifact_schema_and_benchmark_metadata": "Apache-2.0",
            "derived_source_facts": (
                "Source dataset record declares CC-BY-4.0; reviewer artifact data coverage "
                "remains unclear."
            ),
            "source_telemetry_redistributed": False,
        },
        "source": source,
        "scorer": {
            "revision": scorer_fixture.scorer_revision,
            "agent_case_id": scorer_fixture.agent_case_id,
            "ground_truth": scorer_fixture.ground_truth.model_dump(mode="json"),
            "normal_window": list(scorer_fixture.normal_window),
            "abnormal_window": list(scorer_fixture.abnormal_window),
            "mechanism_evidence": scorer_fixture.mechanism_evidence.model_dump(
                mode="json", exclude_none=True
            ),
            "no_model_gates": _mapping(scorer_audit, "no_model_gates"),
        },
        "experiment": {
            "benchmark_protocol": _mapping(run_report, "benchmark_protocol"),
            "formal_protocol": _mapping(run_report, "formal_protocol"),
            "pricing_snapshot": pricing_snapshot,
            "case": _mapping(run_report, "case"),
            "graph_window_contract": _mapping(run_report, "graph_window_contract"),
            "semantic_coverage": _mapping(run_report, "semantic_coverage"),
            "schedule": _list(run_report, "schedule"),
            "execution": _mapping(run_report, "execution"),
            "runs": runs,
            "model_reports": model_reports,
            "cross_model_pooling": False,
        },
        "sanitization": {
            "excluded": [
                "provider responses and thinking",
                "run IDs and provider tool-call IDs",
                "diagnosis explanations, alternatives, and evidence claim text",
                "non-mechanism SQL result rows",
                "query IDs, elapsed timings, and provider error text",
                "local paths, ports, process metadata, and environment data",
                "source telemetry rows, label files, and archives",
            ],
            "included_derived_data": [
                "normalized service-call edge sets and counts",
                "mechanism query schemas, row counts, and result hashes",
                "parsed diagnosis fields and deterministic scorer outputs",
                "query and row counts plus aggregate token and cache usage",
                "within-model paired treatment deltas",
            ],
        },
    }
    return {
        **payload,
        "integrity": {
            "semantic_payload_sha256": canonical_sha256(payload),
            "source_semantic_sha256": canonical_sha256(source),
            "private_input_sha256": dict(sorted((private_input_sha256 or {}).items())),
            "semantic_hash_scope": (
                "all artifact fields except integrity; excludes private file hashes and "
                "run-local IDs, timings, ports, processes, paths, and provider error text"
            ),
        },
    }


def build_measurement_artifact_from_files(
    run_path: Path,
    source_audit_path: Path,
    scorer_audit_path: Path,
    protocol_audit_path: Path,
    scorer_fixture_path: Path = DEFAULT_MEASUREMENT_SCORER_FIXTURE,
    protocol_fixture_path: Path = DEFAULT_PROTOCOL_FIXTURE,
) -> dict[str, object]:
    inputs = {
        "run_report": run_path,
        "source_audit": source_audit_path,
        "scorer_audit": scorer_audit_path,
        "protocol_audit": protocol_audit_path,
        "scorer_fixture": scorer_fixture_path,
        "protocol_fixture": protocol_fixture_path,
    }
    scorer_fixture = load_transfer_scorer_fixture(scorer_fixture_path)
    protocol_fixture = load_transfer_protocol_fixture(protocol_fixture_path)
    return build_measurement_artifact(
        _load_object(run_path),
        _load_object(source_audit_path),
        _load_object(scorer_audit_path),
        _load_object(protocol_audit_path),
        scorer_fixture,
        scorer_fixture_path,
        protocol_fixture,
        protocol_fixture_path,
        private_input_sha256={name: sha256_file(path) for name, path in inputs.items()},
    )


def canonical_sha256(value: object) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _source_payload(source_audit: dict[str, object]) -> dict[str, object]:
    case = _mapping(source_audit, "case")
    equality = _mapping(source_audit, "edge_equality")
    mechanism = _mapping(source_audit, "mechanism_evidence")
    selection = _mapping(source_audit, "selection_audit")
    selected = selection.get("active_frozen_selection")
    if selected is None:
        selected = selection.get("selection")
    return {
        "dataset_revision": source_audit.get("dataset_revision"),
        "adapter_revision": source_audit.get("adapter_revision"),
        "pinned_source": _mapping(source_audit, "pinned_source"),
        "selection": selected,
        "frozen_selection_gate_passed": _mapping(selection, "frozen_selection_gate").get("pass"),
        "case": {
            "agent_facing": _mapping(case, "agent_facing"),
            "source_mapping": _mapping(case, "source_mapping"),
            "normal_window": case.get("normal_window"),
            "abnormal_window": case.get("abnormal_window"),
            "ground_truth_services": case.get("ground_truth_services"),
            "declared_edge": case.get("declared_edge"),
            "fault_type": case.get("fault_type"),
        },
        "greptimedb": _mapping(source_audit, "greptimedb"),
        "ingestion": _ingestion_payload(_mapping(source_audit, "ingestion")),
        "edge_equality": {
            "raw_edge_query": equality.get("raw_edge_query"),
            "graph_edge_query": equality.get("graph_edge_query"),
            "period_raw_replay": period_raw_replay_semantics(equality),
            "period_raw_replay_exact": equality.get("period_raw_replay_exact"),
            "period_graph_replay": period_graph_replay_semantics(equality),
            "period_graph_replay_exact": equality.get("period_graph_replay_exact"),
            "graph_window_strategy_proof": equality.get("graph_window_strategy_proof"),
            "normalized_raw_edges": equality.get("normalized_raw_edges"),
            "normalized_graph_edges": equality.get("normalized_graph_edges"),
            "raw_edge_set_sha256": equality.get("raw_edge_set_sha256"),
            "graph_edge_set_sha256": equality.get("graph_edge_set_sha256"),
            "exact_edge_set_equality": equality.get("exact_edge_set_equality"),
            "window_contract": equality.get("window_contract"),
        },
        "mechanism_evidence": {
            "predicate": mechanism.get("predicate"),
            "query": mechanism.get("query"),
            "normalized_result": mechanism.get("normalized_result"),
            "expected_result": mechanism.get("expected_result"),
            "declared_edge": mechanism.get("declared_edge"),
            "declared_edge_match": mechanism.get("declared_edge_match"),
            "original_method": mechanism.get("original_method"),
            "replacement_method": mechanism.get("replacement_method"),
            **(
                {"span_name": mechanism["span_name"]}
                if mechanism.get("span_name") is not None
                else {}
            ),
            **(
                {"declared_delay_ns": mechanism["declared_delay_ns"]}
                if mechanism.get("declared_delay_ns") is not None
                else {}
            ),
            **(
                {"observable": mechanism["observable"]}
                if mechanism.get("observable") is not None
                else {}
            ),
            **(
                {"service_name": mechanism["service_name"]}
                if mechanism.get("service_name") is not None
                else {}
            ),
            **(
                {"method_name": mechanism["method_name"]}
                if mechanism.get("method_name") is not None
                else {}
            ),
            "evidence_match": mechanism.get("evidence_match"),
            "pass": mechanism.get("pass"),
        },
        "semantic_coverage": _mapping(_mapping(source_audit, "semantic_surfaces"), "coverage"),
        "no_model_gates": _mapping(source_audit, "no_model_gates"),
    }


def _ingestion_payload(ingestion: dict[str, object]) -> dict[str, object]:
    identity = _mapping(ingestion, "source_identity")
    return {
        "source_row_counts": ingestion.get("source_row_counts"),
        "protocol_counts": ingestion.get("protocol_counts"),
        "expected_stored_row_counts": ingestion.get("expected_stored_row_counts"),
        "stored_row_counts": ingestion.get("stored_row_counts"),
        "stored_row_counts_match": ingestion.get("stored_row_counts_match"),
        "protocol_rejections_zero": ingestion.get("protocol_rejections_zero"),
        "id_remapping": ingestion.get("id_remapping"),
        "source_identity": {
            "service_counts_match": identity.get("service_counts_match"),
            "span_kind_counts_match": identity.get("span_kind_counts_match"),
            "status_code_counts_match": identity.get("status_code_counts_match"),
            "source_span_kind_counts": identity.get("source_span_kind_counts"),
            "stored_span_kind_counts": identity.get("stored_span_kind_counts"),
            "source_status_code_counts": identity.get("source_status_code_counts"),
            "stored_status_code_counts": identity.get("stored_status_code_counts"),
        },
    }


def _validate_measurement_bindings(
    run_report: dict[str, object],
    source_audit: dict[str, object],
    scorer_audit: dict[str, object],
    protocol_audit: dict[str, object],
    scorer_fixture: AegisTransferScorerFixture,
    scorer_fixture_path: Path,
    protocol_fixture: AegisTransferProtocolFixture,
    protocol_fixture_path: Path,
) -> None:
    validate_formal_report(
        run_report,
        scorer_fixture,
        scorer_fixture_path,
        protocol_fixture,
        protocol_fixture_path,
        require_complete=True,
    )
    source_gates = source_audit.get("no_model_gates")
    scorer_gates = scorer_audit.get("no_model_gates")
    protocol_gates = protocol_audit.get("no_model_gates")
    if any(
        not isinstance(gates, dict) or gates.get("all_passed") is not True
        for gates in (source_gates, scorer_gates, protocol_gates)
    ):
        raise ValueError("measurement artifact input no-model gates did not pass")
    bindings = _mapping(run_report, "execution_bindings")
    expected = {
        "source_transfer_audit_sha256": source_transfer_audit_sha256(source_audit),
        "source_semantic_sha256": formal_source_semantic_sha256(source_audit),
        "scorer_audit_sha256": canonical_sha256(scorer_audit),
        "protocol_audit_sha256": canonical_sha256(protocol_audit),
        "scorer_fixture_sha256": sha256_file(scorer_fixture_path),
        "protocol_fixture_sha256": sha256_file(protocol_fixture_path),
    }
    if bindings != expected:
        raise ValueError("measurement artifact execution bindings do not match input files")


def _measurement_run_payloads(
    run_report: dict[str, object],
    scorer_fixture: AegisTransferScorerFixture,
    protocol_fixture: AegisTransferProtocolFixture,
    pricing_snapshot: dict[str, object],
) -> list[dict[str, object]]:
    result = []
    for item in _list(run_report, "runs"):
        if not isinstance(item, dict):
            raise ValueError("formal run report contains a malformed run cell")
        run = AgentRun.model_validate(item.get("run"))
        recorded = AegisTransferEvaluation.model_validate(item.get("evaluation"))
        evaluated = evaluate_transfer_protocol_run(
            run,
            scorer_fixture,
            protocol_fixture,
            expected_model=str(item.get("model")),
        )
        if recorded.model_dump(mode="json") != evaluated.model_dump(mode="json"):
            raise ValueError("formal run evaluation does not match deterministic rescoring")
        payload = _run_payload(
            item,
            run,
            evaluated,
            pricing=_mapping(pricing_snapshot, run.model),
        )
        payload["cell_index"] = item.get("cell_index")
        payload["model_index"] = item.get("model_index")
        result.append(payload)
    return result


def _measurement_model_summary(
    runs: list[dict[str, object]],
    model: str,
    *,
    pricing: Mapping[str, object] | None = None,
) -> dict[str, object]:
    treatments = {}
    for visibility in ("raw", "table_semantics", "semantic_graph"):
        cells = [run for run in runs if run["visibility"] == visibility]
        treatment = {
            "runs": len(cells),
            "successful_runs": sum(
                _auditable_completion(_mapping(cell, "evaluation")) for cell in cells
            ),
            "efficiency_eligible_runs": sum(
                _efficiency_eligible(_mapping(cell, "evaluation")) for cell in cells
            ),
        }
        treatments[visibility] = treatment
    report = {
        "inference_role": "within-model case-level description",
        "successful_runs": sum(_auditable_completion(_mapping(run, "evaluation")) for run in runs),
        "total_runs": len(runs),
        "treatments": treatments,
        "paired_treatment_deltas": _paired_treatment_deltas(runs),
        "usage": _usage_summary(runs, model, pricing=pricing),
        "efficiency_eligible_runs": sum(
            _efficiency_eligible(_mapping(run, "evaluation")) for run in runs
        ),
    }
    return report


def _paired_treatment_deltas(runs: list[dict[str, object]]) -> dict[str, object]:
    by_cell = {(int(run["repetition"]), str(run["visibility"])): run for run in runs}
    output = {}
    for name, left, right in (
        ("table_semantics_minus_raw", "raw", "table_semantics"),
        ("semantic_graph_minus_table_semantics", "table_semantics", "semantic_graph"),
    ):
        pairs = []
        for repetition in sorted({int(run["repetition"]) for run in runs}):
            left_run = by_cell[(repetition, left)]
            right_run = by_cell[(repetition, right)]
            eligible = all(
                _efficiency_eligible(_mapping(run, "evaluation")) for run in (left_run, right_run)
            )
            pairs.append(
                {
                    "repetition": repetition,
                    "eligible": eligible,
                    "deltas": (_eligible_pair_deltas(left_run, right_run) if eligible else None),
                }
            )
        eligible_deltas = [pair["deltas"] for pair in pairs if pair["deltas"] is not None]
        output[name] = {
            "pairs": pairs,
            "eligible_pairs": len(eligible_deltas),
            "median_deltas": (
                {
                    field: median(float(delta[field]) for delta in eligible_deltas)
                    for field in (
                        "rows_returned",
                        "correct_completion_tool_calls",
                        "reported_total_tokens",
                    )
                }
                if eligible_deltas
                else None
            ),
        }
    return output


def _eligible_pair_deltas(left: dict[str, object], right: dict[str, object]) -> dict[str, int]:
    left_execution = _mapping(left, "execution")
    right_execution = _mapping(right, "execution")
    left_evaluation = _mapping(left, "evaluation")
    right_evaluation = _mapping(right, "evaluation")
    left_usage = _mapping(left, "usage")
    right_usage = _mapping(right, "usage")
    return {
        "rows_returned": int(_mapping(right_execution, "database_load")["rows_returned"])
        - int(_mapping(left_execution, "database_load")["rows_returned"]),
        "correct_completion_tool_calls": int(right_evaluation["correct_completion_tool_calls"])
        - int(left_evaluation["correct_completion_tool_calls"]),
        "reported_total_tokens": _reported_tokens(right_usage) - _reported_tokens(left_usage),
    }


def _reported_tokens(usage: dict[str, object]) -> int:
    return sum(
        int(usage.get(field, 0))
        for field in (
            "uncached_input_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "output_tokens",
        )
    )


def _run_payload(
    item: dict[str, object],
    run: AgentRun,
    evaluation: AegisTransferEvaluation,
    *,
    pricing: Mapping[str, object] | None = None,
) -> dict[str, object]:
    diagnosis = run.diagnosis
    traces_by_query_id: dict[str, list[object]] = {}
    for trace in run.tool_calls:
        if trace.query_id is not None:
            traces_by_query_id.setdefault(trace.query_id, []).append(trace)
    evidence = diagnosis.evidence if diagnosis is not None else []
    evidence_ids_unique = len({entry.query_id for entry in evidence}) == len(evidence)
    supporting_ids = set(evaluation.supporting_evidence_query_ids)
    causal_scope_ids = set(evaluation.causal_scope_evidence_query_ids)
    mechanism_ids = set(evaluation.mechanism_evidence_query_ids)
    citations = []
    mechanism_queries = []
    for index, entry in enumerate(evidence, start=1):
        matches = traces_by_query_id.get(entry.query_id, [])
        citation = {
            "ordinal": index,
            "execution_valid": (
                evidence_ids_unique
                and bool(entry.claim.strip())
                and is_valid_evidence_trace(matches)
            ),
            "supports_mechanism": entry.query_id in supporting_ids,
            "supports_causal_scope": entry.query_id in causal_scope_ids,
            "supports_fault_mechanism": entry.query_id in mechanism_ids,
            "claim_types": [claim.value for claim in entry.claim_types],
        }
        citations.append(citation)
        if entry.query_id in mechanism_ids and len(matches) == 1:
            trace = matches[0]
            output = trace.output if isinstance(trace.output, dict) else {}
            result_payload = {
                "columns": output.get("columns"),
                "rows": output.get("rows"),
                "truncated": output.get("truncated"),
            }
            rows = output.get("rows")
            mechanism_queries.append(
                {
                    "evidence_ordinal": index,
                    "query": trace.input.get("query") or trace.input.get("sql"),
                    "result": {
                        "columns": output.get("columns"),
                        "row_count": len(rows) if isinstance(rows, list) else None,
                        "truncated": output.get("truncated"),
                        "sha256": canonical_sha256(result_payload),
                    },
                }
            )
    uncached, cache_read, cache_creation, breakdown_complete = _raw_input_breakdown(
        run.model_dump(mode="json")
    )
    pricing = pricing or MODEL_PRICING.get(run.model)
    peak_cost = (
        _estimated_api_cost(run.model_dump(mode="json"), pricing) if pricing is not None else None
    )
    database_load = _mapping(item, "database_load")
    evaluation_payload = evaluation.model_dump(mode="json")
    for field in (
        "correct_completion_tool_calls",
        "tool_calls_through_mechanism_evidence",
        "rows_returned_through_mechanism_evidence",
    ):
        if evaluation_payload.get(field) is None:
            evaluation_payload.pop(field)
    evaluation_payload.pop("supporting_evidence_query_ids")
    evaluation_payload.pop("causal_scope_evidence_query_ids")
    evaluation_payload.pop("mechanism_evidence_query_ids")
    evaluation_payload["supporting_evidence_ordinals"] = [
        citation["ordinal"] for citation in citations if citation["supports_mechanism"] is True
    ]
    evaluation_payload["causal_scope_evidence_ordinals"] = [
        citation["ordinal"] for citation in citations if citation["supports_causal_scope"] is True
    ]
    evaluation_payload["mechanism_evidence_ordinals"] = [
        citation["ordinal"]
        for citation in citations
        if citation["supports_fault_mechanism"] is True
    ]
    diagnosis_payload = None
    if diagnosis is not None:
        diagnosis_payload = {
            "affected_component": diagnosis.affected_component,
            "causal_dependency": diagnosis.causal_dependency,
            "fault_category": diagnosis.fault_category.value,
            "fault_type": diagnosis.fault_type,
            "onset_time": diagnosis.onset_time,
            "confidence": diagnosis.confidence,
            "causal_scope": (
                diagnosis.causal_scope.value if diagnosis.causal_scope is not None else None
            ),
            "causal_operation": diagnosis.causal_operation,
            "mechanism_code": (
                diagnosis.mechanism_code.value if diagnosis.mechanism_code is not None else None
            ),
        }
    return {
        "repetition": int(item.get("repetition", 0)),
        "position": int(item.get("position", 0)),
        "visibility": run.visibility.value,
        "model": run.model,
        "runner": run.runner.value,
        "diagnosis": diagnosis_payload,
        "citations": citations,
        "supporting_mechanism_queries": mechanism_queries,
        "evaluation": evaluation_payload,
        "execution": {
            "tool_calls_executed": len(run.tool_calls),
            "tool_calls_requested": run.tool_calls_requested,
            "tool_budget_exhausted": run.tool_budget_exhausted,
            "runner_error": run.error is not None,
            "rejected_tool_calls": dict(
                sorted(Counter(call.reason_code for call in run.rejected_tool_calls).items())
            ),
            "database_load": {
                "query_count": database_load.get("query_count"),
                "failed_query_count": database_load.get("failed_query_count"),
                "rows_returned": database_load.get("rows_returned"),
                "max_concurrency": database_load.get("max_concurrency"),
            },
        },
        "usage": {
            "uncached_input_tokens": uncached,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_creation,
            "output_tokens": run.usage.output_tokens,
            "cache_breakdown_complete": breakdown_complete,
            "estimated_peak_usd": peak_cost,
        },
    }


def _efficiency_eligible(evaluation: Mapping[str, object]) -> bool:
    return evaluation.get("efficiency_eligible") is True


def _auditable_completion(evaluation: Mapping[str, object]) -> bool:
    return evaluation.get("auditable_completion") is True


def _usage_summary(
    runs: list[dict[str, object]],
    model: str,
    *,
    pricing: Mapping[str, object] | None = None,
) -> dict[str, object]:
    fields = (
        "uncached_input_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "output_tokens",
    )
    totals = {
        field: sum(int(_mapping(run, "usage").get(field, 0)) for run in runs) for field in fields
    }
    input_total = (
        totals["uncached_input_tokens"]
        + totals["cache_read_input_tokens"]
        + totals["cache_creation_input_tokens"]
    )
    peak_costs = [_mapping(run, "usage").get("estimated_peak_usd") for run in runs]
    peak_total = (
        sum(float(value) for value in peak_costs)
        if all(isinstance(value, (int, float)) for value in peak_costs)
        else None
    )
    pricing = pricing or MODEL_PRICING.get(model)
    off_peak_multiplier = (
        pricing.get("off_peak_multiplier") if isinstance(pricing, Mapping) else None
    )
    return {
        **totals,
        "cache_hit_rate": (
            totals["cache_read_input_tokens"] / input_total if input_total else None
        ),
        "estimated_peak_usd": peak_total,
        "estimated_off_peak_usd": (
            peak_total * float(off_peak_multiplier)
            if peak_total is not None and isinstance(off_peak_multiplier, (int, float))
            else None
        ),
        "pricing": pricing,
    }


def _mapping(source: Mapping[str, object], key: str) -> dict[str, object]:
    value = source.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"expected object at {key}")
    return value


def _list(source: Mapping[str, object], key: str) -> list[object]:
    value = source.get(key)
    if not isinstance(value, list):
        raise ValueError(f"expected array at {key}")
    return value


def _load_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value
