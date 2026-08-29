from __future__ import annotations

import hashlib
import json
import re
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
from semantic_rca_bench.datasets.aegis_transfer import (
    normalize_edge_result,
    normalize_jvm_exception_evidence,
    normalize_start_gap_evidence,
)
from semantic_rca_bench.evaluation import component_matches, is_valid_evidence_trace
from semantic_rca_bench.protocol import benchmark_protocol

CALIBRATION_SCORER_REVISION = "aegis-transfer-request-delay-v3"
FORMAL_SCORER_REVISION = "aegis-transfer-jvm-exception-v1"
CALIBRATION_SCORER_FIXTURE = Path("fixtures/reference/aegis-transfer-v28-calibration-scorer.json")
FORMAL_SCORER_FIXTURE = Path("fixtures/reference/aegis-transfer-v28-scorer.json")
_SCORER_IDENTITIES = {
    CALIBRATION_SCORER_REVISION: (
        "aegis-transfer-002",
        "development",
        "deepseek-v4-flash",
    ),
    FORMAL_SCORER_REVISION: ("aegis-transfer-003", "measurement", "deepseek-v4-pro"),
}


class TransferScorerGroundTruth(BaseModel):
    model_config = ConfigDict(frozen=True)

    affected_component: str
    causal_dependency: str | None
    causal_scope: CausalScope | None = None
    causal_operation: str | None = None
    accepted_causal_operations: tuple[str, ...] = ()
    fault_category: FaultCategory
    mechanism_code: MechanismCode | None = None
    source_fault_type: str


class TransferMechanismEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    predicate: str
    expected_result: dict[str, dict[str, int]]
    observable: str | None = None
    threshold_ns: int | None = None
    span_name: str | None = None
    minimum_anomalous_observations: int | None = None


class CanonicalApiRunner(BaseModel):
    model_config = ConfigDict(frozen=True)

    runner: AgentRunner
    model: str
    benchmark_protocol_version: int
    visibility_levels: tuple[Visibility, ...]
    max_tool_calls: int
    max_turns: int
    max_tokens: int
    repetitions: int
    treatment_order_seed: int
    parallel_runs: int
    sampling: str
    prompt_cache: str


