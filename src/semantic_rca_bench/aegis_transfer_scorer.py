from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import sqlglot
from pydantic import BaseModel, ConfigDict
from sqlglot import exp

from semantic_rca_bench.contracts import (
    AgentRun,
    AgentRunner,
    AgentUsage,
    DatabaseLoad,
    Diagnosis,
    Evidence,
    FaultCategory,
    QueryResult,
    RejectedToolCall,
    ToolTrace,
    Visibility,
)
from semantic_rca_bench.datasets.aegis import TRANSFER_AGENT_CASE_ID
from semantic_rca_bench.datasets.aegis_transfer import (
    normalize_delay_evidence,
    normalize_mechanism_evidence,
)
from semantic_rca_bench.evaluation import component_matches, is_valid_evidence_trace
from semantic_rca_bench.protocol import benchmark_protocol

SCORER_REVISION = "aegis-transfer-method-replacement-v3"
DELAY_SCORER_REVISION = "aegis-transfer-request-delay-v2"
DEFAULT_SCORER_FIXTURE = Path("fixtures/reference/aegis-transfer-scorer.json")
DELAY_SCORER_FIXTURE = Path("fixtures/reference/aegis-transfer-v25-scorer.json")


class TransferScorerGroundTruth(BaseModel):
    model_config = ConfigDict(frozen=True)

    affected_component: str
    causal_dependency: str
    fault_category: FaultCategory
    source_fault_type: str
    accepted_fault_type_normalizations: tuple[str, ...]


class TransferMechanismEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    predicate: str
    expected_result: dict[str, dict[str, int]]
    threshold_ns: int | None = None
    span_name: str | None = None


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
    fault_mechanism_match: bool
    citations_execution_valid: bool
    mechanism_evidence_match: bool
    failure_reasons: list[str]
    cited_evidence_count: int
    valid_evidence_count: int
    supporting_evidence_query_ids: list[str]
    tool_calls_through_evidence: int | None
    rows_returned_through_evidence: int | None
    valid_completion: bool | None = None
    correct_completion_tool_calls: int | None = None
    tool_calls_through_mechanism_evidence: int | None = None
    rows_returned_through_mechanism_evidence: int | None = None


def canonical_api_runner_contract() -> dict[str, object]:
    return {
        "runner": AgentRunner.API.value,
        "model": "deepseek-v4-flash",
        "benchmark_protocol_version": benchmark_protocol()["version"],
        "visibility_levels": [level.value for level in Visibility],
        "max_tool_calls": 48,
        "max_turns": 58,
        "max_tokens": 4096,
        "repetitions": 3,
        "treatment_order_seed": 0,
        "parallel_runs": 1,
        "sampling": "provider-default; no seed sent",
        "prompt_cache": "deepseek-automatic-prefix-v1",
    }


