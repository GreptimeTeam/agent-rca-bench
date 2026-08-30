from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import sqlglot
from pydantic import BaseModel, ConfigDict
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, build_scope

from semantic_rca_bench.contracts import (
    AgentRun,
    AgentRunner,
    AgentUsage,
    ApiTransport,
    CausalScope,
    DatabaseLoad,
    Diagnosis,
    Evidence,
    EvidenceClaimType,
    FaultCategory,
    MechanismCode,
    QueryResult,
    RejectedToolCall,
    ToolTrace,
    Visibility,
)
from semantic_rca_bench.datasets.aegis_transfer import normalize_workload_restart_evidence
from semantic_rca_bench.evaluation import component_matches
from semantic_rca_bench.evidence import is_valid_evidence_trace
from semantic_rca_bench.protocol import benchmark_protocol

FORMAL_SCORER_REVISION = "aegis-transfer-workload-restart-v1"
FORMAL_SCORER_FIXTURE = Path("fixtures/reference/aegis-transfer-v31-scorer.json")
_SCORER_IDENTITIES = {
    FORMAL_SCORER_REVISION: (
        "aegis-transfer-004",
        "measurement",
        "deepseek-v4-pro",
        ApiTransport.ANTHROPIC_COMPATIBLE_MESSAGES,
        16384,
        "high",
    ),
}