class AegisTransferScorerFixture(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: int
    scorer_revision: str
    agent_case_id: str
    case_role: str
    ground_truth: TransferScorerGroundTruth
    normal_window: tuple[int, int]
    abnormal_window: tuple[int, int]
    mechanism_evidence: TransferMechanismEvidence
    canonical_api_runner: CanonicalApiRunner


class AegisTransferEvaluation(BaseModel):
    success: bool
    runner_contract_match: bool
    tool_budget_contract_match: bool
    affected_component_match: bool
    causal_dependency_match: bool
    declared_edge_match: bool
    fault_category_match: bool
    citations_execution_valid: bool
    mechanism_evidence_match: bool
    failure_reasons: list[str]
    cited_evidence_count: int
    valid_evidence_count: int
    supporting_evidence_query_ids: list[str]
    causal_scope_evidence_query_ids: list[str]
    mechanism_evidence_query_ids: list[str]
    correct_completion_tool_calls: int | None = None
    tool_calls_through_mechanism_evidence: int | None = None
    rows_returned_through_mechanism_evidence: int | None = None
    causal_scope_match: bool
    causal_operation_match: bool
    mechanism_code_match: bool
    diagnosis_correct: bool
    causal_scope_evidence_match: bool
    required_evidence_covered: bool
    citation_integrity: bool
    execution_reliability: bool
    auditable_completion: bool
    efficiency_eligible: bool


@dataclass(frozen=True)
class DelayEvidencePart:
    normal_samples: int = 0
    normal_max_gap_ns: int | None = None
    abnormal_samples: int = 0
    abnormal_min_gap_ns: int | None = None
    abnormal_max_gap_ns: int | None = None
    abnormal_confirmations: int = 0


@dataclass(frozen=True)
class ExceptionEvidencePart:
    normal_error_spans: int | None = None
    normal_exception_logs: int | None = None
    abnormal_error_spans: int | None = None
    abnormal_exception_logs: int | None = None


@dataclass(frozen=True)
class ExceptionQueryScope:
    statement: exp.Expression
    tables: frozenset[str]
    periods: frozenset[str]
    aggregate_periods_bound: bool


@dataclass(frozen=True)
class StartGapQueryScope:
    statement: exp.Expression
    client_alias: str
    server_alias: str
    aggregate_periods_bound: bool
    gap_output_columns: frozenset[str]
    timestamp_output_sources: dict[str, frozenset[str]]
    projections: dict[str, tuple[exp.Expression, ...]]


def canonical_api_runner_contract(*, model: str) -> dict[str, object]:
    selected_protocol = benchmark_protocol()
    return {
        "runner": AgentRunner.API.value,
        "model": model,
        "benchmark_protocol_version": selected_protocol["version"],
        "visibility_levels": [level.value for level in Visibility],
        "max_tool_calls": 48,
        "max_turns": 58,
        "max_tokens": 4096,
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
    if expected_identity is None or fixture.version != 2:
        raise ValueError("unsupported Aegis transfer scorer fixture revision")
    if fixture.agent_case_id != expected_identity[0]:
        raise ValueError("Aegis transfer scorer fixture uses the wrong opaque case ID")
    if fixture.case_role != expected_identity[1]:
        raise ValueError("Aegis transfer scorer fixture uses the wrong case role")
    if fixture.canonical_api_runner.model_dump(mode="json") != canonical_api_runner_contract(
        model=expected_identity[2],
    ):
        raise ValueError("Aegis transfer canonical API runner contract drifted")
    truth = fixture.ground_truth
    if truth.causal_scope is None:
        raise ValueError("Aegis transfer structured diagnosis contract drifted")
    if truth.causal_scope is CausalScope.DEPENDENCY_EDGE and not truth.causal_dependency:
        raise ValueError("dependency-scoped scorer has no causal dependency")
    if truth.causal_scope is CausalScope.COMPONENT and truth.causal_dependency is not None:
        raise ValueError("component-scoped scorer must not define a causal dependency")
    accepted_operations = truth.accepted_causal_operations or (
        (truth.causal_operation,) if truth.causal_operation else ()
    )
    if truth.causal_operation and truth.causal_operation not in accepted_operations:
        raise ValueError("canonical causal operation is not accepted by the scorer")
    if fixture.normal_window[1] != fixture.abnormal_window[0]:
        raise ValueError("Aegis transfer scorer windows must be contiguous")
    predicate = fixture.mechanism_evidence.predicate
    if predicate == "source_declared_http_client_server_start_gap":
        expected = fixture.mechanism_evidence.expected_result
        threshold = fixture.mechanism_evidence.threshold_ns
        normal = expected.get("normal", {})
        abnormal = expected.get("abnormal", {})
        if (
            threshold is None
            or not fixture.mechanism_evidence.span_name
            or fixture.mechanism_evidence.observable != "server.timestamp - client.timestamp"
            or (fixture.mechanism_evidence.minimum_anomalous_observations or 0) < 2
            or set(expected) != {"normal", "abnormal"}
            or normal.get("count", 0) <= 0
            or normal.get("max_start_gap_ns", threshold) >= threshold
            or normal.get("at_or_above_threshold", 1) != 0
            or abnormal.get("count", 0) <= 0
            or abnormal.get("min_start_gap_ns", -1) < threshold
            or abnormal.get("at_or_above_threshold") != abnormal.get("count")
        ):
            raise ValueError("start-gap scorer does not prove the frozen threshold transition")
    elif predicate == "source_declared_jvm_exception":
        expected = fixture.mechanism_evidence.expected_result
        normal = expected.get("normal", {})
        abnormal = expected.get("abnormal", {})
        if (
            fixture.ground_truth.causal_scope is not CausalScope.COMPONENT
            or fixture.ground_truth.mechanism_code is not MechanismCode.APPLICATION_ERROR
            or fixture.mechanism_evidence.observable != "error spans and exception logs"
            or (fixture.mechanism_evidence.minimum_anomalous_observations or 0) < 2
            or set(expected) != {"normal", "abnormal"}
            or normal.get("error_span_count") != 0
            or normal.get("exception_log_count") != 0
            or abnormal.get("error_span_count", 0)
            < fixture.mechanism_evidence.minimum_anomalous_observations
            or abnormal.get("exception_log_count", 0)
            < fixture.mechanism_evidence.minimum_anomalous_observations
        ):
            raise ValueError("JVM exception scorer does not prove the frozen transition")
    else:
        raise ValueError("unsupported Aegis transfer mechanism evidence predicate")
    return fixture


def evaluate_aegis_transfer_run(
    run: AgentRun,
    fixture: AegisTransferScorerFixture,
    *,
    expected_model: str | None = None,
) -> AegisTransferEvaluation:
    return _evaluate_structured_transfer_run(run, fixture, expected_model=expected_model)


def _evaluate_structured_transfer_run(
    run: AgentRun,
    fixture: AegisTransferScorerFixture,
    *,
    expected_model: str | None,
) -> AegisTransferEvaluation:
    diagnosis = run.diagnosis
    truth = fixture.ground_truth
    runner_contract_match = (
        run.runner is fixture.canonical_api_runner.runner
        and run.model == (expected_model or fixture.canonical_api_runner.model)
        and run.visibility in fixture.canonical_api_runner.visibility_levels
    )
    tool_budget_contract_match = len(run.tool_calls) <= fixture.canonical_api_runner.max_tool_calls
    affected_match = diagnosis is not None and component_matches(
        diagnosis.affected_component, truth.affected_component
    )
    if truth.causal_scope is CausalScope.COMPONENT:
        dependency_match = diagnosis is not None and diagnosis.causal_dependency is None
    else:
        dependency_match = (
            diagnosis is not None
            and diagnosis.causal_dependency is not None
            and truth.causal_dependency is not None
            and component_matches(diagnosis.causal_dependency, truth.causal_dependency)
        )
    declared_edge_match = affected_match and dependency_match
    causal_scope_match = diagnosis is not None and diagnosis.causal_scope is truth.causal_scope
    causal_operation_match = diagnosis is not None and _causal_operation_matches(
        diagnosis.causal_operation, truth
    )
    category_match = diagnosis is not None and diagnosis.fault_category is truth.fault_category
    mechanism_code_match = (
        diagnosis is not None and diagnosis.mechanism_code is truth.mechanism_code
    )
    diagnosis_correct = all(
        (
            declared_edge_match,
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
    mechanism_parts: list[tuple[str, int, DelayEvidencePart | ExceptionEvidencePart]] = []
    for item in evidence:
        matches = traces_by_query_id.get(item.query_id, [])
        if len(matches) != 1 or not is_valid_evidence_trace(matches):
            continue
        trace = matches[0]
        mechanism_part = _structured_mechanism_evidence_from_trace(trace, fixture)
        if EvidenceClaimType.CAUSAL_SCOPE in item.claim_types and (
            _trace_proves_causal_scope(trace, fixture) or mechanism_part is not None
        ):
            causal_support_ids.append(item.query_id)
        if EvidenceClaimType.FAULT_MECHANISM in item.claim_types and mechanism_part is not None:
            mechanism_parts.append((item.query_id, trace_indexes[id(trace)], mechanism_part))

    mechanism_evidence_match = _structured_parts_prove_transition(
        [part for _, _, part in mechanism_parts], fixture
    )
    mechanism_support_ids = [query_id for query_id, _, _ in mechanism_parts]
    supporting_ids = list(dict.fromkeys([*causal_support_ids, *mechanism_support_ids]))
    causal_scope_evidence_match = bool(causal_support_ids)
    required_evidence_covered = (
        typed_evidence and causal_scope_evidence_match and mechanism_evidence_match
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
    support_index = (
        max(index for _, index, _ in mechanism_parts) if mechanism_evidence_match else None
    )
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
        "affected component does not match the frozen causal scope": affected_match,
        "causal dependency does not match the frozen causal scope": dependency_match,
        "causal scope does not match the frozen source mechanism": causal_scope_match,
        "causal operation does not match a source-observed operation": causal_operation_match,
        "fault category does not match the frozen source mechanism": category_match,
        "structured mechanism does not match the frozen source mechanism": mechanism_code_match,
        "typed evidence does not support the declared causal scope": causal_scope_evidence_match,
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
        affected_component_match=affected_match,
        causal_dependency_match=dependency_match,
        declared_edge_match=declared_edge_match,
        fault_category_match=category_match,
        citations_execution_valid=citations_execution_valid,
        mechanism_evidence_match=mechanism_evidence_match,
        failure_reasons=[reason for reason, passed in checks.items() if not passed],
        cited_evidence_count=len(evidence),
        valid_evidence_count=valid_evidence_count,
        supporting_evidence_query_ids=(supporting_ids if required_evidence_covered else []),
        causal_scope_evidence_query_ids=(
            list(dict.fromkeys(causal_support_ids)) if causal_scope_evidence_match else []
        ),
        mechanism_evidence_query_ids=(
            list(dict.fromkeys(mechanism_support_ids)) if mechanism_evidence_match else []
        ),
        correct_completion_tool_calls=len(run.tool_calls) if efficiency_eligible else None,
        tool_calls_through_mechanism_evidence=(
            support_index + 1 if support_index is not None else None
        ),
        rows_returned_through_mechanism_evidence=rows_through_evidence,
        causal_scope_match=causal_scope_match,
        causal_operation_match=causal_operation_match,
        mechanism_code_match=mechanism_code_match,
        diagnosis_correct=diagnosis_correct,
        causal_scope_evidence_match=causal_scope_evidence_match,
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
    normal_start = datetime.fromtimestamp(fixture.normal_window[0], UTC).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
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
            "wrong_affected_component": (
                _replace_diagnosis(
                    canonical_run,
                    affected_component="wrong-service",
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
    if fixture.ground_truth.causal_scope is CausalScope.COMPONENT:
        cases["unexpected_dependency"] = (
            _replace_diagnosis(canonical_run, causal_dependency="unexpected-service"),
            False,
        )
    else:
        cases["reversed_edge"] = (
            _replace_diagnosis(
                canonical_run,
                affected_component=fixture.ground_truth.causal_dependency,
                causal_dependency=fixture.ground_truth.affected_component,
            ),
            False,
        )
        cases["missing_dependency"] = (
            _replace_diagnosis(canonical_run, causal_dependency=None),
            False,
        )
    if fixture.mechanism_evidence.predicate == "source_declared_http_client_server_start_gap":
        cases.update(
            {
                "equivalent_case_normalized_pair_scope": (
                    _replace_trace_query(
                        canonical_run,
                        canonical_query.replace(
                            "c.span_kind = 'SPAN_KIND_CLIENT'",
                            "UPPER(c.span_kind) = 'SPAN_KIND_CLIENT'",
                        )
                        .replace(
                            "s.span_kind = 'SPAN_KIND_SERVER'",
                            "LOWER(s.span_kind) = 'span_kind_server'",
                        )
                        .replace(
                            f"c.service_name = '{fixture.ground_truth.affected_component}'",
                            "LOWER(c.service_name) = "
                            f"'{fixture.ground_truth.affected_component.lower()}'",
                        )
                        .replace(
                            f"s.service_name = '{fixture.ground_truth.causal_dependency}'",
                            f"s.service_name IN ('{fixture.ground_truth.causal_dependency}')",
                        ),
                    ),
                    True,
                ),
                "neutralized_client_role": (
                    _replace_trace_query(
                        canonical_run,
                        canonical_query.replace(
                            "c.span_kind = 'SPAN_KIND_CLIENT'",
                            "(c.span_kind = 'SPAN_KIND_CLIENT' OR 1 = 1)",
                        ),
                    ),
                    False,
                ),
                "neutralized_parent_relation": (
                    _replace_trace_query(
                        canonical_run,
                        canonical_query.replace(
                            "s.parent_span_id = c.span_id",
                            "(s.parent_span_id = c.span_id OR 1 = 1)",
                        ),
                    ),
                    False,
                ),
                "wrong_parent_relation": (
                    _replace_trace_query(
                        canonical_run,
                        canonical_query.replace(
                            "s.parent_span_id = c.span_id",
                            "s.parent_span_id <> c.span_id",
                        ),
                    ),
                    False,
                ),
                "reversed_start_gap": (
                    _replace_trace_query(
                        canonical_run,
                        canonical_query.replace(
                            "CAST(s.timestamp AS BIGINT) - CAST(c.timestamp AS BIGINT)",
                            "CAST(c.timestamp AS BIGINT) - CAST(s.timestamp AS BIGINT)",
                        ),
                    ),
                    False,
                ),
                "unrelated_max_aliased_as_gap": (
                    _replace_trace_query(
                        canonical_run,
                        canonical_query.replace(
                            "MAX(server_start_gap_ns) AS max_start_gap_ns",
                            "MAX(server_duration_ns) AS max_start_gap_ns",
                        ),
                    ),
                    False,
                ),
            }
        )
    elif fixture.mechanism_evidence.predicate == "source_declared_jvm_exception":
        cases.update(
            {
                "equivalent_case_normalized_predicates": (
                    _replace_trace_query(
                        canonical_run,
                        canonical_query.replace(
                            f"service_name = '{fixture.ground_truth.affected_component}'",
                            "LOWER(service_name) = LOWER("
                            f"'{fixture.ground_truth.affected_component.upper()}')",
                        ).replace(
                            "span_status_code = 'STATUS_CODE_ERROR'",
                            "UPPER(span_status_code) = 'STATUS_CODE_ERROR'",
                        ),
                    ),
                    True,
                ),
                "wrong_source_span_status": (
                    _replace_trace_query(
                        canonical_run,
                        canonical_query.replace(
                            "span_status_code = 'STATUS_CODE_ERROR'",
                            "span_status_code = 'STATUS_CODE_UNSET'",
                        ),
                    ),
                    False,
                ),
                "wrong_source_service": (
                    _replace_trace_query(
                        canonical_run,
                        canonical_query.replace(
                            fixture.ground_truth.affected_component,
                            "wrong-service",
                        ),
                    ),
                    False,
                ),
                "wrong_causal_operation_filter": (
                    _replace_trace_query(
                        canonical_run,
                        canonical_query.replace("retrievebyname", "listtrains"),
                    ),
                    False,
                ),
                "symptom_only_without_exception": (
                    _replace_trace_query(
                        canonical_run,
                        canonical_query.replace("%exception%", "%failed%"),
                    ),
                    False,
                ),
                "neutralized_service_filter": (
                    _replace_trace_query(
                        canonical_run,
                        canonical_query.replace(
                            f"service_name = '{fixture.ground_truth.affected_component}'",
                            (
                                "(service_name = "
                                f"'{fixture.ground_truth.affected_component}' OR 1 = 1)"
                            ),
                            1,
                        ),
                    ),
                    False,
                ),
                "hardcoded_span_count": (
                    _replace_trace_query(
                        canonical_run,
                        canonical_query.replace(
                            "COALESCE(SUM(e.error_span_count), 0) AS error_span_count",
                            "1981 AS error_span_count",
                        ),
                    ),
                    False,
                ),
                "wrong_aggregate_source": (
                    _replace_trace_query(
                        canonical_run,
                        canonical_query.replace(
                            "SUM(e.error_span_count)",
                            "SUM(e.exception_log_count)",
                            1,
                        ),
                    ),
                    False,
                ),
                "dead_subquery_status_predicate": (
                    _replace_trace_query(
                        canonical_run,
                        canonical_query.replace(
                            "span_status_code = 'STATUS_CODE_ERROR'",
                            "span_status_code = 'STATUS_CODE_UNSET' "
                            "AND EXISTS (SELECT 1 WHERE "
                            "span_status_code = 'STATUS_CODE_ERROR')",
                        ),
                    ),
                    False,
                ),
                "unbounded_trace_scan": (
                    _replace_trace_query(
                        canonical_run,
                        canonical_query.replace(
                            f"    AND timestamp >= '{normal_start}' "
                            f"AND timestamp < '{abnormal_end}'\n",
                            "",
                            1,
                        ),
                    ),
                    False,
                ),
                "multiplied_trace_rows": (
                    _replace_trace_query(
                        canonical_run,
                        canonical_query.replace(
                            "  FROM traces\n",
                            "  FROM traces CROSS JOIN "
                            "(SELECT 1 AS duplicate UNION ALL "
                            "SELECT 2 AS duplicate) copies\n",
                            1,
                        ),
                    ),
                    False,
                ),
                "mislabeled_aggregate_period": (
                    _replace_trace_query(
                        canonical_run,
                        canonical_query.replace("THEN 'normal'", "THEN 'abnormal'", 1),
                    ),
                    False,
                ),
                "dirty_baseline": (
                    _replace_mechanism_value(canonical_run, "normal", "error_span_count", 1),
                    False,
                ),
                "missing_anomalous_logs": (
                    _replace_mechanism_value(canonical_run, "abnormal", "exception_log_count", 0),
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
            == canonical_api_runner_contract(model=scorer_identity[2])
        ),
        "opaque_agent_input": opaque_case_gate,
        "scorer_regressions": scorer_regressions_pass,
    }
    gates["all_passed"] = all(gates.values())
    return {
        "audit_schema_version": 1,
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


def _trace_proves_declared_edge(
    trace: ToolTrace,
    fixture: AegisTransferScorerFixture,
) -> bool:
    if trace.error is not None or not isinstance(trace.output, dict):
        return False
    if trace.tool_name not in {"execute_sql", "query_semantic_graph"}:
        return False
    if trace.tool_name == "execute_sql":
        query = str(trace.input.get("query") or trace.input.get("sql") or "").lower()
        if "semantic_relationships" not in query:
            return False
    try:
        result = QueryResult.model_validate(trace.output)
    except ValueError:
        return False
    edges = normalize_edge_result(result)
    if edges is None:
        return False
    truth = fixture.ground_truth
    return any(
        edge["src_type"] == "service"
        and edge["src_id"] == truth.affected_component
        and edge["dst_type"] == "service"
        and edge["dst_id"] == truth.causal_dependency
        and edge["rel_type"] == "calls"
        and edge["provenance"] == "trace"
        and int(edge["request_count"]) > 0
        for edge in edges
    )


def _trace_proves_causal_scope(
    trace: ToolTrace,
    fixture: AegisTransferScorerFixture,
) -> bool:
    if fixture.ground_truth.causal_scope is CausalScope.DEPENDENCY_EDGE:
        return _trace_proves_declared_edge(trace, fixture)
    if trace.error is not None or not isinstance(trace.output, dict):
        return False
    if trace.tool_name not in {"execute_sql", "query_semantic_graph"}:
        return False
    if trace.tool_name == "execute_sql":
        query = str(trace.input.get("query") or trace.input.get("sql") or "").lower()
        if "semantic_entities" not in query:
            return False
    try:
        result = QueryResult.model_validate(trace.output)
    except ValueError:
        return False
    columns = [column.lower() for column in result.columns]
    if result.truncated or columns.count("entity_type") != 1 or columns.count("entity_id") != 1:
        return False
    type_index = columns.index("entity_type")
    id_index = columns.index("entity_id")
    return any(
        len(row) > max(type_index, id_index)
        and str(row[type_index]).lower() == "service"
        and component_matches(str(row[id_index]), fixture.ground_truth.affected_component)
        for row in result.rows
    )


def _structured_mechanism_evidence_from_trace(
    trace: ToolTrace,
    fixture: AegisTransferScorerFixture,
) -> DelayEvidencePart | ExceptionEvidencePart | None:
    if fixture.mechanism_evidence.predicate == "source_declared_http_client_server_start_gap":
        return _start_gap_evidence_from_trace(trace, fixture)
    if fixture.mechanism_evidence.predicate == "source_declared_jvm_exception":
        return _jvm_exception_evidence_from_trace(trace, fixture)
    return None


def _structured_parts_prove_transition(
    parts: list[DelayEvidencePart | ExceptionEvidencePart],
    fixture: AegisTransferScorerFixture,
) -> bool:
    if fixture.mechanism_evidence.predicate == "source_declared_http_client_server_start_gap":
        if any(not isinstance(part, DelayEvidencePart) for part in parts):
            return False
        return _start_gap_parts_prove_transition(
            [part for part in parts if isinstance(part, DelayEvidencePart)], fixture
        )
    if fixture.mechanism_evidence.predicate == "source_declared_jvm_exception":
        if any(not isinstance(part, ExceptionEvidencePart) for part in parts):
            return False
        return _exception_parts_prove_transition(
            [part for part in parts if isinstance(part, ExceptionEvidencePart)], fixture
        )
    return False


def _jvm_exception_evidence_from_trace(
    trace: ToolTrace,
    fixture: AegisTransferScorerFixture,
) -> ExceptionEvidencePart | None:
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
    query = str(trace.input.get("query") or trace.input.get("sql") or "")
    scope = _jvm_exception_query_scope(query, fixture)
    if scope is None:
        return None
    statement = scope.statement
    tables = set(scope.tables)
    periods = set(scope.periods)
    normalized = normalize_jvm_exception_evidence(result)
    if (
        normalized is not None
        and tables == {"traces", "logs"}
        and scope.aggregate_periods_bound
        and _output_alias_has_aggregate(statement, "error_span_count")
        and _output_alias_has_aggregate(statement, "exception_log_count")
    ):
        return ExceptionEvidencePart(
            normal_error_spans=normalized["normal"]["error_span_count"],
            normal_exception_logs=normalized["normal"]["exception_log_count"],
            abnormal_error_spans=normalized["abnormal"]["error_span_count"],
            abnormal_exception_logs=normalized["abnormal"]["exception_log_count"],
        )
    if len(tables) != 1:
        return None
    table = next(iter(tables))
    counts = _period_counts_from_result(result, statement, periods)
    if counts is not None:
        if len(periods) > 1 and not scope.aggregate_periods_bound:
            return None
        return _exception_part_from_counts(table, counts)
    raw_counts = _raw_exception_counts(result, table, fixture)
    return _exception_part_from_counts(table, raw_counts) if raw_counts is not None else None


def _jvm_exception_query_scope(
    query: str,
    fixture: AegisTransferScorerFixture,
) -> ExceptionQueryScope | None:
    statement = _parse_single_statement(query)
    if statement is None or any(statement.find_all(exp.Limit)):
        return None
    tables = {
        table.name.lower()
        for table in statement.find_all(exp.Table)
        if table.name.lower() in {"traces", "logs"}
    }
    if not tables:
        return None
    if any(_identity_literal_comparison(item) for item in statement.find_all(exp.EQ, exp.In)):
        return None
    service = fixture.ground_truth.affected_component
    allowed_epochs = {
        fixture.normal_window[0],
        fixture.normal_window[1],
        fixture.abnormal_window[1],
    }
    observed_epochs = _timestamp_literal_epochs(statement)
    if observed_epochs - allowed_epochs:
        return None
    operations = fixture.ground_truth.accepted_causal_operations or (
        fixture.ground_truth.causal_operation or "",
    )
    operation_tokens = {
        token
        for operation in operations
        for token in (operation, operation.rsplit(".", 1)[-1])
        if token
    }
    periods: set[str] = set()
    aggregate_periods_bound = True
    observed_telemetry_scopes: set[tuple[str, str]] = set()
    for table in tables:
        table_scopes = _table_select_scopes(statement, table)
        if not table_scopes:
            return None
        time_column = "timestamp" if table == "traces" else "greptime_timestamp"
        for table_scope in table_scopes:
            scope_identity = (table, table_scope.sql())
            if scope_identity in observed_telemetry_scopes or any(
                join.find_ancestor(exp.Select) is table_scope
                for join in table_scope.find_all(exp.Join)
            ):
                return None
            observed_telemetry_scopes.add(scope_identity)
            if not _has_column_in_literals(
                table_scope,
                "service_name",
                {service},
                allow_case_normalization=True,
            ):
                return None
            if table == "traces" and (
                not _has_column_in_literals(
                    table_scope,
                    "span_status_code",
                    {"STATUS_CODE_ERROR"},
                    allow_case_normalization=True,
                )
                or not any(
                    _has_column_text_fragment(
                        table_scope,
                        "span_name",
                        token,
                        allow_case_normalization=True,
                    )
                    for token in operation_tokens
                )
            ):
                return None
            if table == "logs" and not (
                _has_column_like_fragment(
                    table_scope,
                    "line",
                    "exception",
                    allow_case_normalization=True,
                )
                and _has_column_in_literals(
                    table_scope,
                    "level",
                    {"ERROR", "SEVERE", "FATAL"},
                    allow_case_normalization=True,
                )
            ):
                return None
            scope_periods = _exact_scope_periods(table_scope, time_column, fixture)
            if not scope_periods:
                return None
            periods.update(scope_periods)
            aggregate_periods_bound = aggregate_periods_bound and _scope_binds_period(
                table_scope,
                time_column,
                scope_periods,
                fixture,
            )
    if not periods or not periods <= {"normal", "abnormal"}:
        return None
    return ExceptionQueryScope(
        statement=statement,
        tables=frozenset(tables),
        periods=frozenset(periods),
        aggregate_periods_bound=aggregate_periods_bound,
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


def _has_column_like_fragment(
    statement: exp.Expression,
    column_name: str,
    fragment: str,
    *,
    allow_case_normalization: bool = False,
) -> bool:
    for like in statement.find_all(exp.Like, exp.ILike):
        if not _belongs_to_select_scope(like, statement):
            continue
        normalization = _column_normalization(like.this, column_name)
        target_matches = isinstance(like.this, exp.Column) and (
            like.this.name.lower() == column_name.lower()
        )
        if normalization is not None:
            target_matches = allow_case_normalization
        value = _evaluated_string_literal(like.expression)
        if isinstance(like, exp.ILike) and value is not None:
            value = value.lower()
            expected_fragment = fragment.lower()
        else:
            expected_fragment = _normalize_string(fragment, normalization)
        if (
            target_matches
            and value is not None
            and _like_pattern_covers_fragment(value, expected_fragment)
            and _is_positive_filter_predicate(like)
        ):
            return True
    return False


def _has_column_text_fragment(
    statement: exp.Expression,
    column_name: str,
    fragment: str,
    *,
    allow_case_normalization: bool = False,
) -> bool:
    if _has_column_like_fragment(
        statement,
        column_name,
        fragment,
        allow_case_normalization=allow_case_normalization,
    ):
        return True
    for comparison in statement.find_all(exp.EQ, exp.In):
        if not _belongs_to_select_scope(comparison, statement) or not _is_positive_filter_predicate(
            comparison
        ):
            continue
        if isinstance(comparison, exp.EQ):
            pairs: tuple[tuple[exp.Expression, exp.Expression], ...] = (
                (comparison.this, comparison.expression),
                (comparison.expression, comparison.this),
            )
        else:
            pairs = tuple((comparison.this, value) for value in comparison.expressions)
        matches = []
        for column, value in pairs:
            normalization = _column_normalization(column, column_name)
            target_matches = isinstance(column, exp.Column) and (
                column.name.lower() == column_name.lower()
            )
            if normalization is not None:
                target_matches = allow_case_normalization
            literal = _evaluated_string_literal(value)
            matches.append(
                target_matches
                and literal is not None
                and _normalize_string(fragment, normalization) in literal
            )
        if matches and (all(matches) if isinstance(comparison, exp.In) else any(matches)):
            return True
    return False


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


def _like_pattern_covers_fragment(pattern: str, fragment: str) -> bool:
    return "_" not in pattern and "\\" not in pattern and pattern.replace("%", "") == fragment


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


def _output_alias_has_aggregate(statement: exp.Expression, alias: str) -> bool:
    for select in _result_selects(statement):
        for projection in select.expressions:
            if projection.alias_or_name.lower() != alias.lower():
                continue
            if any(projection.find_all(exp.AggFunc)) and any(
                column.name.lower() == alias.lower() for column in projection.find_all(exp.Column)
            ):
                return True
    return False


def _result_selects(statement: exp.Expression) -> tuple[exp.Select, ...]:
    if isinstance(statement, exp.Select):
        return (statement,)
    if isinstance(statement, exp.Subquery):
        return _result_selects(statement.this)
    if isinstance(statement, exp.Union):
        return (*_result_selects(statement.this), *_result_selects(statement.expression))
    return ()


def _period_counts_from_result(
    result: QueryResult,
    statement: exp.Expression,
    periods: set[str],
) -> dict[str, int] | None:
    columns = [column.lower() for column in result.columns]
    count_aliases = {
        projection.alias_or_name.lower()
        for select in _result_selects(statement)
        for projection in select.expressions
        if projection.alias_or_name and _is_row_count_aggregate(projection)
    }
    count_columns = [column for column in columns if column in count_aliases]
    if len(count_columns) != 1:
        return None
    count_index = columns.index(count_columns[0])
    if columns.count("period") == 1:
        period_index = columns.index("period")
        counts: dict[str, int] = {}
        for row in result.rows:
            if not _row_covers(row, period_index, count_index):
                return None
            period = str(row[period_index]).lower()
            count = _strict_int(row[count_index])
            if period not in periods or count is None or count < 0:
                return None
            counts[period] = counts.get(period, 0) + count
        return counts or None
    if len(periods) != 1 or len(result.rows) != 1:
        return None
    if not _row_covers(result.rows[0], count_index):
        return None
    count = _strict_int(result.rows[0][count_index])
    return {next(iter(periods)): count} if count is not None and count >= 0 else None


def _is_row_count_aggregate(projection: exp.Expression) -> bool:
    if any(projection.find_all(exp.Count)):
        return True
    for summation in projection.find_all(exp.Sum):
        value = summation.this
        if isinstance(value, exp.Literal) and _strict_int_literal(value) == 1:
            return True
        if not isinstance(value, exp.Case):
            continue
        outputs = [branch.args.get("true") for branch in value.args.get("ifs") or []]
        outputs.append(value.args.get("default"))
        integers = {
            parsed
            for output in outputs
            if isinstance(output, exp.Literal)
            and (parsed := _strict_int_literal(output)) is not None
        }
        if integers and integers <= {0, 1} and 1 in integers:
            return True
    return False


def _raw_exception_counts(
    result: QueryResult,
    table: str,
    fixture: AegisTransferScorerFixture,
) -> dict[str, int] | None:
    columns = [column.lower() for column in result.columns]
    time_column = "timestamp" if table == "traces" else "greptime_timestamp"
    required = (
        {time_column, "service_name", "span_name", "span_status_code"}
        if table == "traces"
        else {time_column, "service_name", "level", "line"}
    )
    if not required.issubset(columns):
        return None
    indexes = {name: columns.index(name) for name in required}
    counts = {"normal": 0, "abnormal": 0}
    for row in result.rows:
        if not _row_covers(row, *indexes.values()):
            return None
        timestamp = _timestamp_ns(row[indexes[time_column]])
        if timestamp is None:
            return None
        epoch = timestamp // 1_000_000_000
        if fixture.normal_window[0] <= epoch < fixture.normal_window[1]:
            counts["normal"] += 1
        elif fixture.abnormal_window[0] <= epoch < fixture.abnormal_window[1]:
            counts["abnormal"] += 1
        else:
            return None
    return counts


def _exception_part_from_counts(table: str, counts: dict[str, int]) -> ExceptionEvidencePart:
    if table == "traces":
        return ExceptionEvidencePart(
            normal_error_spans=counts.get("normal"),
            abnormal_error_spans=counts.get("abnormal"),
        )
    return ExceptionEvidencePart(
        normal_exception_logs=counts.get("normal"),
        abnormal_exception_logs=counts.get("abnormal"),
    )


def _start_gap_evidence_from_trace(
    trace: ToolTrace,
    fixture: AegisTransferScorerFixture,
) -> DelayEvidencePart | None:
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
    query = str(trace.input.get("query") or trace.input.get("sql") or "")
    scope = _start_gap_query_scope(query, fixture)
    if scope is None:
        return None

    normalized = normalize_start_gap_evidence(result)
    if normalized is not None:
        if (
            not {
                "min_start_gap_ns",
                "max_start_gap_ns",
            }.issubset(scope.gap_output_columns)
            or not scope.aggregate_periods_bound
        ):
            return None
        normal = normalized.get("normal", {})
        abnormal = normalized.get("abnormal", {})
        threshold = int(fixture.mechanism_evidence.threshold_ns or 0)
        threshold_is_bound = _threshold_output_is_bound(
            scope,
            "at_or_above_threshold",
            threshold,
        )
        for values in (normal, abnormal):
            count = int(values.get("count", 0))
            minimum = _optional_int(values.get("min_start_gap_ns"))
            maximum = _optional_int(values.get("max_start_gap_ns"))
            threshold_count = int(values.get("at_or_above_threshold", 0))
            if threshold_is_bound and (
                (minimum is not None and minimum >= threshold and threshold_count != count)
                or (maximum is not None and maximum < threshold and threshold_count != 0)
            ):
                return None
        return DelayEvidencePart(
            normal_samples=int(normal.get("count", 0)),
            normal_max_gap_ns=_optional_int(normal.get("max_start_gap_ns")),
            abnormal_samples=int(abnormal.get("count", 0)),
            abnormal_min_gap_ns=_optional_int(abnormal.get("min_start_gap_ns")),
            abnormal_max_gap_ns=_optional_int(abnormal.get("max_start_gap_ns")),
            abnormal_confirmations=(
                int(abnormal.get("at_or_above_threshold", 0)) if threshold_is_bound else 0
            ),
        )
    flexible = _flexible_period_start_gap_result(result, fixture, scope)
    if flexible is not None:
        return flexible
    raw = _raw_start_gap_result(result, fixture, scope)
    if raw is not None:
        return raw
    return _time_binned_start_gap_result(result, query, fixture, scope)


def _start_gap_query_scope(
    query: str,
    fixture: AegisTransferScorerFixture,
) -> StartGapQueryScope | None:
    statement = _parse_single_statement(query)
    if statement is None or any(statement.find_all(exp.Limit)):
        return None
    if any(
        _identity_literal_comparison(comparison)
        for comparison in statement.find_all(exp.EQ, exp.In)
    ):
        return None
    candidates: list[tuple[exp.Select, str, str]] = []
    for select in statement.find_all(exp.Select):
        direct_tables = [
            table
            for table in select.find_all(exp.Table)
            if table.find_ancestor(exp.Select) is select
        ]
        if len(direct_tables) != 2 or any(
            table.name.lower() != "traces" for table in direct_tables
        ):
            continue
        trace_aliases = {table.alias_or_name.lower() for table in direct_tables}
        if len(trace_aliases) != 2:
            continue
        client_aliases = (
            _aliases_matching_literal(
                select,
                "span_kind",
                "SPAN_KIND_CLIENT",
                allow_case_normalization=True,
            )
            & _aliases_matching_literal(
                select,
                "service_name",
                fixture.ground_truth.affected_component,
                allow_case_normalization=True,
            )
            & trace_aliases
        )
        server_aliases = (
            _aliases_matching_literal(
                select,
                "span_kind",
                "SPAN_KIND_SERVER",
                allow_case_normalization=True,
            )
            & _aliases_matching_literal(
                select,
                "service_name",
                str(fixture.ground_truth.causal_dependency),
                allow_case_normalization=True,
            )
            & _aliases_matching_literal(
                select,
                "span_name",
                str(fixture.mechanism_evidence.span_name),
            )
            & trace_aliases
        )
        if len(client_aliases) != 1 or len(server_aliases) != 1:
            continue
        client_alias = next(iter(client_aliases))
        server_alias = next(iter(server_aliases))
        if client_alias == server_alias:
            continue
        equalities = [
            equality
            for equality in select.find_all(exp.EQ)
            if _belongs_to_select_scope(equality, select)
        ]
        if not (
            _has_qualified_column_equality(
                equalities,
                client_alias,
                "trace_id",
                server_alias,
                "trace_id",
            )
            and _has_qualified_column_equality(
                equalities,
                server_alias,
                "parent_span_id",
                client_alias,
                "span_id",
            )
        ):
            continue
        client_bounds = _time_bounds(
            select,
            "timestamp",
            filters_only=True,
            table_alias=client_alias,
        )
        if client_bounds != {
            ("gte", fixture.normal_window[0]),
            ("lt", fixture.abnormal_window[1]),
        }:
            continue
        candidates.append((select, client_alias, server_alias))
    if len(candidates) != 1:
        return None
    source_scope, client_alias, server_alias = candidates[0]
    result_lineage = _result_lineage_selects(statement, source_scope)
    if result_lineage is None:
        return None
    projections = _select_projections(result_lineage)
    timestamp_sources = _timestamp_output_sources(projections)
    gap_outputs = _gap_output_columns(
        projections,
        client_alias=client_alias,
        server_alias=server_alias,
    )
    return StartGapQueryScope(
        statement=statement,
        client_alias=client_alias,
        server_alias=server_alias,
        aggregate_periods_bound=_scope_binds_period(
            source_scope,
            "timestamp",
            {"normal", "abnormal"},
            fixture,
            table_alias=client_alias,
        ),
        gap_output_columns=frozenset(gap_outputs),
        timestamp_output_sources={
            key: frozenset(value) for key, value in timestamp_sources.items()
        },
        projections={key: tuple(value) for key, value in projections.items()},
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


def _aliases_matching_literal(
    statement: exp.Expression,
    column_name: str,
    literal_value: str,
    *,
    allow_case_normalization: bool = False,
) -> set[str]:
    matches = set()
    for comparison in statement.find_all(exp.EQ, exp.In):
        if not _belongs_to_select_scope(comparison, statement) or not _is_positive_filter_predicate(
            comparison
        ):
            continue
        pairs = (
            (
                (comparison.this, comparison.expression),
                (comparison.expression, comparison.this),
            )
            if isinstance(comparison, exp.EQ)
            else tuple((comparison.this, value) for value in comparison.expressions)
        )
        if isinstance(comparison, exp.In) and len(comparison.expressions) != 1:
            continue
        for column, value in pairs:
            if not _column_literal_matches(
                column,
                value,
                column_name,
                literal_value,
                allow_case_normalization=allow_case_normalization,
            ):
                continue
            source_column = (
                column
                if isinstance(column, exp.Column)
                else column.this
                if isinstance(column, (exp.Lower, exp.Upper))
                else None
            )
            if isinstance(source_column, exp.Column) and source_column.table:
                matches.add(source_column.table.lower())
    return matches


def _has_qualified_column_equality(
    equalities: list[exp.EQ],
    left_table: str,
    left_name: str,
    right_table: str,
    right_name: str,
) -> bool:
    expected = {
        (left_table.lower(), left_name.lower()),
        (right_table.lower(), right_name.lower()),
    }
    return any(
        isinstance(equality.this, exp.Column)
        and isinstance(equality.expression, exp.Column)
        and {
            (equality.this.table.lower(), equality.this.name.lower()),
            (equality.expression.table.lower(), equality.expression.name.lower()),
        }
        == expected
        and _is_positive_filter_predicate(equality)
        for equality in equalities
    )


def _result_lineage_selects(
    statement: exp.Expression,
    source_select: exp.Select,
) -> tuple[exp.Select, ...] | None:
    root = build_scope(statement)
    if root is None:
        return None
    scopes: list[Scope] = []
    pending = [root]
    while pending:
        scope = pending.pop()
        if any(scope is observed for observed in scopes):
            continue
        scopes.append(scope)
        for source in scope.sources.values():
            if isinstance(source, Scope):
                pending.append(source)
            elif isinstance(source, exp.Table) and scope.expression is not source_select:
                return None
    selects = tuple(
        scope.expression for scope in scopes if isinstance(scope.expression, exp.Select)
    )
    return selects if any(select is source_select for select in selects) else None


def _select_projections(
    selects: tuple[exp.Select, ...],
) -> dict[str, list[exp.Expression]]:
    projections: dict[str, list[exp.Expression]] = {}
    for select in selects:
        for projection in select.expressions:
            alias = projection.alias_or_name.lower()
            if alias:
                projections.setdefault(alias, []).append(
                    projection.this if isinstance(projection, exp.Alias) else projection
                )
    return projections


def _timestamp_output_sources(
    projections: dict[str, list[exp.Expression]],
) -> dict[str, set[str]]:
    sources: dict[str, set[str]] = {alias: set() for alias in projections}
    changed = True
    while changed:
        changed = False
        for alias, expressions in projections.items():
            observed = set(sources[alias])
            for expression in expressions:
                for column in expression.find_all(exp.Column):
                    if column.name.lower() == "timestamp" and column.table:
                        observed.add(column.table.lower())
                    observed.update(sources.get(column.name.lower(), set()))
            if observed != sources[alias]:
                sources[alias] = observed
                changed = True
    return sources


def _gap_output_columns(
    projections: dict[str, list[exp.Expression]],
    *,
    client_alias: str,
    server_alias: str,
) -> set[str]:
    gap_columns = {
        alias
        for alias, expressions in projections.items()
        if any(
            _contains_ordered_timestamp_gap(
                expression,
                client_alias=client_alias,
                server_alias=server_alias,
            )
            for expression in expressions
        )
    }
    changed = True
    while changed:
        changed = False
        for alias, expressions in projections.items():
            if alias in gap_columns:
                continue
            if any(
                any(
                    column.name.lower() in gap_columns for column in expression.find_all(exp.Column)
                )
                for expression in expressions
            ):
                gap_columns.add(alias)
                changed = True
    return gap_columns


def _contains_ordered_timestamp_gap(
    expression: exp.Expression,
    *,
    client_alias: str,
    server_alias: str,
) -> bool:
    for subtraction in expression.find_all(exp.Sub):
        left_columns = {
            (column.table.lower(), column.name.lower())
            for column in subtraction.this.find_all(exp.Column)
        }
        right_columns = {
            (column.table.lower(), column.name.lower())
            for column in subtraction.expression.find_all(exp.Column)
        }
        if (server_alias, "timestamp") in left_columns and (
            client_alias,
            "timestamp",
        ) in right_columns:
            return True
    return False


def _threshold_output_is_bound(
    scope: StartGapQueryScope,
    column: str,
    threshold: int,
) -> bool:
    for expression in scope.projections.get(column.lower(), ()):
        if not any(
            child.name.lower() in scope.gap_output_columns
            for child in expression.find_all(exp.Column)
        ):
            continue
        for comparison in expression.find_all(exp.GTE):
            if (
                any(
                    child.name.lower() in scope.gap_output_columns
                    for child in comparison.this.find_all(exp.Column)
                )
                and isinstance(comparison.expression, exp.Literal)
                and not comparison.expression.is_string
                and _strict_int_literal(comparison.expression) == threshold
            ):
                return True
    return False


def _identity_literal_comparison(comparison: exp.Expression) -> bool:
    columns = list(comparison.find_all(exp.Column))
    if not any(
        column.name.lower() in {"trace_id", "span_id", "parent_span_id"} for column in columns
    ):
        return False
    return any(comparison.find_all(exp.Literal))


def _flexible_period_start_gap_result(
    result: QueryResult,
    fixture: AegisTransferScorerFixture,
    scope: StartGapQueryScope,
) -> DelayEvidencePart | None:
    columns = [column.lower() for column in result.columns]
    period_index = _unique_column(columns, lambda value: value == "period")
    count_index = _unique_column(columns, _is_count_column)
    max_index = _unique_column(columns, lambda value: "max" in value and "gap" in value)
    if None in {period_index, count_index, max_index}:
        return None
    min_index = _unique_column(columns, lambda value: "min" in value and "gap" in value)
    threshold_index = _unique_column(
        columns, lambda value: "threshold" in value and ("count" in value or "above" in value)
    )
    if columns[max_index] not in scope.gap_output_columns:
        return None
    if min_index is not None and columns[min_index] not in scope.gap_output_columns:
        return None
    if not scope.aggregate_periods_bound:
        return None
    threshold = int(fixture.mechanism_evidence.threshold_ns or 0)
    threshold_is_bound = threshold_index is not None and _threshold_output_is_bound(
        scope,
        columns[threshold_index],
        threshold,
    )
    periods: dict[str, dict[str, int | None]] = {}
    for row in result.rows:
        required_indexes = [period_index, count_index, max_index]
        if min_index is not None:
            required_indexes.append(min_index)
        if threshold_is_bound and threshold_index is not None:
            required_indexes.append(threshold_index)
        if not _row_covers(row, *required_indexes):
            return None
        period = str(row[period_index])
        if period not in {"normal", "abnormal"} or period in periods:
            return None
        count = _strict_int(row[count_index])
        maximum = _gap_to_ns(row[max_index], columns[max_index])
        minimum = _gap_to_ns(row[min_index], columns[min_index]) if min_index is not None else None
        threshold_count = _strict_int(row[threshold_index]) if threshold_is_bound else 0
        if (
            count is None
            or count <= 0
            or maximum is None
            or (min_index is not None and minimum is None)
            or threshold_count is None
            or not 0 <= threshold_count <= count
        ):
            return None
        periods[period] = {
            "count": count,
            "maximum": maximum,
            "minimum": minimum,
            "threshold_count": threshold_count,
        }
    normal = periods.get("normal", {})
    abnormal = periods.get("abnormal", {})
    return DelayEvidencePart(
        normal_samples=int(normal.get("count", 0)),
        normal_max_gap_ns=_optional_int(normal.get("maximum")),
        abnormal_samples=int(abnormal.get("count", 0)),
        abnormal_min_gap_ns=_optional_int(abnormal.get("minimum")),
        abnormal_max_gap_ns=_optional_int(abnormal.get("maximum")),
        abnormal_confirmations=int(abnormal.get("threshold_count", 0)),
    )


def _raw_start_gap_result(
    result: QueryResult,
    fixture: AegisTransferScorerFixture,
    scope: StartGapQueryScope,
) -> DelayEvidencePart | None:
    columns = [column.lower() for column in result.columns]
    client_index = _unique_column(
        columns, lambda value: value in {"c_start", "client_start", "client_timestamp"}
    )
    server_index = _unique_column(
        columns, lambda value: value in {"s_start", "server_start", "server_timestamp"}
    )
    if client_index is None or server_index is None:
        return None
    if scope.client_alias not in scope.timestamp_output_sources.get(
        columns[client_index], frozenset()
    ) or scope.server_alias not in scope.timestamp_output_sources.get(
        columns[server_index], frozenset()
    ):
        return None
    threshold = int(fixture.mechanism_evidence.threshold_ns or 0)
    gaps: dict[str, list[int]] = {"normal": [], "abnormal": []}
    for row in result.rows:
        if not _row_covers(row, client_index, server_index):
            return None
        client_ns = _timestamp_ns(row[client_index])
        server_ns = _timestamp_ns(row[server_index])
        if client_ns is None or server_ns is None:
            return None
        period = _period_for_epoch_ns(client_ns, fixture)
        if period is not None:
            gaps[period].append(server_ns - client_ns)
    return DelayEvidencePart(
        normal_samples=len(gaps["normal"]),
        normal_max_gap_ns=max(gaps["normal"], default=None),
        abnormal_samples=len(gaps["abnormal"]),
        abnormal_min_gap_ns=min(gaps["abnormal"], default=None),
        abnormal_max_gap_ns=max(gaps["abnormal"], default=None),
        abnormal_confirmations=sum(value >= threshold for value in gaps["abnormal"]),
    )


def _time_binned_start_gap_result(
    result: QueryResult,
    query: str,
    fixture: AegisTransferScorerFixture,
    scope: StartGapQueryScope,
) -> DelayEvidencePart | None:
    width = _date_bin_width_seconds(query)
    if width is None:
        return None
    columns = [column.lower() for column in result.columns]
    time_index = _unique_column(columns, lambda value: value in {"t", "time", "bucket"})
    count_index = _unique_column(columns, _is_count_column)
    max_index = _unique_column(columns, lambda value: "max" in value and "gap" in value)
    if None in {time_index, count_index, max_index}:
        return None
    if columns[max_index] not in scope.gap_output_columns:
        return None
    threshold = int(fixture.mechanism_evidence.threshold_ns or 0)
    normal_maxima = []
    normal_samples = 0
    abnormal_maxima = []
    abnormal_samples = 0
    confirmations = 0
    for row in result.rows:
        if not _row_covers(row, time_index, count_index, max_index):
            return None
        bucket_ns = _timestamp_ns(row[time_index])
        count = _strict_int(row[count_index])
        maximum = _gap_to_ns(row[max_index], columns[max_index])
        if bucket_ns is None or count is None or count <= 0 or maximum is None:
            return None
        start = bucket_ns // 1_000_000_000
        if fixture.normal_window[0] <= start and start + width <= fixture.normal_window[1]:
            normal_samples += count
            normal_maxima.append(maximum)
        elif fixture.abnormal_window[0] <= start and start + width <= fixture.abnormal_window[1]:
            abnormal_samples += count
            abnormal_maxima.append(maximum)
            confirmations += maximum >= threshold
    return DelayEvidencePart(
        normal_samples=normal_samples,
        normal_max_gap_ns=max(normal_maxima, default=None),
        abnormal_samples=abnormal_samples,
        abnormal_max_gap_ns=max(abnormal_maxima, default=None),
        abnormal_confirmations=confirmations,
    )


def _start_gap_parts_prove_transition(
    parts: list[DelayEvidencePart],
    fixture: AegisTransferScorerFixture,
) -> bool:
    if not parts:
        return False
    threshold = int(fixture.mechanism_evidence.threshold_ns or 0)
    minimum_confirmations = int(fixture.mechanism_evidence.minimum_anomalous_observations or 0)
    normal_maxima = [part.normal_max_gap_ns for part in parts if part.normal_samples > 0]
    abnormal_confirmations = max(
        (part.abnormal_confirmations for part in parts),
        default=0,
    )
    fully_delayed = max(
        (
            part.abnormal_samples
            for part in parts
            if part.abnormal_samples > 0
            and part.abnormal_min_gap_ns is not None
            and part.abnormal_min_gap_ns >= threshold
        ),
        default=0,
    )
    return (
        bool(normal_maxima)
        and all(value is not None and value < threshold for value in normal_maxima)
        and max(abnormal_confirmations, fully_delayed) >= minimum_confirmations
    )


def _exception_parts_prove_transition(
    parts: list[ExceptionEvidencePart],
    fixture: AegisTransferScorerFixture,
) -> bool:
    if not parts:
        return False
    minimum = int(fixture.mechanism_evidence.minimum_anomalous_observations or 0)
    normal_spans = [
        part.normal_error_spans for part in parts if part.normal_error_spans is not None
    ]
    normal_logs = [
        part.normal_exception_logs for part in parts if part.normal_exception_logs is not None
    ]
    abnormal_spans = max(
        (part.abnormal_error_spans for part in parts if part.abnormal_error_spans is not None),
        default=-1,
    )
    abnormal_logs = max(
        (
            part.abnormal_exception_logs
            for part in parts
            if part.abnormal_exception_logs is not None
        ),
        default=-1,
    )
    return (
        bool(normal_spans)
        and bool(normal_logs)
        and all(value == 0 for value in normal_spans)
        and all(value == 0 for value in normal_logs)
        and abnormal_spans >= minimum
        and abnormal_logs >= minimum
    )


def _causal_operation_matches(
    observed: str | None,
    truth: TransferScorerGroundTruth,
) -> bool:
    accepted = truth.accepted_causal_operations or (
        (truth.causal_operation,) if truth.causal_operation else ()
    )
    if not accepted:
        return observed is None
    if observed is None:
        return False
    normalized = _normalize_causal_operation(observed)
    return any(normalized == _normalize_causal_operation(value) for value in accepted)


def _normalize_causal_operation(value: str) -> str:
    normalized = " ".join(value.strip().lower().split())
    if re.fullmatch(r"(?:[a-z_$][\w$]*\.)*[a-z_$][\w$]*\(\)", normalized):
        return normalized[:-2]
    return normalized


def _unique_column(columns: list[str], predicate: Callable[[str], bool]) -> int | None:
    matches = [index for index, value in enumerate(columns) if predicate(value)]
    return matches[0] if len(matches) == 1 else None


def _row_covers(row: list[object], *indexes: int) -> bool:
    return not indexes or len(row) > max(indexes)


def _is_count_column(value: str) -> bool:
    return value in {"n", "count", "cnt", "span_count", "sample_count"} or value.endswith("_count")


def _gap_to_ns(value: object, column: str) -> int | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    if column.endswith("_ms") or "gap_ms" in column:
        return round(float(value) * 1_000_000)
    if column.endswith("_us") or "gap_us" in column:
        return round(float(value) * 1_000)
    if column.endswith("_s"):
        return round(float(value) * 1_000_000_000)
    return round(float(value))


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


def _period_for_epoch_ns(value: int, fixture: AegisTransferScorerFixture) -> str | None:
    epoch = value // 1_000_000_000
    if fixture.normal_window[0] <= epoch < fixture.normal_window[1]:
        return "normal"
    if fixture.abnormal_window[0] <= epoch < fixture.abnormal_window[1]:
        return "abnormal"
    return None


def _date_bin_width_seconds(query: str) -> int | None:
    match = re.search(
        r"date_bin\s*\(\s*['\"](\d+)\s+(second|minute)s?['\"]",
        query,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    value = int(match.group(1))
    return value * (60 if match.group(2).lower() == "minute" else 1)


def _strict_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_int(value: object) -> int | None:
    return _strict_int(value) if value is not None else None


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
        mechanism_details_match = True
        if fixture.mechanism_evidence.predicate == "source_declared_http_client_server_start_gap":
            mechanism_details_match = (
                mechanism.get("declared_delay_ns") == fixture.mechanism_evidence.threshold_ns
                and mechanism.get("span_name") == fixture.mechanism_evidence.span_name
            )
        if fixture.mechanism_evidence.predicate == "source_declared_http_client_server_start_gap":
            mechanism_details_match = (
                mechanism_details_match
                and mechanism.get("observable") == fixture.mechanism_evidence.observable
            )
        if fixture.mechanism_evidence.predicate == "source_declared_jvm_exception":
            mechanism_details_match = (
                mechanism.get("observable") == fixture.mechanism_evidence.observable
                and mechanism.get("service_name") == fixture.ground_truth.affected_component
                and mechanism.get("method_name")
                in {
                    operation.rsplit(".", 1)[-1]
                    for operation in (
                        fixture.ground_truth.accepted_causal_operations
                        or (fixture.ground_truth.causal_operation or "",)
                    )
                }
            )
        expected_edge = (
            (
                fixture.ground_truth.affected_component,
                fixture.ground_truth.causal_dependency,
            )
            if fixture.ground_truth.causal_scope is CausalScope.DEPENDENCY_EDGE
            else None
        )
        observed_edge = case.get("declared_edge")
        return (
            audit["mode"] == "aegis-transfer-no-model-audit"
            and isinstance(case, dict)
            and case["agent_facing"]["case_id"] == fixture.agent_case_id
            and tuple(case["normal_window"]) == fixture.normal_window
            and tuple(case["abnormal_window"]) == fixture.abnormal_window
            and (tuple(observed_edge) if isinstance(observed_edge, list) else observed_edge)
            == expected_edge
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
        diagnosis=Diagnosis(
            affected_component=truth.affected_component,
            causal_dependency=truth.causal_dependency,
            causal_scope=truth.causal_scope,
            causal_operation=truth.causal_operation,
            fault_category=truth.fault_category,
            mechanism_code=truth.mechanism_code,
            fault_type=truth.source_fault_type,
            confidence=1,
            evidence=[
                Evidence(
                    query_id="q01",
                    claim="paired spans prove the source mechanism",
                    claim_types=[
                        EvidenceClaimType.CAUSAL_SCOPE,
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


def _timestamp_literal_epochs(statement: exp.Expression) -> set[int]:
    epochs = set()
    for literal in statement.find_all(exp.Literal):
        value = str(literal.this)
        if value.isdigit() and len(value) == 10:
            epochs.add(int(value))
            continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        epochs.add(int(parsed.timestamp()))
    return epochs


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
