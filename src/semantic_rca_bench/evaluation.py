from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import datetime

from semantic_rca_bench.contracts import AgentRun, Evaluation, GroundTruth, ToolTrace
from semantic_rca_bench.evidence import is_valid_evidence_trace


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


_QUALIFIER_WORDS = r"(?:service|pod|container|instance|deployment)+"


def component_matches(
    predicted: str,
    expected: str,
    *,
    source_identities: Sequence[str] = (),
) -> bool:
    """Whether an answer names the expected component.

    `source_identities` are the pod or container names the case oracle froze as
    equivalent to that component. An answer that names one of them is more
    specific than the service, not wrong, so it matches. Only exact declared
    identities count: the matcher never infers a pod-to-service relation from
    free text.
    """
    predicted_key = _component_key(predicted)
    expected_key = _component_key(expected)
    if predicted_key == expected_key:
        return True
    identity_keys = {_component_key(value) for value in source_identities if value}
    identity_keys.discard("")
    if predicted_key in identity_keys:
        return True
    if not predicted_key.startswith(expected_key):
        return False
    qualifier = predicted_key[len(expected_key) :]
    if re.fullmatch(_QUALIFIER_WORDS, qualifier):
        return True
    # "reservation pod reservation-7c6c958b77-fgxl5" carries the canonical name,
    # a qualifier word, and the declared instance. Strip the first two and the
    # remainder still has to be a declared identity exactly.
    remainder = re.sub(f"^{_QUALIFIER_WORDS}", "", qualifier)
    return remainder != qualifier and remainder in identity_keys


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
    diagnosis = run.diagnosis
    causal_component_match = (
        diagnosis.causal_scope.uses_causal_component
        and diagnosis.causal_component is not None
        and component_matches(diagnosis.causal_component, truth.causal_component)
        if diagnosis is not None and truth.component_scoreable
        else False
        if truth.component_scoreable
        else None
    )
    predicted_fault_category = diagnosis.fault_category.value if diagnosis is not None else None
    expected_fault_category = (
        truth.fault_category.value
        if truth.fault_category is not None
        else ground_truth_fault_category(truth.fault_type)
    )
    fault_type_match = (
        fault_type_matches(diagnosis.fault_type, truth.fault_type)
        if diagnosis is not None
        else False
    )
    fault_category_match = predicted_fault_category == expected_fault_category
    evidence = diagnosis.evidence if diagnosis is not None else []
    traces_by_query_id: dict[str, list[ToolTrace]] = {}
    for trace in run.tool_calls:
        if trace.query_id is not None:
            traces_by_query_id.setdefault(trace.query_id, []).append(trace)
    valid_evidence_count = sum(
        bool(item.claim.strip())
        and is_valid_evidence_trace(traces_by_query_id.get(item.query_id, []))
        for item in evidence
    )
    onset_error = _onset_error(diagnosis.onset_time, truth.inject_time) if diagnosis else None
    discovery_calls = sum(
        _is_discovery_call(trace.tool_name, trace.input) for trace in run.tool_calls
    )
    cited_query_ids = {item.query_id for item in evidence if item.query_id in traces_by_query_id}
    first_cited_index = next(
        (index for index, trace in enumerate(run.tool_calls) if trace.query_id in cited_query_ids),
        None,
    )
    discovery_before_cited_query = (
        sum(
            _is_discovery_call(trace.tool_name, trace.input)
            for trace in run.tool_calls[:first_cited_index]
        )
        if first_cited_index is not None
        else None
    )
    exact_repeated_calls = len(run.tool_calls) - len(
        {
            (trace.tool_name, json.dumps(trace.input, sort_keys=True, default=str))
            for trace in run.tool_calls
        }
    )
    joint_match = (
        causal_component_match and fault_type_match if causal_component_match is not None else None
    )
    valid_completion = (
        joint_match is True
        and diagnosis is not None
        and len(evidence) > 0
        and valid_evidence_count == len(evidence)
        and run.error is None
        and not run.tool_budget_exhausted
    )
    return Evaluation(
        causal_component_match=causal_component_match,
        fault_type_match=fault_type_match,
        fault_category_match=fault_category_match,
        joint_match=joint_match,
        predicted_fault_type=diagnosis.fault_type if diagnosis else None,
        expected_fault_type=truth.fault_type,
        predicted_fault_category=predicted_fault_category,
        expected_fault_category=expected_fault_category,
        onset_error_seconds=onset_error,
        cited_evidence_count=len(evidence),
        valid_evidence_count=valid_evidence_count,
        tool_calls_executed=len(run.tool_calls),
        tool_calls_requested=run.tool_calls_requested,
        discovery_calls=discovery_calls,
        discovery_calls_before_first_cited_query=discovery_before_cited_query,
        semantic_calls=sum(
            trace.tool_name in {"describe_table", "search_table_semantics", "query_semantic_graph"}
            for trace in run.tool_calls
        ),
        failed_calls=(
            sum(trace.error is not None for trace in run.tool_calls)
            + sum(call.reason_code == "invalid" for call in run.rejected_tool_calls)
        ),
        exact_repeated_calls=exact_repeated_calls,
        valid_completion=valid_completion,
        correct_completion_tool_calls=len(run.tool_calls) if valid_completion else None,
    )


def _is_discovery_call(tool_name: str, arguments: dict[str, object]) -> bool:
    if tool_name in {"describe_table", "search_table_semantics"}:
        return True
    if tool_name != "execute_sql":
        return False
    query = str(arguments.get("query") or arguments.get("sql") or "").lower()
    return bool(
        re.search(
            r"\b(show\s+(?:full\s+)?(?:databases|schemas|tables)|describe|desc\s+|"
            r"information_schema\.|pg_catalog\.)",
            query,
        )
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