def load_transfer_scorer_fixture(path: Path = DEFAULT_SCORER_FIXTURE) -> AegisTransferScorerFixture:
    fixture = AegisTransferScorerFixture.model_validate_json(path.read_text())
    supported = {
        SCORER_REVISION: (TRANSFER_AGENT_CASE_ID, "development"),
        DELAY_SCORER_REVISION: ("aegis-transfer-002", "measurement"),
    }
    expected_identity = supported.get(fixture.scorer_revision)
    if fixture.version != 1 or expected_identity is None:
        raise ValueError("unsupported Aegis transfer scorer fixture revision")
    if fixture.agent_case_id != expected_identity[0]:
        raise ValueError("Aegis transfer scorer fixture uses the wrong opaque case ID")
    if fixture.case_role != expected_identity[1]:
        raise ValueError("Aegis transfer scorer fixture uses the wrong case role")
    if fixture.canonical_api_runner.model_dump(mode="json") != canonical_api_runner_contract():
        raise ValueError("Aegis transfer canonical API runner contract drifted")
    if not fixture.ground_truth.accepted_fault_type_normalizations:
        raise ValueError("Aegis transfer scorer has no accepted fault mechanism labels")
    if any(
        value != _normalize_fault_type(value)
        for value in fixture.ground_truth.accepted_fault_type_normalizations
    ):
        raise ValueError("Aegis transfer accepted fault mechanism labels are not normalized")
    if (
        _normalize_fault_type(fixture.ground_truth.source_fault_type)
        not in fixture.ground_truth.accepted_fault_type_normalizations
    ):
        raise ValueError("Aegis transfer source fault type is not accepted by its scorer")
    if fixture.normal_window[1] != fixture.abnormal_window[0]:
        raise ValueError("Aegis transfer scorer windows must be contiguous")
    predicate = fixture.mechanism_evidence.predicate
    if predicate == "source_declared_http_method_replacement":
        if (
            fixture.mechanism_evidence.threshold_ns is not None
            or fixture.mechanism_evidence.span_name is not None
        ):
            raise ValueError("method replacement scorer must not define delay fields")
    elif predicate == "source_declared_http_delay_threshold":
        expected = fixture.mechanism_evidence.expected_result
        threshold = fixture.mechanism_evidence.threshold_ns
        if (
            threshold is None
            or not fixture.mechanism_evidence.span_name
            or set(expected) != {"normal", "abnormal"}
            or expected["normal"].get("max_duration_ns", threshold) >= threshold
            or expected["abnormal"].get("max_duration_ns", -1) < threshold
        ):
            raise ValueError("delay scorer does not prove the frozen threshold transition")
    else:
        raise ValueError("unsupported Aegis transfer mechanism evidence predicate")
    return fixture