class TransferScorerGroundTruth(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    causal_scope: CausalScope
    causal_component: str
    fault_category: FaultCategory
    mechanism_code: MechanismCode | None = None
    source_fault_type: str


class TransferMechanismEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    predicate: str
    expected_result: dict[str, dict[str, int | float]]
    observable: str | None = None
    metric_table: str | None = None
    identity_column: str | None = None
    identity_value: str | None = None
    minimum_anomalous_observations: int | None = None


class CanonicalApiRunner(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    runner: AgentRunner
    model: str
    api_transport: ApiTransport
    reasoning_effort: str | None
    benchmark_protocol_version: int
    visibility_levels: tuple[Visibility, ...]
    max_tool_calls: int
    max_turns: int
    max_output_tokens: int
    repetitions: int
    treatment_order_seed: int
    parallel_runs: int
    sampling: str
    prompt_cache: str


class AegisTransferScorerFixture(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int
    scorer_revision: str
    agent_case_id: str
    case_role: str
    ground_truth: TransferScorerGroundTruth
    normal_window: tuple[int, int]
    abnormal_window: tuple[int, int]
    required_evidence_claims: tuple[EvidenceClaimType, ...]
    mechanism_evidence: TransferMechanismEvidence
    canonical_api_runner: CanonicalApiRunner


class EvidenceClaimGrounding(BaseModel):
    required: bool
    grounded: bool
    supporting_query_ids: list[str]


class AegisTransferEvaluation(BaseModel):
    success: bool
    runner_contract_match: bool
    tool_budget_contract_match: bool
    causal_component_match: bool | None
    edge_source_match: bool | None
    edge_destination_match: bool | None
    causal_locus_match: bool
    fault_category_match: bool
    citations_execution_valid: bool
    mechanism_evidence_match: bool
    failure_reasons: list[str]
    cited_evidence_count: int
    valid_evidence_count: int
    supporting_evidence_query_ids: list[str]
    causal_locus_evidence_query_ids: list[str]
    mechanism_evidence_query_ids: list[str]
    claim_grounding: dict[str, EvidenceClaimGrounding]
    correct_completion_tool_calls: int | None = None
    tool_calls_through_required_evidence: int | None = None
    rows_returned_through_required_evidence: int | None = None
    causal_scope_match: bool
    causal_operation_match: bool
    mechanism_code_match: bool
    diagnosis_correct: bool
    causal_locus_evidence_match: bool
    required_evidence_covered: bool
    citation_integrity: bool
    execution_reliability: bool
    auditable_completion: bool
    efficiency_eligible: bool


@dataclass(frozen=True)
class RestartEvidencePart:
    normal_samples: int = 0
    normal_min_restarts: float | None = None
    normal_max_restarts: float | None = None
    abnormal_samples: int = 0
    abnormal_min_restarts: float | None = None
    abnormal_max_restarts: float | None = None


def canonical_api_runner_contract(
    *,
    model: str,
    api_transport: ApiTransport,
    max_output_tokens: int,
    reasoning_effort: str | None,
) -> dict[str, object]:
    selected_protocol = benchmark_protocol()
    return {
        "runner": AgentRunner.API.value,
        "model": model,
        "api_transport": api_transport.value,
        "reasoning_effort": reasoning_effort,
        "benchmark_protocol_version": selected_protocol["version"],
        "visibility_levels": [level.value for level in Visibility],
        "max_tool_calls": 48,
        "max_turns": 58,
        "max_output_tokens": max_output_tokens,
        "repetitions": 2,
        "treatment_order_seed": 0,
        "parallel_runs": 1,
        "sampling": "provider-default; no seed sent",
        "prompt_cache": "deepseek-automatic-prefix-v1",
    }


def load_transfer_scorer_fixture(
    path: Path = FORMAL_SCORER_FIXTURE,
) -> AegisTransferScorerFixture:
    fixture = AegisTransferScorerFixture.model_validate_json(path.read_text())
    expected_identity = _SCORER_IDENTITIES.get(fixture.scorer_revision)
    if expected_identity is None or fixture.version != 3:
        raise ValueError("unsupported Aegis transfer scorer fixture revision")
    if fixture.agent_case_id != expected_identity[0]:
        raise ValueError("Aegis transfer scorer fixture uses the wrong opaque case ID")
    if fixture.case_role != expected_identity[1]:
        raise ValueError("Aegis transfer scorer fixture uses the wrong case role")
    if fixture.canonical_api_runner.model_dump(mode="json") != canonical_api_runner_contract(
        model=expected_identity[2],
        api_transport=expected_identity[3],
        max_output_tokens=expected_identity[4],
        reasoning_effort=expected_identity[5],
    ):
        raise ValueError("Aegis transfer canonical API runner contract drifted")
    truth = fixture.ground_truth
    if truth.causal_scope is not CausalScope.COMPONENT:
        raise ValueError("workload-restart scorer must define one causal component")
    if fixture.normal_window[1] != fixture.abnormal_window[0]:
        raise ValueError("Aegis transfer scorer windows must be contiguous")
    if len(set(fixture.required_evidence_claims)) != len(fixture.required_evidence_claims) or set(
        fixture.required_evidence_claims
    ) != {EvidenceClaimType.CAUSAL_LOCUS, EvidenceClaimType.FAULT_MECHANISM}:
        raise ValueError(
            "Aegis transfer scorer must require causal-locus and fault-mechanism evidence"
        )
    evidence = fixture.mechanism_evidence.expected_result
    normal = evidence.get("normal", {})
    abnormal = evidence.get("abnormal", {})
    if (
        fixture.mechanism_evidence.predicate != "source_declared_workload_restart"
        or fixture.ground_truth.causal_scope is not CausalScope.COMPONENT
        or fixture.ground_truth.mechanism_code is not MechanismCode.WORKLOAD_RESTART
        or fixture.mechanism_evidence.observable != "k8s.container.restarts"
        or fixture.mechanism_evidence.metric_table != "k8s_container_restarts"
        or fixture.mechanism_evidence.identity_column != "k8s_container_name"
        or fixture.mechanism_evidence.identity_value != fixture.ground_truth.causal_component
        or (fixture.mechanism_evidence.minimum_anomalous_observations or 0) < 2
        or set(evidence) != {"normal", "abnormal"}
        or normal.get("count", 0) <= 0
        or normal.get("min_restarts") != 0
        or normal.get("max_restarts") != 0
        or abnormal.get("count", 0) <= 0
        or abnormal.get("max_restarts", 0) < 1
    ):
        raise ValueError("workload restart scorer does not prove the frozen transition")
    return fixture


def evaluate_aegis_transfer_run(
    run: AgentRun,
    fixture: AegisTransferScorerFixture,
    *,
    expected_model: str | None = None,
    expected_api_transport: ApiTransport | None = None,
    expected_reasoning_effort: str | None = None,
    expected_max_output_tokens: int | None = None,
) -> AegisTransferEvaluation:
    return _evaluate_structured_transfer_run(
        run,
        fixture,
        expected_model=expected_model,
        expected_api_transport=expected_api_transport,
        expected_reasoning_effort=expected_reasoning_effort,
        expected_max_output_tokens=expected_max_output_tokens,
    )


def _evaluate_structured_transfer_run(
    run: AgentRun,
    fixture: AegisTransferScorerFixture,
    *,
    expected_model: str | None,
    expected_api_transport: ApiTransport | None,
    expected_reasoning_effort: str | None,
    expected_max_output_tokens: int | None,
) -> AegisTransferEvaluation:
    diagnosis = run.diagnosis
    truth = fixture.ground_truth
    runner_contract_match = (
        run.runner is fixture.canonical_api_runner.runner
        and run.model == (expected_model or fixture.canonical_api_runner.model)
        and run.api_transport
        is (expected_api_transport or fixture.canonical_api_runner.api_transport)
        and run.reasoning_effort
        == (
            expected_reasoning_effort
            if expected_api_transport is not None
            else fixture.canonical_api_runner.reasoning_effort
        )
        and run.max_output_tokens
        == (
            expected_max_output_tokens
            if expected_max_output_tokens is not None
            else fixture.canonical_api_runner.max_output_tokens
        )
        and run.visibility in fixture.canonical_api_runner.visibility_levels
    )
    tool_budget_contract_match = len(run.tool_calls) <= fixture.canonical_api_runner.max_tool_calls
    causal_component_match = (
        diagnosis is not None
        and diagnosis.causal_component is not None
        and diagnosis.edge_source is None
        and diagnosis.edge_destination is None
        and component_matches(diagnosis.causal_component, truth.causal_component)
    )
    edge_source_match = None
    edge_destination_match = None
    causal_locus_match = causal_component_match
    causal_scope_match = diagnosis is not None and diagnosis.causal_scope is truth.causal_scope
    causal_operation_match = diagnosis is not None and diagnosis.causal_operation is None
    category_match = diagnosis is not None and diagnosis.fault_category is truth.fault_category
    mechanism_code_match = (
        diagnosis is not None and diagnosis.mechanism_code is truth.mechanism_code
    )
    diagnosis_correct = all(
        (
            causal_locus_match,
            causal_scope_match,
            causal_operation_match,
            category_match,
            mechanism_code_match,
        )
    )

    evidence = diagnosis.evidence if diagnosis is not None else []
    traces_by_query_id: dict[str, list[ToolTrace]] = {}
    trace_indexes: dict[int, int] = {}
    for index, trace in enumerate(run.tool_calls):
        trace_indexes[id(trace)] = index
        if trace.query_id is not None:
            traces_by_query_id.setdefault(trace.query_id, []).append(trace)
    evidence_ids_unique = len({item.query_id for item in evidence}) == len(evidence)
    typed_evidence = bool(evidence) and all(
        item.claim_types and len(item.claim_types) == len(set(item.claim_types))
        for item in evidence
    )
    valid_evidence_count = sum(
        bool(item.claim.strip())
        and is_valid_evidence_trace(traces_by_query_id.get(item.query_id, []))
        for item in evidence
    )
    citations_execution_valid = (
        bool(evidence) and evidence_ids_unique and valid_evidence_count == len(evidence)
    )

    causal_support_ids: list[str] = []
    mechanism_parts: list[tuple[str, int, RestartEvidencePart]] = []
    for item in evidence:
        matches = traces_by_query_id.get(item.query_id, [])
        if len(matches) != 1 or not is_valid_evidence_trace(matches):
            continue
        trace = matches[0]
        mechanism_part = _structured_mechanism_evidence_from_trace(trace, fixture)
        if EvidenceClaimType.CAUSAL_LOCUS in item.claim_types and (
            mechanism_part is not None
            and _mechanism_part_proves_causal_locus(mechanism_part, fixture)
        ):
            causal_support_ids.append(item.query_id)
        if EvidenceClaimType.FAULT_MECHANISM in item.claim_types and mechanism_part is not None:
            mechanism_parts.append((item.query_id, trace_indexes[id(trace)], mechanism_part))

    mechanism_evidence_match = _structured_parts_prove_transition(
        [part for _, _, part in mechanism_parts], fixture
    )
    mechanism_support_ids = [query_id for query_id, _, _ in mechanism_parts]
    causal_support_ids = list(dict.fromkeys(causal_support_ids))
    mechanism_support_ids = list(dict.fromkeys(mechanism_support_ids))
    causal_locus_evidence_match = bool(causal_support_ids)
    claim_grounding = {
        EvidenceClaimType.CAUSAL_LOCUS.value: EvidenceClaimGrounding(
            required=EvidenceClaimType.CAUSAL_LOCUS in fixture.required_evidence_claims,
            grounded=causal_locus_evidence_match,
            supporting_query_ids=causal_support_ids,
        ),
        EvidenceClaimType.FAULT_MECHANISM.value: EvidenceClaimGrounding(
            required=EvidenceClaimType.FAULT_MECHANISM in fixture.required_evidence_claims,
            grounded=mechanism_evidence_match,
            supporting_query_ids=(mechanism_support_ids if mechanism_evidence_match else []),
        ),
    }
    required_evidence_covered = typed_evidence and all(
        claim_grounding[claim.value].grounded for claim in fixture.required_evidence_claims
    )
    supporting_ids = list(
        dict.fromkeys(
            query_id
            for claim in fixture.required_evidence_claims
            for query_id in claim_grounding[claim.value].supporting_query_ids
        )
    )
    execution_reliability = (
        runner_contract_match
        and tool_budget_contract_match
        and run.error is None
        and not run.tool_budget_exhausted
        and not any(item.reason_code == "invalid" for item in run.rejected_tool_calls)
    )
    efficiency_eligible = diagnosis_correct and required_evidence_covered and execution_reliability
    auditable_completion = efficiency_eligible and citations_execution_valid
    support_indexes = [
        trace_indexes[id(trace)]
        for query_id in supporting_ids
        for trace in traces_by_query_id.get(query_id, [])
        if id(trace) in trace_indexes
    ]
    support_index = max(support_indexes) if required_evidence_covered and support_indexes else None
    calls_through_evidence = (
        run.tool_calls[: support_index + 1] if support_index is not None else None
    )
    rows_through_evidence = (
        sum(item.database_load.rows_returned for item in calls_through_evidence)
        if calls_through_evidence is not None
        and all(item.database_load is not None for item in calls_through_evidence)
        else None
    )

    checks = {
        "run does not use the frozen canonical API runner contract": runner_contract_match,
        "run exceeds the frozen tool-call contract": tool_budget_contract_match,
        "causal locus does not match the frozen source mechanism": causal_locus_match,
        "causal scope does not match the frozen source mechanism": causal_scope_match,
        "causal operation does not match a source-observed operation": causal_operation_match,
        "fault category does not match the frozen source mechanism": category_match,
        "structured mechanism does not match the frozen source mechanism": mechanism_code_match,
        "typed evidence does not support the declared causal locus": causal_locus_evidence_match,
        "typed evidence does not prove the frozen observable transition": mechanism_evidence_match,
        "evidence items do not declare their supported claims": typed_evidence,
        "evidence citations are missing, duplicated, failed, truncated, or invalid": (
            citations_execution_valid
        ),
        "runner reported an error": run.error is None,
        "investigation tool budget was exhausted": not run.tool_budget_exhausted,
        "run contains an invalid rejected tool call": not any(
            item.reason_code == "invalid" for item in run.rejected_tool_calls
        ),
    }
    return AegisTransferEvaluation(
        success=auditable_completion,
        runner_contract_match=runner_contract_match,
        tool_budget_contract_match=tool_budget_contract_match,
        causal_component_match=causal_component_match,
        edge_source_match=edge_source_match,
        edge_destination_match=edge_destination_match,
        causal_locus_match=causal_locus_match,
        fault_category_match=category_match,
        citations_execution_valid=citations_execution_valid,
        mechanism_evidence_match=mechanism_evidence_match,
        failure_reasons=[reason for reason, passed in checks.items() if not passed],
        cited_evidence_count=len(evidence),
        valid_evidence_count=valid_evidence_count,
        supporting_evidence_query_ids=(supporting_ids if required_evidence_covered else []),
        causal_locus_evidence_query_ids=(causal_support_ids if causal_locus_evidence_match else []),
        mechanism_evidence_query_ids=(mechanism_support_ids if mechanism_evidence_match else []),
        claim_grounding=claim_grounding,
        correct_completion_tool_calls=len(run.tool_calls) if efficiency_eligible else None,
        tool_calls_through_required_evidence=(
            support_index + 1 if support_index is not None else None
        ),
        rows_returned_through_required_evidence=rows_through_evidence,
        causal_scope_match=causal_scope_match,
        causal_operation_match=causal_operation_match,
        mechanism_code_match=mechanism_code_match,
        diagnosis_correct=diagnosis_correct,
        causal_locus_evidence_match=causal_locus_evidence_match,
        required_evidence_covered=required_evidence_covered,
        citation_integrity=citations_execution_valid,
        execution_reliability=execution_reliability,
        auditable_completion=auditable_completion,
        efficiency_eligible=efficiency_eligible,
    )


def audit_transfer_scorer(
    transfer_audit: dict[str, object],
    fixture: AegisTransferScorerFixture,
    fixture_path: Path,
) -> dict[str, object]:
    _validate_fixture_file(fixture, fixture_path)
    source_gate_match = _transfer_audit_matches_fixture(transfer_audit, fixture)
    canonical_run = _canonical_synthetic_run(transfer_audit, fixture)
    cases: dict[str, tuple[AgentRun, bool]] = {
        "canonical_positive": (canonical_run, True),
        "graph_navigation_only": (
            _graph_navigation_only_run(canonical_run, fixture),
            False,
        ),
        "wrong_fault_category": (
            _replace_diagnosis(
                canonical_run,
                fault_category=(
                    FaultCategory.OTHER
                    if fixture.ground_truth.fault_category is FaultCategory.DELAY
                    else FaultCategory.DELAY
                ),
            ),
            False,
        ),
        "invalid_citation": (_replace_evidence_query_id(canonical_run, "missing"), False),
        "duplicated_citation": (_duplicate_evidence(canonical_run), False),
        "runner_error": (canonical_run.model_copy(update={"error": "runner failed"}), False),
        "budget_exhausted": (
            canonical_run.model_copy(update={"tool_budget_exhausted": True}),
            False,
        ),
        "tool_budget_contract_violation": (
            canonical_run.model_copy(
                update={
                    "tool_calls": canonical_run.tool_calls
                    * (fixture.canonical_api_runner.max_tool_calls + 1),
                    "tool_calls_requested": fixture.canonical_api_runner.max_tool_calls + 1,
                }
            ),
            False,
        ),
        "invalid_rejected_call": (
            canonical_run.model_copy(
                update={
                    "rejected_tool_calls": [
                        RejectedToolCall(
                            tool_name="execute_sql",
                            input={},
                            error="invalid call",
                        )
                    ]
                }
            ),
            False,
        ),
    }
    canonical_query = str(_canonical_trace(canonical_run).input["query"])
    abnormal_end = datetime.fromtimestamp(fixture.abnormal_window[1], UTC).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    narrowed_end = datetime.fromtimestamp(fixture.abnormal_window[1] - 1, UTC).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    wrong_scope = (
        CausalScope.DEPENDENCY_EDGE
        if fixture.ground_truth.causal_scope is CausalScope.COMPONENT
        else CausalScope.COMPONENT
    )
    cases.update(
        {
            "wrong_causal_locus": (
                _replace_diagnosis(
                    canonical_run,
                    causal_component="wrong-service",
                ),
                False,
            ),
            "wrong_causal_scope": (
                _replace_diagnosis(canonical_run, causal_scope=wrong_scope),
                False,
            ),
            "wrong_causal_operation": (
                _replace_diagnosis(canonical_run, causal_operation="POST /wrong"),
                False,
            ),
            "wrong_mechanism": (
                _replace_diagnosis(
                    canonical_run,
                    mechanism_code=MechanismCode.CONNECTION_FAILURE,
                ),
                False,
            ),
            "untyped_evidence": (_remove_evidence_claim_types(canonical_run), False),
            "narrowed_outer_window": (
                _replace_trace_query(
                    canonical_run,
                    canonical_query.replace(abnormal_end, narrowed_end),
                ),
                False,
            ),
        }
    )
    cases.update(
        {
            "unexpected_edge": (
                _replace_diagnosis(
                    canonical_run,
                    edge_source=fixture.ground_truth.causal_component,
                    edge_destination="unexpected-service",
                ),
                False,
            ),
            "equivalent_case_normalized_workload": (
                _replace_trace_query(
                    canonical_run,
                    canonical_query.replace(
                        f"k8s_container_name = '{fixture.ground_truth.causal_component}'",
                        "LOWER(k8s_container_name) = "
                        f"'{fixture.ground_truth.causal_component.lower()}'",
                    ),
                ),
                True,
            ),
            "wrong_workload_identity": (
                _replace_trace_query(
                    canonical_run,
                    canonical_query.replace(
                        str(fixture.ground_truth.causal_component),
                        "wrong-service",
                    ),
                ),
                False,
            ),
            "wrong_restart_metric": (
                _replace_trace_query(
                    canonical_run,
                    canonical_query.replace(
                        "k8s_container_restarts",
                        "k8s_container_ready",
                    ),
                ),
                False,
            ),
            "value_cherry_pick": (
                _replace_trace_query(
                    canonical_run,
                    canonical_query.replace(
                        f"AND greptime_timestamp < '{abnormal_end}'",
                        f"AND greptime_timestamp < '{abnormal_end}'\n  AND greptime_value > 0",
                    ),
                ),
                False,
            ),
            "limited_restart_samples": (
                _replace_trace_query(canonical_run, canonical_query + "\nLIMIT 1"),
                False,
            ),
            "hardcoded_restart_maximum": (
                _replace_trace_query(
                    canonical_run,
                    canonical_query.replace(
                        "MAX(greptime_value) AS max_restarts",
                        "1 AS max_restarts",
                    ),
                ),
                False,
            ),
            "dirty_restart_baseline": (
                _replace_mechanism_value(canonical_run, "normal", "max_restarts", 1.0),
                False,
            ),
            "missing_restart_transition": (
                _replace_mechanism_value(canonical_run, "abnormal", "max_restarts", 0.0),
                False,
            ),
        }
    )
    for row_name in fixture.mechanism_evidence.expected_result:
        cases[f"missing_{row_name}"] = (_remove_mechanism_row(canonical_run, row_name), False)

    results = {}
    for name, (run, expected_success) in cases.items():
        evaluation = evaluate_aegis_transfer_run(run, fixture)
        results[name] = {
            "expected_success": expected_success,
            "observed_success": evaluation.success,
            "pass": evaluation.success is expected_success,
            "evaluation": evaluation.model_dump(mode="json"),
        }
    scorer_regressions_pass = all(item["pass"] for item in results.values())
    case = transfer_audit.get("case")
    agent_facing = case.get("agent_facing", {}) if isinstance(case, dict) else {}
    agent_payload = json.dumps(agent_facing, sort_keys=True)
    source_mapping = case.get("source_mapping", {}) if isinstance(case, dict) else {}
    source_case = source_mapping.get("source_case") if isinstance(source_mapping, dict) else None
    opaque_case_gate = (
        isinstance(agent_facing, dict)
        and agent_facing.get("case_id") == fixture.agent_case_id
        and agent_facing.get("fault_taxonomy") == []
        and fixture.ground_truth.source_fault_type not in agent_payload
        and (not isinstance(source_case, str) or source_case not in agent_payload)
    )
    scorer_identity = _SCORER_IDENTITIES.get(fixture.scorer_revision)
    gates = {
        "source_transfer_audit_match": source_gate_match,
        "canonical_runner_contract_match": (
            scorer_identity is not None
            and fixture.canonical_api_runner.model_dump(mode="json")
            == canonical_api_runner_contract(
                model=scorer_identity[2],
                api_transport=scorer_identity[3],
                max_output_tokens=scorer_identity[4],
                reasoning_effort=scorer_identity[5],
            )
        ),
        "opaque_agent_input": opaque_case_gate,
        "scorer_regressions": scorer_regressions_pass,
    }
    gates["all_passed"] = all(gates.values())
    return {
        "audit_schema_version": 2,
        "mode": "aegis-transfer-scorer-no-model-audit",
        "scorer_revision": fixture.scorer_revision,
        "fixture_sha256": sha256_file(fixture_path),
        "agent_case_id": fixture.agent_case_id,
        "case_role": fixture.case_role,
        "canonical_api_runner": fixture.canonical_api_runner.model_dump(mode="json"),
        "source_transfer_audit_sha256": source_transfer_audit_sha256(transfer_audit),
        "synthetic_regressions": results,
        "no_model_gates": gates,
    }


def _result_projection_reads_any(
    scope: exp.Select,
    output_column: str,
    source_columns: set[str],
) -> bool:
    matches = [
        projection
        for projection in scope.expressions
        if projection.alias_or_name.lower() == output_column.lower()
    ]
    if len(matches) != 1:
        return False
    expression = matches[0].this if isinstance(matches[0], exp.Alias) else matches[0]
    if isinstance(expression, exp.Column):
        return expression.name.lower() in source_columns
    if isinstance(expression, (exp.Lower, exp.Upper)) and isinstance(expression.this, exp.Column):
        return expression.this.name.lower() in source_columns
    return False


def _structured_mechanism_evidence_from_trace(
    trace: ToolTrace,
    fixture: AegisTransferScorerFixture,
) -> RestartEvidencePart | None:
    return _workload_restart_evidence_from_trace(trace, fixture)


def _structured_parts_prove_transition(
    parts: list[RestartEvidencePart],
    fixture: AegisTransferScorerFixture,
) -> bool:
    return _restart_parts_prove_transition(parts, fixture)


def _mechanism_part_proves_causal_locus(
    part: RestartEvidencePart,
    fixture: AegisTransferScorerFixture,
) -> bool:
    minimum = int(fixture.mechanism_evidence.minimum_anomalous_observations or 0)
    return (
        part.abnormal_samples >= minimum
        and part.abnormal_max_restarts is not None
        and part.abnormal_max_restarts >= 1
    )


def _workload_restart_evidence_from_trace(
    trace: ToolTrace,
    fixture: AegisTransferScorerFixture,
) -> RestartEvidencePart | None:
    if (
        trace.tool_name != "execute_sql"
        or trace.error is not None
        or not isinstance(trace.output, dict)
    ):
        return None
    try:
        result = QueryResult.model_validate(trace.output)
    except ValueError:
        return None
    if result.query_id != trace.query_id or result.truncated:
        return None
    statement = _parse_single_statement(
        str(trace.input.get("query") or trace.input.get("sql") or "")
    )
    table = fixture.mechanism_evidence.metric_table
    identity_column = fixture.mechanism_evidence.identity_column
    identity_value = fixture.mechanism_evidence.identity_value
    if (
        statement is None
        or not table
        or not identity_column
        or not identity_value
        or not _uses_only_source_tables(statement, {table})
        or _has_identity_literal_filter(statement)
        or any(statement.find_all(exp.Limit, exp.Having))
    ):
        return None
    scopes = _table_select_scopes(statement, table)
    if len(scopes) != 1:
        return None
    scope = scopes[0]
    if any(
        join.find_ancestor(exp.Select) is scope for join in scope.find_all(exp.Join)
    ) or not _restart_filter_scope_is_complete(scope, identity_column, identity_value):
        return None
    periods = _restart_scope_periods(scope, fixture)
    if not periods:
        return None
    aggregate = _restart_aggregate_result(result, scope, periods, fixture)
    if aggregate is not None:
        return aggregate
    return _restart_raw_result(result, scope, periods, fixture)


def _restart_filter_scope_is_complete(
    scope: exp.Select,
    identity_column: str,
    identity_value: str,
) -> bool:
    where = scope.args.get("where")
    if where is None or any(where.find_all(exp.Or, exp.Not, exp.Subquery)):
        return False
    if any(
        column.name.lower() not in {"greptime_timestamp", identity_column.lower()}
        for column in where.find_all(exp.Column)
    ):
        return False
    identity_predicates = [
        predicate
        for predicate in where.find_all(
            exp.EQ,
            exp.In,
            exp.Like,
            exp.ILike,
            exp.GT,
            exp.GTE,
            exp.LT,
            exp.LTE,
            exp.Between,
        )
        if any(
            column.name.lower() == identity_column.lower()
            for column in predicate.find_all(exp.Column)
        )
    ]
    return len(identity_predicates) == 1 and _has_column_in_literals(
        scope,
        identity_column,
        {identity_value},
        allow_case_normalization=True,
    )


def _restart_scope_periods(
    scope: exp.Select,
    fixture: AegisTransferScorerFixture,
) -> set[str] | None:
    periods = _exact_scope_periods(scope, "greptime_timestamp", fixture)
    if periods is not None:
        return periods
    bounds = _time_bounds(scope, "greptime_timestamp", filters_only=True)
    inclusive = {
        frozenset({("gte", fixture.normal_window[0]), ("lte", fixture.normal_window[1])}): {
            "normal"
        },
        frozenset({("gte", fixture.abnormal_window[0]), ("lte", fixture.abnormal_window[1])}): {
            "abnormal"
        },
        frozenset({("gte", fixture.normal_window[0]), ("lte", fixture.abnormal_window[1])}): {
            "normal",
            "abnormal",
        },
    }
    return inclusive.get(frozenset(bounds))


def _restart_aggregate_result(
    result: QueryResult,
    scope: exp.Select,
    periods: set[str],
    fixture: AegisTransferScorerFixture,
) -> RestartEvidencePart | None:
    aliases = {
        projection.alias_or_name.lower(): (
            projection.this if isinstance(projection, exp.Alias) else projection
        )
        for projection in scope.expressions
        if projection.alias_or_name
    }
    if not (
        _is_unconditional_row_count_projection(aliases.get("sample_count", exp.Null()))
        and _is_metric_aggregate(aliases.get("min_restarts"), exp.Min)
        and _is_metric_aggregate(aliases.get("max_restarts"), exp.Max)
    ):
        return None
    if len(periods) == 2 and not _scope_binds_period(scope, "greptime_timestamp", periods, fixture):
        return None
    normalized = normalize_workload_restart_evidence(result)
    if normalized is not None and periods == {"normal", "abnormal"}:
        return _restart_part(normalized)
    if len(periods) != 1 or len(result.rows) != 1:
        return None
    columns = [column.lower() for column in result.columns]
    required = ("sample_count", "min_restarts", "max_restarts")
    if any(columns.count(column) != 1 for column in required):
        return None
    indexes = [columns.index(column) for column in required]
    row = result.rows[0]
    if not _row_covers(row, *indexes):
        return None
    count = _strict_int(row[indexes[0]])
    minimum = _strict_number(row[indexes[1]])
    maximum = _strict_number(row[indexes[2]])
    if count is None or count <= 0 or minimum is None or maximum is None or minimum > maximum:
        return None
    period = next(iter(periods))
    return _restart_part(
        {period: {"count": count, "min_restarts": minimum, "max_restarts": maximum}}
    )


def _is_metric_aggregate(
    expression: exp.Expression | None,
    aggregate_type: type[exp.AggFunc],
) -> bool:
    return (
        isinstance(expression, aggregate_type)
        and isinstance(expression.this, exp.Column)
        and expression.this.name.lower() == "greptime_value"
    )


def _restart_raw_result(
    result: QueryResult,
    scope: exp.Select,
    periods: set[str],
    fixture: AegisTransferScorerFixture,
) -> RestartEvidencePart | None:
    columns = [column.lower() for column in result.columns]
    time_index = _unique_column(columns, lambda value: value == "greptime_timestamp")
    value_index = _unique_column(columns, lambda value: value == "greptime_value")
    if (
        time_index is None
        or value_index is None
        or not _result_projection_reads_any(scope, columns[time_index], {"greptime_timestamp"})
        or not _result_projection_reads_any(scope, columns[value_index], {"greptime_value"})
    ):
        return None
    values: dict[str, list[float]] = {period: [] for period in periods}
    for row in result.rows:
        if not _row_covers(row, time_index, value_index):
            return None
        timestamp = _timestamp_ns(row[time_index])
        value = _strict_number(row[value_index])
        if timestamp is None or value is None:
            return None
        epoch = timestamp // 1_000_000_000
        if fixture.normal_window[0] <= epoch < fixture.normal_window[1]:
            period = "normal"
        elif fixture.abnormal_window[0] <= epoch < fixture.abnormal_window[1]:
            period = "abnormal"
        else:
            return None
        if period not in values:
            return None
        values[period].append(value)
    if any(not values[period] for period in periods):
        return None
    return _restart_part(
        {
            period: {
                "count": len(period_values),
                "min_restarts": min(period_values),
                "max_restarts": max(period_values),
            }
            for period, period_values in values.items()
        }
    )


def _restart_part(
    periods: dict[str, dict[str, int | float]],
) -> RestartEvidencePart:
    normal = periods.get("normal", {})
    abnormal = periods.get("abnormal", {})
    return RestartEvidencePart(
        normal_samples=int(normal.get("count", 0)),
        normal_min_restarts=_optional_float(normal.get("min_restarts")),
        normal_max_restarts=_optional_float(normal.get("max_restarts")),
        abnormal_samples=int(abnormal.get("count", 0)),
        abnormal_min_restarts=_optional_float(abnormal.get("min_restarts")),
        abnormal_max_restarts=_optional_float(abnormal.get("max_restarts")),
    )


def _is_unconditional_row_count_projection(projection: exp.Expression) -> bool:
    expression = projection.this if isinstance(projection, exp.Alias) else projection
    if not isinstance(expression, exp.Count):
        return False
    value = expression.this
    return isinstance(value, exp.Star) or (
        isinstance(value, exp.Literal) and _strict_int_literal(value) == 1
    )


def _column_eq_literal_count(
    statement: exp.Expression,
    column_name: str,
    literal_value: str,
    *,
    allow_case_normalization: bool = False,
) -> int:
    count = 0
    for equality in statement.find_all(exp.EQ):
        if not _belongs_to_select_scope(equality, statement):
            continue
        for column, value in (
            (equality.this, equality.expression),
            (equality.expression, equality.this),
        ):
            if _column_literal_matches(
                column,
                value,
                column_name,
                literal_value,
                allow_case_normalization=allow_case_normalization,
            ) and _is_positive_filter_predicate(equality):
                count += 1
    return count


def _has_column_eq_literal(
    statement: exp.Expression,
    column_name: str,
    literal_value: str,
    *,
    allow_case_normalization: bool = False,
) -> bool:
    return (
        _column_eq_literal_count(
            statement,
            column_name,
            literal_value,
            allow_case_normalization=allow_case_normalization,
        )
        > 0
    )


def _has_column_in_literals(
    statement: exp.Expression,
    column_name: str,
    allowed_values: set[str],
    *,
    allow_case_normalization: bool = False,
) -> bool:
    if any(
        _has_column_eq_literal(
            statement,
            column_name,
            value,
            allow_case_normalization=allow_case_normalization,
        )
        for value in allowed_values
    ):
        return True
    for inclusion in statement.find_all(exp.In):
        if not _belongs_to_select_scope(inclusion, statement):
            continue
        normalization = _column_normalization(inclusion.this, column_name)
        if normalization is None and not (
            isinstance(inclusion.this, exp.Column)
            and inclusion.this.name.lower() == column_name.lower()
        ):
            continue
        if normalization is not None and not allow_case_normalization:
            continue
        values = {
            value
            for item in inclusion.expressions
            if (value := _evaluated_string_literal(item)) is not None
        }
        expected_values = {_normalize_string(value, normalization) for value in allowed_values}
        if (
            len(values) == len(inclusion.expressions)
            and values
            and values <= expected_values
            and _is_positive_filter_predicate(inclusion)
        ):
            return True
    return False


def _column_normalization(expression: exp.Expression, column_name: str) -> str | None:
    if not isinstance(expression, (exp.Lower, exp.Upper)):
        return None
    column = expression.this
    if not isinstance(column, exp.Column) or column.name.lower() != column_name.lower():
        return None
    return "lower" if isinstance(expression, exp.Lower) else "upper"


def _column_literal_matches(
    column: exp.Expression,
    value: exp.Expression,
    column_name: str,
    literal_value: str,
    *,
    allow_case_normalization: bool,
) -> bool:
    if isinstance(column, exp.Column) and column.name.lower() == column_name.lower():
        return _evaluated_string_literal(value) == literal_value
    normalization = _column_normalization(column, column_name)
    return (
        allow_case_normalization
        and normalization is not None
        and _evaluated_string_literal(value) == _normalize_string(literal_value, normalization)
    )


def _evaluated_string_literal(expression: exp.Expression) -> str | None:
    if isinstance(expression, exp.Literal) and expression.is_string:
        return str(expression.this)
    if isinstance(expression, (exp.Lower, exp.Upper)):
        value = expression.this
        if not isinstance(value, exp.Literal) or not value.is_string:
            return None
        literal_normalization = "lower" if isinstance(expression, exp.Lower) else "upper"
        return _normalize_string(str(value.this), literal_normalization)
    return None


def _normalize_string(value: str, normalization: str | None) -> str:
    if normalization == "lower":
        return value.lower()
    if normalization == "upper":
        return value.upper()
    return value


def _table_select_scopes(statement: exp.Expression, table_name: str) -> tuple[exp.Select, ...]:
    scopes: list[exp.Select] = []
    for table in statement.find_all(exp.Table):
        if table.name.lower() != table_name.lower():
            continue
        scope = table.find_ancestor(exp.Select)
        if scope is not None and all(scope is not item for item in scopes):
            scopes.append(scope)
    return tuple(scopes)


def _belongs_to_select_scope(expression: exp.Expression, scope: exp.Expression) -> bool:
    if not isinstance(scope, exp.Select):
        return True
    return expression.find_ancestor(exp.Select) is scope


def _is_positive_filter_predicate(expression: exp.Expression) -> bool:
    current = expression.parent
    while current is not None:
        if isinstance(current, (exp.Or, exp.Not)):
            return False
        if isinstance(current, (exp.Where, exp.Join, exp.Having)):
            return True
        if isinstance(current, exp.Select):
            return False
        current = current.parent
    return False


def _exact_scope_periods(
    scope: exp.Select,
    time_column: str,
    fixture: AegisTransferScorerFixture,
) -> set[str] | None:
    bounds = _time_bounds(scope, time_column, filters_only=True)
    expected = {
        frozenset(
            {
                ("gte", fixture.normal_window[0]),
                ("lt", fixture.normal_window[1]),
            }
        ): {"normal"},
        frozenset(
            {
                ("gte", fixture.abnormal_window[0]),
                ("lt", fixture.abnormal_window[1]),
            }
        ): {"abnormal"},
        frozenset(
            {
                ("gte", fixture.normal_window[0]),
                ("lt", fixture.abnormal_window[1]),
            }
        ): {"normal", "abnormal"},
    }
    return expected.get(frozenset(bounds))


def _time_bounds(
    expression: exp.Expression,
    time_column: str,
    *,
    filters_only: bool,
    table_alias: str | None = None,
) -> set[tuple[str, int]]:
    bounds: set[tuple[str, int]] = set()
    comparison_types = (
        (exp.GTE, "gte", "lte"),
        (exp.LT, "lt", "gt"),
        (exp.GT, "gt", "lt"),
        (exp.LTE, "lte", "gte"),
    )
    for comparison_type, direct_operator, reversed_operator in comparison_types:
        for comparison in expression.find_all(comparison_type):
            if not _belongs_to_select_scope(comparison, expression):
                continue
            if filters_only and not _is_positive_filter_predicate(comparison):
                continue
            for column, value, operator in (
                (comparison.this, comparison.expression, direct_operator),
                (comparison.expression, comparison.this, reversed_operator),
            ):
                if not (
                    isinstance(column, exp.Column)
                    and column.name.lower() == time_column.lower()
                    and (table_alias is None or column.table.lower() == table_alias.lower())
                ):
                    continue
                epoch = _timestamp_expression_epoch(value)
                if epoch is not None:
                    bounds.add((operator, epoch))
    for between in expression.find_all(exp.Between):
        if not _belongs_to_select_scope(between, expression):
            continue
        if filters_only and not _is_positive_filter_predicate(between):
            continue
        column = between.this
        if not (
            isinstance(column, exp.Column)
            and column.name.lower() == time_column.lower()
            and (table_alias is None or column.table.lower() == table_alias.lower())
        ):
            continue
        lower = _timestamp_expression_epoch(between.args["low"])
        upper = _timestamp_expression_epoch(between.args["high"])
        if lower is not None and upper is not None:
            bounds.update({("gte", lower), ("lte", upper)})
    return bounds


def _scope_binds_period(
    scope: exp.Select,
    time_column: str,
    periods: set[str],
    fixture: AegisTransferScorerFixture,
    *,
    table_alias: str | None = None,
) -> bool:
    period_projections = [
        projection
        for projection in scope.expressions
        if projection.alias_or_name.lower() == "period"
    ]
    if len(period_projections) != 1:
        return False
    projection = period_projections[0]
    expression = projection.this if isinstance(projection, exp.Alias) else projection
    if len(periods) == 1:
        return (
            isinstance(expression, exp.Literal)
            and expression.is_string
            and str(expression.this).lower() == next(iter(periods))
        )
    if periods != {"normal", "abnormal"}:
        return False
    case = expression if isinstance(expression, exp.Case) else expression.find(exp.Case)
    if case is None:
        return False
    expected = {
        "normal": {
            ("gte", fixture.normal_window[0]),
            ("lt", fixture.normal_window[1]),
        },
        "abnormal": {
            ("gte", fixture.abnormal_window[0]),
            ("lt", fixture.abnormal_window[1]),
        },
    }
    observed: dict[str, set[tuple[str, int]]] = {}
    for branch in case.args.get("ifs") or []:
        value = branch.args.get("true")
        condition = branch.this
        if not isinstance(value, exp.Literal) or not value.is_string:
            continue
        period = str(value.this).lower()
        if period in expected:
            observed[period] = _time_bounds(
                condition,
                time_column,
                filters_only=False,
                table_alias=table_alias,
            )
    if all(observed.get(period) == bounds for period, bounds in expected.items()):
        return True

    default = case.args.get("default")
    if not isinstance(default, exp.Literal) or not default.is_string or len(observed) != 1:
        return False
    branch_period, branch_bounds = next(iter(observed.items()))
    default_period = str(default.this).lower()
    boundary = fixture.abnormal_window[0]
    return (
        branch_period == "normal"
        and default_period == "abnormal"
        and branch_bounds == {("lt", boundary)}
    ) or (
        branch_period == "abnormal"
        and default_period == "normal"
        and branch_bounds == {("gte", boundary)}
    )


def _parse_single_statement(query: str) -> exp.Expression | None:
    for dialect in ("postgres", "mysql"):
        try:
            statements = sqlglot.parse(query, read=dialect)
        except sqlglot.errors.ParseError:
            continue
        if len(statements) == 1:
            return statements[0]
    return None


def _uses_only_source_tables(statement: exp.Expression, allowed: set[str]) -> bool:
    root = build_scope(statement)
    if root is None:
        return False
    pending = [root]
    observed: set[int] = set()
    while pending:
        scope = pending.pop()
        if id(scope) in observed:
            continue
        observed.add(id(scope))
        for source in scope.sources.values():
            if isinstance(source, Scope):
                pending.append(source)
            elif isinstance(source, exp.Table) and source.name.lower() not in allowed:
                return False
    return True


def _has_identity_literal_filter(statement: exp.Expression) -> bool:
    return any(
        _identity_literal_comparison(comparison)
        for comparison in statement.find_all(
            exp.EQ,
            exp.In,
            exp.Like,
            exp.ILike,
            exp.GT,
            exp.GTE,
            exp.LT,
            exp.LTE,
            exp.Between,
        )
    )


def _identity_literal_comparison(comparison: exp.Expression) -> bool:
    columns = list(comparison.find_all(exp.Column))
    if not any(
        column.name.lower() in {"trace_id", "span_id", "parent_span_id"} for column in columns
    ):
        return False
    return any(comparison.find_all(exp.Literal))


def _restart_parts_prove_transition(
    parts: list[RestartEvidencePart],
    fixture: AegisTransferScorerFixture,
) -> bool:
    if not parts:
        return False
    minimum = int(fixture.mechanism_evidence.minimum_anomalous_observations or 0)
    normal = [part for part in parts if part.normal_samples > 0]
    abnormal = [part for part in parts if part.abnormal_samples > 0]
    return (
        bool(normal)
        and bool(abnormal)
        and all(part.normal_min_restarts == 0 and part.normal_max_restarts == 0 for part in normal)
        and all(
            part.abnormal_min_restarts is not None
            and part.abnormal_max_restarts is not None
            and part.abnormal_min_restarts >= 0
            and part.abnormal_max_restarts >= 1
            for part in abnormal
        )
        and max(part.normal_samples for part in normal) >= minimum
        and max(part.abnormal_samples for part in abnormal) >= minimum
    )


def _unique_column(columns: list[str], predicate: Callable[[str], bool]) -> int | None:
    matches = [index for index, value in enumerate(columns) if predicate(value)]
    return matches[0] if len(matches) == 1 else None


def _row_covers(row: list[object], *indexes: int) -> bool:
    return not indexes or len(row) > max(indexes)


def _timestamp_ns(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        if value >= 10**17:
            return value
        if value >= 10**14:
            return value * 1_000
        if value >= 10**11:
            return value * 1_000_000
        if value >= 10**8:
            return value * 1_000_000_000
        return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        delta = parsed.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
        return (delta.days * 86_400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1_000
    return None


def _strict_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _strict_number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _optional_float(value: object) -> float | None:
    return _strict_number(value) if value is not None else None


def _strict_int_literal(literal: exp.Literal) -> int | None:
    value = str(literal.this)
    if not value.isdigit():
        return None
    return int(value)


def _timestamp_expression_epoch(expression: exp.Expression) -> int | None:
    if isinstance(expression, exp.Cast):
        target = expression.args.get("to")
        if not isinstance(target, exp.DataType) or target.this not in {
            exp.DataType.Type.DATE,
            exp.DataType.Type.DATETIME,
            exp.DataType.Type.TIMESTAMP,
            exp.DataType.Type.TIMESTAMPTZ,
        }:
            return None
        expression = expression.this
    if not isinstance(expression, exp.Literal):
        return None
    value = str(expression.this)
    if value.isdigit() and len(value) == 10:
        return int(value)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


def _transfer_audit_matches_fixture(
    audit: dict[str, object],
    fixture: AegisTransferScorerFixture,
) -> bool:
    try:
        case = audit["case"]
        mechanism = audit["mechanism_evidence"]
        gates = audit["no_model_gates"]
        if not isinstance(mechanism, dict):
            return False
        mechanism_details_match = (
            mechanism.get("observable") == fixture.mechanism_evidence.observable
            and mechanism.get("identity_field") == "attr.k8s.container.name"
            and mechanism.get("identity_value") == fixture.mechanism_evidence.identity_value
            and mechanism.get("declared_pod_identity_match") is False
        )
        observed_edge = case.get("declared_edge")
        return (
            audit["mode"] == "aegis-transfer-no-model-audit"
            and isinstance(case, dict)
            and case["agent_facing"]["case_id"] == fixture.agent_case_id
            and tuple(case["normal_window"]) == fixture.normal_window
            and tuple(case["abnormal_window"]) == fixture.abnormal_window
            and observed_edge is None
            and case["fault_type"] == fixture.ground_truth.source_fault_type
            and isinstance(mechanism, dict)
            and mechanism["predicate"] == fixture.mechanism_evidence.predicate
            and mechanism["normalized_result"] == fixture.mechanism_evidence.expected_result
            and mechanism["expected_result"] == fixture.mechanism_evidence.expected_result
            and mechanism["pass"] is True
            and mechanism_details_match
            and isinstance(gates, dict)
            and gates["all_passed"] is True
            and gates["case_normalized_predicates_source_equivalent"] is True
        )
    except (KeyError, TypeError):
        return False


def _canonical_synthetic_run(
    audit: dict[str, object],
    fixture: AegisTransferScorerFixture,
) -> AgentRun:
    mechanism = audit.get("mechanism_evidence")
    if not isinstance(mechanism, dict):
        raise ValueError("transfer audit has no mechanism evidence")
    query = mechanism.get("query")
    output = mechanism.get("result")
    if not isinstance(query, str) or not isinstance(output, dict):
        raise ValueError("transfer audit mechanism evidence is malformed")
    result = QueryResult.model_validate(output).model_copy(update={"query_id": "q01"})
    truth = fixture.ground_truth
    return AgentRun(
        run_id="no-model-scorer-audit",
        visibility=Visibility.RAW,
        model=fixture.canonical_api_runner.model,
        runner=fixture.canonical_api_runner.runner,
        api_transport=fixture.canonical_api_runner.api_transport,
        reasoning_effort=fixture.canonical_api_runner.reasoning_effort,
        max_output_tokens=fixture.canonical_api_runner.max_output_tokens,
        diagnosis=Diagnosis(
            causal_scope=truth.causal_scope,
            causal_component=truth.causal_component,
            edge_source=None,
            edge_destination=None,
            impacted_component=None,
            causal_operation=None,
            fault_category=truth.fault_category,
            mechanism_code=truth.mechanism_code,
            fault_type=truth.source_fault_type,
            confidence=1,
            evidence=[
                Evidence(
                    query_id="q01",
                    claim="paired spans prove the source mechanism",
                    claim_types=[
                        EvidenceClaimType.CAUSAL_LOCUS,
                        EvidenceClaimType.FAULT_MECHANISM,
                    ],
                )
            ],
            explanation="The stored paired spans prove the frozen source mechanism.",
        ),
        tool_calls=[
            ToolTrace(
                tool_name="execute_sql",
                input={"query": query},
                query_id="q01",
                output=result.model_dump(mode="json"),
                database_load=DatabaseLoad(query_count=1, rows_returned=len(result.rows)),
            )
        ],
        tool_calls_requested=1,
        usage=AgentUsage(),
        elapsed_seconds=0,
        responses=[],
    )


def _graph_navigation_only_run(
    run: AgentRun,
    fixture: AegisTransferScorerFixture,
) -> AgentRun:
    if run.diagnosis is None:
        raise ValueError("graph navigation regression requires a diagnosis")
    truth = fixture.ground_truth
    result = QueryResult(
        query_id="graph-navigation",
        columns=["entity_type", "entity_id"],
        rows=[["service", truth.causal_component]],
        elapsed_seconds=0,
    )
    evidence = Evidence(
        query_id=result.query_id,
        claim="The Graph identifies the declared service or call path.",
        claim_types=[EvidenceClaimType.CAUSAL_LOCUS],
    )
    return run.model_copy(
        update={
            "visibility": Visibility.SEMANTIC_GRAPH,
            "diagnosis": run.diagnosis.model_copy(update={"evidence": [evidence]}),
            "tool_calls": [
                ToolTrace(
                    tool_name="query_semantic_graph",
                    input={"view": "entities"},
                    query_id=result.query_id,
                    output=result.model_dump(mode="json"),
                    database_load=DatabaseLoad(query_count=1, rows_returned=1),
                )
            ],
            "tool_calls_requested": 1,
        }
    )


def _replace_diagnosis(run: AgentRun, **updates: object) -> AgentRun:
    if run.diagnosis is None:
        raise ValueError("synthetic scorer run has no diagnosis")
    return run.model_copy(update={"diagnosis": run.diagnosis.model_copy(update=updates)})


def _replace_evidence_query_id(run: AgentRun, query_id: str) -> AgentRun:
    if run.diagnosis is None:
        raise ValueError("synthetic scorer run has no diagnosis")
    evidence = [item.model_copy(update={"query_id": query_id}) for item in run.diagnosis.evidence]
    return _replace_diagnosis(run, evidence=evidence)


def _duplicate_evidence(run: AgentRun) -> AgentRun:
    if run.diagnosis is None:
        raise ValueError("synthetic scorer run has no diagnosis")
    return _replace_diagnosis(run, evidence=[*run.diagnosis.evidence, *run.diagnosis.evidence])


def _remove_evidence_claim_types(run: AgentRun) -> AgentRun:
    if run.diagnosis is None:
        raise ValueError("synthetic scorer run has no diagnosis")
    evidence = [item.model_copy(update={"claim_types": []}) for item in run.diagnosis.evidence]
    return _replace_diagnosis(run, evidence=evidence)


def _canonical_trace(run: AgentRun) -> ToolTrace:
    return run.tool_calls[0]


def _replace_trace_query(run: AgentRun, query: str) -> AgentRun:
    trace = _canonical_trace(run).model_copy(update={"input": {"query": query}})
    return run.model_copy(update={"tool_calls": [trace]})


def _remove_mechanism_row(run: AgentRun, key: str) -> AgentRun:
    trace = _canonical_trace(run)
    result = QueryResult.model_validate(trace.output)
    period_index = result.columns.index("period")
    if "side" in result.columns:
        period, side = {
            "normal_server_methods": ("normal", "server"),
            "abnormal_client_methods": ("abnormal", "client"),
            "abnormal_server_methods": ("abnormal", "server"),
        }[key]
        side_index = result.columns.index("side")
        rows = [
            row
            for row in result.rows
            if not (row[period_index] == period and row[side_index] == side)
        ]
    else:
        rows = [row for row in result.rows if row[period_index] != key]
    changed_result = result.model_copy(update={"rows": rows})
    changed_trace = trace.model_copy(update={"output": changed_result.model_dump(mode="json")})
    return run.model_copy(update={"tool_calls": [changed_trace]})


def _replace_mechanism_value(run: AgentRun, period: str, column: str, value: object) -> AgentRun:
    trace = _canonical_trace(run)
    result = QueryResult.model_validate(trace.output)
    period_index = result.columns.index("period")
    value_index = result.columns.index(column)
    rows = [
        [
            value if index == value_index and row[period_index] == period else item
            for index, item in enumerate(row)
        ]
        for row in result.rows
    ]
    changed_result = result.model_copy(update={"rows": rows})
    changed_trace = trace.model_copy(update={"output": changed_result.model_dump(mode="json")})
    return run.model_copy(update={"tool_calls": [changed_trace]})


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_fixture_file(fixture: AegisTransferScorerFixture, path: Path) -> None:
    stored = AegisTransferScorerFixture.model_validate_json(path.read_text())
    if stored != fixture:
        raise ValueError("Aegis transfer scorer object does not match its bound fixture file")


def _sha256_json(value: object) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def source_transfer_audit_sha256(audit: dict[str, object]) -> str:
    binding_fields = (
        "audit_schema_version",
        "mode",
        "dataset_revision",
        "adapter_revision",
        "pinned_source",
        "selection_audit",
        "case",
        "greptimedb",
        "source_audit",
        "ingestion",
        "edge_equality",
        "mechanism_evidence",
        "semantic_surfaces",
        "no_model_gates",
    )
    return _sha256_json({key: audit.get(key) for key in binding_fields})
