from __future__ import annotations

import re
from datetime import datetime

from semantic_rca_bench.contracts import AgentRun, Evaluation, GroundTruth


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def component_matches(predicted: str, expected: str) -> bool:
    return _component_key(predicted) == _component_key(expected)


def _component_key(value: str) -> str:
    without_qualifier = re.sub(r"\s*\([^)]*\)\s*$", "", value.strip())
    normalized = _normalize(without_qualifier)
    if normalized.endswith("service"):
        normalized = normalized[: -len("service")]
    return normalized


def ground_truth_fault_category(value: str) -> str:
    native = value.strip().lower()
    return {"mem": "memory"}.get(native, native)


def fault_type_matches(predicted: str, expected: str) -> bool:
    return _normalize(predicted) == _normalize(expected)


def evaluate(run: AgentRun, truth: GroundTruth) -> Evaluation:
    component_match = component_matches(run.diagnosis.root_cause_component, truth.component)
    predicted_fault_category = run.diagnosis.fault_category.value
    expected_fault_category = (
        truth.fault_category.value
        if truth.fault_category is not None
        else ground_truth_fault_category(truth.fault_type)
    )
    fault_type_match = fault_type_matches(run.diagnosis.fault_type, truth.fault_type)
    fault_category_match = predicted_fault_category == expected_fault_category
    query_ids = {trace.query_id for trace in run.tool_calls if trace.query_id is not None}
    valid_evidence_count = sum(
        evidence.query_id in query_ids for evidence in run.diagnosis.evidence
    )
    onset_error = _onset_error(run.diagnosis.onset_time, truth.inject_time)
    return Evaluation(
        component_match=component_match,
        fault_type_match=fault_type_match,
        fault_category_match=fault_category_match,
        joint_match=component_match and fault_type_match,
        predicted_fault_type=run.diagnosis.fault_type,
        expected_fault_type=truth.fault_type,
        predicted_fault_category=predicted_fault_category,
        expected_fault_category=expected_fault_category,
        onset_error_seconds=onset_error,
        cited_evidence_count=len(run.diagnosis.evidence),
        valid_evidence_count=valid_evidence_count,
    )


def _onset_error(value: str | None, expected_epoch: int | None) -> float | None:
    if value is None or expected_epoch is None:
        return None
    try:
        if value.isdigit():
            observed = float(value)
        else:
            observed = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError):
        return None
    return abs(observed - expected_epoch)