def evaluate_aegis_transfer_run(
    run: AgentRun,
    fixture: AegisTransferScorerFixture,
    *,
    expected_model: str | None = None,
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
    dependency_match = (
        diagnosis is not None
        and diagnosis.causal_dependency is not None
        and component_matches(diagnosis.causal_dependency, truth.causal_dependency)
    )
    declared_edge_match = affected_match and dependency_match
    category_match = diagnosis is not None and diagnosis.fault_category is truth.fault_category
    mechanism_match = (
        diagnosis is not None
        and _normalize_fault_type(diagnosis.fault_type) in truth.accepted_fault_type_normalizations
    )

    evidence = diagnosis.evidence if diagnosis is not None else []
    traces_by_query_id: dict[str, list[ToolTrace]] = {}
    for trace in run.tool_calls:
        if trace.query_id is not None:
            traces_by_query_id.setdefault(trace.query_id, []).append(trace)
    evidence_ids_unique = len({item.query_id for item in evidence}) == len(evidence)
    valid_evidence_count = sum(
        bool(item.claim.strip())
        and is_valid_evidence_trace(traces_by_query_id.get(item.query_id, []))
        for item in evidence
    )
    citations_execution_valid = (
        bool(evidence) and evidence_ids_unique and valid_evidence_count == len(evidence)
    )

    support_indexes = []
    support_query_ids = []
    mechanism_parts = []
    for item in evidence:
        matches = traces_by_query_id.get(item.query_id, [])
        if len(matches) != 1:
            continue
        trace = matches[0]
        normalized = _mechanism_evidence_from_trace(trace, fixture)
        if normalized is not None:
            support_query_ids.append(item.query_id)
            support_indexes.append(run.tool_calls.index(trace))
            mechanism_parts.append(normalized)
    merged_mechanism = _merge_mechanism_evidence(mechanism_parts, fixture)
    mechanism_evidence_match = merged_mechanism == fixture.mechanism_evidence.expected_result
    support_index = max(support_indexes) if mechanism_evidence_match else None

    checks = {
        "run does not use the frozen canonical API runner contract": runner_contract_match,
        "run exceeds the frozen tool-call contract": tool_budget_contract_match,
        "affected component does not match the declared edge source": affected_match,
        "causal dependency does not match the declared edge destination": dependency_match,
        "submitted directed edge does not match the frozen declared edge": declared_edge_match,
        "fault category does not match the frozen source mechanism": category_match,
        "fault mechanism is not accepted by the frozen scorer": mechanism_match,
        "evidence citations are missing, duplicated, failed, truncated, or invalid": (
            citations_execution_valid
        ),
        "no cited SQL result proves the complete frozen mechanism predicate": (
            mechanism_evidence_match
        ),
        "runner reported an error": run.error is None,
        "investigation tool budget was exhausted": not run.tool_budget_exhausted,
        "run contains an invalid rejected tool call": not any(
            item.reason_code == "invalid" for item in run.rejected_tool_calls
        ),
    }
    failure_reasons = [reason for reason, passed in checks.items() if not passed]
    calls_through_evidence = (
        run.tool_calls[: support_index + 1] if support_index is not None else None
    )
    rows_through_evidence = (
        sum(item.database_load.rows_returned for item in calls_through_evidence)
        if calls_through_evidence is not None
        and all(item.database_load is not None for item in calls_through_evidence)
        else None
    )
    success = not failure_reasons
    calls_to_mechanism = support_index + 1 if support_index is not None else None
    legacy_evidence_metric = (
        fixture.mechanism_evidence.predicate == "source_declared_http_method_replacement"
    )
    return AegisTransferEvaluation(
        success=success,
        runner_contract_match=runner_contract_match,
        tool_budget_contract_match=tool_budget_contract_match,
        affected_component_match=affected_match,
        causal_dependency_match=dependency_match,
        declared_edge_match=declared_edge_match,
        fault_category_match=category_match,
        fault_mechanism_match=mechanism_match,
        citations_execution_valid=citations_execution_valid,
        mechanism_evidence_match=mechanism_evidence_match,
        failure_reasons=failure_reasons,
        cited_evidence_count=len(evidence),
        valid_evidence_count=valid_evidence_count,
        supporting_evidence_query_ids=(support_query_ids if mechanism_evidence_match else []),
        tool_calls_through_evidence=(calls_to_mechanism if legacy_evidence_metric else None),
        rows_returned_through_evidence=(rows_through_evidence if legacy_evidence_metric else None),
        valid_completion=success,
        correct_completion_tool_calls=len(run.tool_calls) if success else None,
        tool_calls_through_mechanism_evidence=calls_to_mechanism,
        rows_returned_through_mechanism_evidence=rows_through_evidence,
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
        "reversed_edge": (
            _replace_diagnosis(
                canonical_run,
                affected_component=fixture.ground_truth.causal_dependency,
                causal_dependency=fixture.ground_truth.affected_component,
            ),
            False,
        ),
        "missing_dependency": (
            _replace_diagnosis(canonical_run, causal_dependency=None),
            False,
        ),
        "wrong_mechanism": (_replace_diagnosis(canonical_run, fault_type="memory leak"), False),
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
        "wrong_parent_relation": (
            _replace_trace_query(
                canonical_run,
                _canonical_trace(canonical_run)
                .input["query"]
                .replace(
                    "s.parent_span_id = c.span_id",
                    "s.parent_span_id <> c.span_id",
                ),
            ),
            False,
        ),
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
    gates = {
        "source_transfer_audit_match": source_gate_match,
        "canonical_runner_contract_match": (
            fixture.canonical_api_runner.model_dump(mode="json") == canonical_api_runner_contract()
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


def _mechanism_evidence_from_trace(
    trace: ToolTrace,
    fixture: AegisTransferScorerFixture,
) -> dict[str, dict[str, int]] | None:
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
    query = str(trace.input.get("query") or trace.input.get("sql") or "")
    if fixture.mechanism_evidence.predicate == "source_declared_http_delay_threshold":
        normalized = normalize_delay_evidence(result)
    else:
        normalized = normalize_mechanism_evidence(result)
    populated_groups = {key for key, values in (normalized or {}).items() if values}
    if (
        result.query_id != trace.query_id
        or result.truncated
        or normalized is None
        or not populated_groups
        or not _valid_mechanism_query_scope(query, fixture, populated_groups)
    ):
        return None
    return normalized


def _valid_mechanism_query_scope(
    query: str,
    fixture: AegisTransferScorerFixture,
    populated_groups: set[str],
) -> bool:
    statement = None
    for dialect in ("postgres", "mysql"):
        try:
            statements = sqlglot.parse(query, read=dialect)
        except sqlglot.errors.ParseError:
            continue
        if len(statements) == 1:
            statement = statements[0]
            break
    if statement is None:
        return False
    if sum(table.name.lower() == "traces" for table in statement.find_all(exp.Table)) < 2:
        return False
    lowered = query.lower()
    required_text = {
        fixture.ground_truth.affected_component.lower(),
        fixture.ground_truth.causal_dependency.lower(),
        "span_kind_client",
        "span_kind_server",
    }
    predicate = fixture.mechanism_evidence.predicate
    if predicate == "source_declared_http_delay_threshold":
        required_text.update(
            (
                "duration_nano",
                "span_name",
                str(fixture.mechanism_evidence.span_name).lower(),
            )
        )
        window_epochs = [
            *(fixture.normal_window if "normal" in populated_groups else ()),
            *(fixture.abnormal_window if "abnormal" in populated_groups else ()),
        ]
    else:
        required_text.add("span_attributes.http.request.method")
        window_epochs = []
        if "normal_server_methods" in populated_groups:
            window_epochs.extend(fixture.normal_window)
        if populated_groups & {"abnormal_client_methods", "abnormal_server_methods"}:
            window_epochs.extend(fixture.abnormal_window)
    observed_epochs = _timestamp_literal_epochs(statement)
    if not all(epoch in observed_epochs for epoch in window_epochs):
        return False
    if not all(value in lowered for value in required_text):
        return False
    equalities = list(statement.find_all(exp.EQ))
    return _has_column_equality(equalities, "trace_id", "trace_id") and _has_column_equality(
        equalities,
        "parent_span_id",
        "span_id",
    )


def _merge_mechanism_evidence(
    parts: list[dict[str, dict[str, int]]],
    fixture: AegisTransferScorerFixture,
) -> dict[str, dict[str, int]] | None:
    merged = {key: {} for key in fixture.mechanism_evidence.expected_result}
    for part in parts:
        for group, methods in part.items():
            if group not in merged or set(merged[group]) & set(methods):
                return None
            merged[group].update(methods)
    return {key: dict(sorted(methods.items())) for key, methods in merged.items()}


def _has_column_equality(
    equalities: list[exp.EQ],
    left_name: str,
    right_name: str,
) -> bool:
    for equality in equalities:
        left = equality.this
        right = equality.expression
        if not isinstance(left, exp.Column) or not isinstance(right, exp.Column):
            continue
        names = {left.name.lower(), right.name.lower()}
        if names == {left_name, right_name} and left.table.lower() != right.table.lower():
            return True
    return False


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
        if fixture.mechanism_evidence.predicate == "source_declared_http_delay_threshold":
            mechanism_details_match = (
                mechanism.get("declared_delay_ns") == fixture.mechanism_evidence.threshold_ns
                and mechanism.get("span_name") == fixture.mechanism_evidence.span_name
            )
        return (
            audit["mode"] == "aegis-transfer-no-model-audit"
            and isinstance(case, dict)
            and case["agent_facing"]["case_id"] == fixture.agent_case_id
            and tuple(case["normal_window"]) == fixture.normal_window
            and tuple(case["abnormal_window"]) == fixture.abnormal_window
            and tuple(case["declared_edge"])
            == (
                fixture.ground_truth.affected_component,
                fixture.ground_truth.causal_dependency,
            )
            and case["fault_type"] == fixture.ground_truth.source_fault_type
            and isinstance(mechanism, dict)
            and mechanism["predicate"] == fixture.mechanism_evidence.predicate
            and mechanism["normalized_result"] == fixture.mechanism_evidence.expected_result
            and mechanism["expected_result"] == fixture.mechanism_evidence.expected_result
            and mechanism["pass"] is True
            and mechanism_details_match
            and isinstance(gates, dict)
            and gates["all_passed"] is True
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
            fault_category=truth.fault_category,
            fault_type=truth.source_fault_type,
            confidence=1,
            evidence=[Evidence(query_id="q01", claim="paired spans prove the source mechanism")],
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


def _normalize_fault_type(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


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
