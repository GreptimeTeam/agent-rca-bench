from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from datetime import UTC, datetime

import sqlglot
from pydantic import BaseModel
from sqlglot import exp
from sqlglot.optimizer.scope import build_scope

from semantic_rca_bench.contracts import (
    AgentRun,
    AgentRunner,
    ApiTransport,
    EvidenceClaimType,
    MechanismCode,
    QueryResult,
    ToolTrace,
)
from semantic_rca_bench.datasets.openrca2_transfer import (
    AlternativeMetricSignal,
    DirectLogMechanismEvidence,
    SourceMechanismEvidence,
    TransferCaseSpec,
)
from semantic_rca_bench.evaluation import component_matches
from semantic_rca_bench.evidence import is_valid_evidence_trace


class ClaimGrounding(BaseModel):
    required: bool
    grounded: bool | None
    supporting_query_ids: list[str]


class TransferEvaluation(BaseModel):
    diagnosis_correct: bool
    causal_locus_match: bool
    causal_scope_match: bool
    fault_category_match: bool
    mechanism_code_match: bool
    causal_operation_match: bool | None
    # None when the case has no deterministic mechanism oracle: the secondary
    # evidence audit is not estimable, which is distinct from failing it.
    causal_locus_evidence_match: bool | None
    baseline_evidence_match: bool | None
    anomaly_evidence_match: bool | None
    mechanism_evidence_match: bool | None
    required_evidence_covered: bool | None
    citations_execution_valid: bool
    execution_reliability: bool
    efficiency_eligible: bool
    auditable_completion: bool
    success: bool
    cited_evidence_count: int
    valid_evidence_count: int
    supporting_evidence_query_ids: list[str]
    causal_locus_evidence_query_ids: list[str]
    baseline_evidence_query_ids: list[str]
    anomaly_evidence_query_ids: list[str]
    mechanism_evidence_query_ids: list[str]
    claim_grounding: dict[str, ClaimGrounding]
    failure_reasons: list[str]
    evidence_audit_failure_reasons: list[str]
    semantic_adjudication_required: bool
    semantic_adjudication_reason_codes: list[str]
    correct_completion_tool_calls: int | None = None
    tool_calls_through_required_evidence: int | None = None
    rows_returned_through_required_evidence: int | None = None


class PeriodEvidence(BaseModel):
    model_config = {"frozen": True}

    normal_count: int = 0
    normal_high_count: int = 0
    abnormal_count: int = 0
    abnormal_high_count: int = 0


class EvidenceScope(BaseModel):
    model_config = {"frozen": True}

    source_table: str | None = None
    identity_bound: bool = False
    periods: tuple[str, ...] = ()
    complete_periods: tuple[str, ...] = ()
    value_independent: bool = False
    result_complete: bool = False
    lineage_valid_periods: tuple[str, ...] = ()


class ClaimVerdict(BaseModel):
    model_config = {"frozen": True}

    scope: EvidenceScope | None = None
    facts: PeriodEvidence | None = None
    baseline_clear: bool = False
    anomaly_present: bool = False
    direct_mechanism: bool = False
    baseline_rejection_codes: tuple[str, ...] = ()
    anomaly_rejection_codes: tuple[str, ...] = ()
    rejection_codes: tuple[str, ...] = ()


class _TimeCoverage(BaseModel):
    model_config = {"frozen": True}

    periods: tuple[str, ...]
    complete_periods: tuple[str, ...]


def evaluate_transfer_run(
    run: AgentRun,
    case: TransferCaseSpec,
    *,
    expected_model: str,
    expected_transport: ApiTransport,
    expected_reasoning_effort: str | None,
    expected_max_output_tokens: int,
    max_tool_calls: int,
) -> TransferEvaluation:
    diagnosis = run.diagnosis
    causal_scope_match = diagnosis is not None and diagnosis.causal_scope is case.causal_scope
    if case.causal_scope.uses_causal_component:
        causal_locus_match = (
            diagnosis is not None
            and diagnosis.causal_component is not None
            and diagnosis.edge_source is None
            and diagnosis.edge_destination is None
            and case.causal_component is not None
            and component_matches(diagnosis.causal_component, case.causal_component)
        )
    else:
        causal_locus_match = (
            diagnosis is not None
            and diagnosis.causal_component is None
            and diagnosis.edge_source is not None
            and diagnosis.edge_destination is not None
            and case.edge_source is not None
            and case.edge_destination is not None
            and component_matches(diagnosis.edge_source, case.edge_source)
            and component_matches(diagnosis.edge_destination, case.edge_destination)
        )
    fault_category_match = diagnosis is not None and diagnosis.fault_category is case.fault_category
    accepted_mechanism_codes = _accepted_mechanism_codes(case)
    mechanism_code_match = (
        diagnosis is not None and diagnosis.mechanism_code in accepted_mechanism_codes
    )
    allowed_operations = (
        set(case.mechanism_evidence.allowed_operations) if case.mechanism_evidence else set()
    )
    causal_operation_match = (
        None
        if not allowed_operations
        else (
            diagnosis is not None
            and diagnosis.causal_operation is not None
            and diagnosis.causal_operation.strip() in allowed_operations
        )
    )
    diagnosis_correct = all(
        (causal_scope_match, causal_locus_match, fault_category_match, mechanism_code_match)
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

    verdicts: list[tuple[str, int, ClaimVerdict]] = []
    for item in evidence:
        matches = traces_by_query_id.get(item.query_id, [])
        if len(matches) != 1 or not is_valid_evidence_trace(matches):
            continue
        trace = matches[0]
        verdicts.append(
            (item.query_id, trace_indexes[id(trace)], _mechanism_verdict_from_trace(trace, case))
        )
    auditable = case.mechanism_evidence is not None
    baseline_ids = [query_id for query_id, _, verdict in verdicts if verdict.baseline_clear]
    anomaly_ids = [query_id for query_id, _, verdict in verdicts if verdict.anomaly_present]
    direct_ids = [query_id for query_id, _, verdict in verdicts if verdict.direct_mechanism]
    mechanism_evidence_match = (
        bool(direct_ids) or (bool(baseline_ids) and bool(anomaly_ids)) if auditable else None
    )
    mechanism_ids = (
        list(dict.fromkeys([*direct_ids, *baseline_ids, *anomaly_ids]))
        if mechanism_evidence_match
        else []
    )
    locus_ids = list(dict.fromkeys(anomaly_ids))
    causal_locus_evidence_match = bool(locus_ids) if auditable else None
    claim_grounding = {
        EvidenceClaimType.CAUSAL_LOCUS.value: ClaimGrounding(
            required=True,
            grounded=causal_locus_evidence_match,
            supporting_query_ids=locus_ids,
        ),
        EvidenceClaimType.FAULT_MECHANISM.value: ClaimGrounding(
            required=True,
            grounded=mechanism_evidence_match,
            supporting_query_ids=mechanism_ids,
        ),
    }
    required_evidence_covered = (
        (typed_evidence and causal_locus_evidence_match and mechanism_evidence_match)
        if auditable
        else None
    )
    supporting_ids = list(dict.fromkeys([*locus_ids, *mechanism_ids]))
    runner_contract_match = (
        run.runner is AgentRunner.API
        and run.model == expected_model
        and run.api_transport is expected_transport
        and run.reasoning_effort == expected_reasoning_effort
        and run.max_output_tokens == expected_max_output_tokens
    )
    execution_reliability = (
        runner_contract_match
        and len(run.tool_calls) <= max_tool_calls
        and run.error is None
        and not run.tool_budget_exhausted
        and not any(item.reason_code == "invalid" for item in run.rejected_tool_calls)
    )
    adjudication_reason_codes = list(
        dict.fromkeys(
            code
            for _, _, verdict in verdicts
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
        and not required_evidence_covered
    )
    has_execution_valid_citation = valid_evidence_count > 0
    efficiency_eligible = (
        diagnosis_correct and has_execution_valid_citation and execution_reliability
    )
    auditable_completion = efficiency_eligible and citations_execution_valid
    support_indexes = [
        trace_indexes[id(trace)]
        for query_id in supporting_ids
        for trace in traces_by_query_id.get(query_id, [])
        if id(trace) in trace_indexes
    ]
    support_index = max(support_indexes) if required_evidence_covered and support_indexes else None
    calls = run.tool_calls[: support_index + 1] if support_index is not None else None
    rows = (
        sum(trace.database_load.rows_returned for trace in calls)
        if calls is not None and all(trace.database_load is not None for trace in calls)
        else None
    )
    headline_checks = {
        "runner contract mismatch": runner_contract_match,
        "causal scope mismatch": causal_scope_match,
        "causal locus mismatch": causal_locus_match,
        "fault category mismatch": fault_category_match,
        "mechanism code mismatch": mechanism_code_match,
        "no execution-valid citation": has_execution_valid_citation,
        "execution reliability failed": execution_reliability,
    }
    evidence_checks = (
        {
            "causal locus lacks incident-local evidence": causal_locus_evidence_match,
            "baseline-clear evidence is missing": bool(baseline_ids) or bool(direct_ids),
            "anomalous mechanism evidence is missing": bool(anomaly_ids) or bool(direct_ids),
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
    return TransferEvaluation(
        diagnosis_correct=diagnosis_correct,
        causal_locus_match=causal_locus_match,
        causal_scope_match=causal_scope_match,
        fault_category_match=fault_category_match,
        mechanism_code_match=mechanism_code_match,
        causal_operation_match=causal_operation_match,
        causal_locus_evidence_match=causal_locus_evidence_match,
        baseline_evidence_match=bool(baseline_ids) if auditable else None,
        anomaly_evidence_match=bool(anomaly_ids) if auditable else None,
        mechanism_evidence_match=mechanism_evidence_match,
        required_evidence_covered=required_evidence_covered,
        citations_execution_valid=citations_execution_valid,
        execution_reliability=execution_reliability,
        efficiency_eligible=efficiency_eligible,
        auditable_completion=auditable_completion,
        success=auditable_completion,
        cited_evidence_count=len(evidence),
        valid_evidence_count=valid_evidence_count,
        supporting_evidence_query_ids=(supporting_ids if required_evidence_covered else []),
        causal_locus_evidence_query_ids=(locus_ids if causal_locus_evidence_match else []),
        baseline_evidence_query_ids=baseline_ids,
        anomaly_evidence_query_ids=anomaly_ids,
        mechanism_evidence_query_ids=mechanism_ids,
        claim_grounding=claim_grounding,
        failure_reasons=[reason for reason, passed in headline_checks.items() if not passed],
        evidence_audit_failure_reasons=[
            reason for reason, passed in evidence_checks.items() if not passed
        ],
        semantic_adjudication_required=semantic_adjudication_required,
        semantic_adjudication_reason_codes=(
            adjudication_reason_codes if semantic_adjudication_required else []
        ),
        correct_completion_tool_calls=len(run.tool_calls) if efficiency_eligible else None,
        tool_calls_through_required_evidence=(
            support_index + 1 if support_index is not None else None
        ),
        rows_returned_through_required_evidence=rows,
    )


def _accepted_mechanism_codes(case: TransferCaseSpec) -> set[MechanismCode]:
    return {
        case.mechanism_code,
        *(item.mechanism_code for item in case.direct_log_mechanism_evidence),
    }


def _mechanism_verdict_from_trace(
    trace: ToolTrace,
    case: TransferCaseSpec,
) -> ClaimVerdict:
    if (
        trace.tool_name != "execute_sql"
        or trace.error is not None
        or not isinstance(trace.output, dict)
    ):
        return _rejected_verdict("execution_invalid")
    try:
        result = QueryResult.model_validate(trace.output)
    except ValueError:
        return _rejected_verdict("execution_invalid")
    if result.query_id != trace.query_id or result.truncated:
        return _rejected_verdict("result_incomplete")
    statement = _parse_single_statement(
        str(trace.input.get("query") or trace.input.get("sql") or "")
    )
    if statement is None:
        return _rejected_verdict("query_unparseable")
    if case.mechanism_evidence is None:
        return _rejected_verdict("no_deterministic_oracle")
    if case.mechanism_code is MechanismCode.CALL_PATH_DELAY:
        return _delay_verdict(statement, result, case)
    metric_verdict = _metric_verdict(statement, result, case)
    if metric_verdict.baseline_clear or metric_verdict.anomaly_present:
        return metric_verdict
    for evidence in case.direct_log_mechanism_evidence:
        direct_verdict = _direct_log_verdict(statement, result, case, evidence)
        if direct_verdict.direct_mechanism:
            return direct_verdict
    return metric_verdict


def _direct_log_verdict(
    statement: exp.Expression,
    result: QueryResult,
    case: TransferCaseSpec,
    evidence: DirectLogMechanismEvidence,
) -> ClaimVerdict:
    if not _uses_only_source_tables(statement, {"logs"}):
        return _rejected_verdict("wrong_source")
    scopes = _table_select_scopes(statement, "logs")
    if len(scopes) != 1:
        return _rejected_verdict("lineage_unproven")
    scope = scopes[0]
    timestamp_alias = _projection_alias(scope, "greptime_timestamp")
    message_alias = _projection_alias(scope, "line")
    columns = [column.lower() for column in result.columns]
    if (
        timestamp_alias is None
        or message_alias is None
        or columns.count(timestamp_alias) != 1
        or columns.count(message_alias) != 1
    ):
        return _rejected_verdict("lineage_unproven")
    timestamp_index = columns.index(timestamp_alias)
    message_index = columns.index(message_alias)
    matches = 0
    for row in result.rows:
        if not _row_covers(row, timestamp_index, message_index):
            return _rejected_verdict("lineage_unproven")
        timestamp = _timestamp_ns(row[timestamp_index])
        message = row[message_index]
        if (
            timestamp is not None
            and _timestamp_period(timestamp, case) == "abnormal"
            and isinstance(message, str)
            and _configuration_error_event_matches(message, evidence)
        ):
            matches += 1
    if matches < 1:
        return _rejected_verdict("insufficient_anomalous_observations")
    return ClaimVerdict(
        scope=EvidenceScope(
            source_table="logs",
            identity_bound=True,
            periods=("abnormal",),
            complete_periods=(),
            value_independent=True,
            result_complete=True,
            lineage_valid_periods=("abnormal",),
        ),
        facts=PeriodEvidence(abnormal_count=matches, abnormal_high_count=matches),
        baseline_clear=False,
        anomaly_present=True,
        direct_mechanism=True,
    )


def _configuration_error_event_matches(
    message: str,
    evidence: DirectLogMechanismEvidence,
) -> bool:
    try:
        event = json.loads(message).get("object")
    except (AttributeError, json.JSONDecodeError):
        return False
    if not isinstance(event, dict):
        return False
    regarding = event.get("regarding")
    return (
        isinstance(regarding, dict)
        and event.get("reason") == evidence.event_reason
        and evidence.message_fragment in str(event.get("note") or "")
        and regarding.get("fieldPath") == f"spec.containers{{{evidence.container_identity}}}"
    )


def _metric_verdict(
    statement: exp.Expression,
    result: QueryResult,
    case: TransferCaseSpec,
) -> ClaimVerdict:
    primary = case.mechanism_evidence
    signals: tuple[SourceMechanismEvidence | AlternativeMetricSignal, ...] = (
        primary,
        *primary.alternative_metric_signals,
    )
    table_names = {table.name.lower() for table in statement.find_all(exp.Table)}
    matching = [signal for signal in signals if signal.source_table.lower() in table_names]
    if len(matching) != 1:
        return _rejected_verdict("wrong_source")
    return _metric_signal_verdict(statement, result, case, matching[0])


def _metric_signal_verdict(
    statement: exp.Expression,
    result: QueryResult,
    case: TransferCaseSpec,
    evidence: SourceMechanismEvidence | AlternativeMetricSignal,
) -> ClaimVerdict:
    if isinstance(statement, exp.Union):
        return _metric_union_verdict(statement, result, case, evidence)
    identity_column = evidence.identity_column
    identity_value = evidence.identity_value
    if not identity_column or not identity_value:
        return _rejected_verdict("identity_missing")
    if not _uses_only_source_tables(statement, {evidence.source_table}):
        return _rejected_verdict("wrong_source")
    scopes = _table_select_scopes(statement, evidence.source_table)
    if len(scopes) != 1:
        return _rejected_verdict("wrong_source")
    source_scope = scopes[0]
    result_scope = statement if isinstance(statement, exp.Select) else source_scope
    if any(
        join.find_ancestor(exp.Select) is source_scope for join in source_scope.find_all(exp.Join)
    ):
        return _rejected_verdict("row_multiplication_possible")
    # QUALIFY keeps a subset of each window partition, so it drops source rows
    # whichever columns it names. The column allowlist cannot bound that: ranking
    # on the timestamp alone still discards every row after the first.
    if source_scope.args.get("qualify") is not None:
        return _rejected_verdict("result_incomplete")
    where = source_scope.args.get("where")
    if where is not None and any(where.find_all(exp.Not)):
        return _rejected_verdict("identity_not_scope_preserving")
    scope_predicates = {
        predicate.column.lower(): predicate.value
        for predicate in evidence.scope_preserving_predicates
    }
    equivalent_predicates = {
        predicate.column.lower(): predicate.value
        for predicate in evidence.identity_equivalent_predicates
    }
    filter_values = {
        identity_column.lower(): identity_value,
        **scope_predicates,
        **equivalent_predicates,
    }
    allowed_columns = {
        "greptime_timestamp",
        identity_column.lower(),
        evidence.value_column.lower(),
        *scope_predicates,
        *equivalent_predicates,
    }
    # HAVING is excluded on purpose: it is evaluated after aggregation and names
    # projection aliases rather than source columns, so the column allowlist does
    # not apply. `_metric_having_preserves_complete_facts` resolves those aliases
    # and checks it separately.
    filtered_columns = (
        {column.name.lower() for column in where.find_all(exp.Column)}
        if where is not None
        else set()
    )
    if any(column not in allowed_columns for column in filtered_columns):
        return _rejected_verdict("identity_not_scope_preserving")
    identity_bound = where is not None and _identity_filter_binds_target(
        where.this,
        identity_column.lower(),
        identity_value,
        equivalent_predicates,
        evidence.identity_domains,
    )
    if where is not None:
        if not _identity_filters_preserve_target(where.this, filter_values):
            return _rejected_verdict("identity_not_scope_preserving")
        if not _time_filters_supported(where, "greptime_timestamp"):
            return _rejected_verdict("time_coverage_invalid")
    coverage = _time_coverage(statement, "greptime_timestamp", case)
    periods = set(coverage.periods) if coverage is not None else {"normal", "abnormal"}
    branch_result = _source_branch_result(result, statement, result_scope)
    if branch_result is None:
        return _rejected_verdict("lineage_unproven")
    scoped_result, result_identity_bound = _metric_identity_result(
        branch_result,
        result_scope,
        {
            identity_column.lower(): identity_value,
            **equivalent_predicates,
        },
    )
    if not identity_bound and not result_identity_bound:
        return _rejected_verdict("identity_missing")

    def value_scale(node: exp.Expression) -> float | None:
        return _source_value_scale(node, evidence.value_column)

    facts = _raw_metric_result(
        scoped_result,
        result_scope,
        periods,
        case,
        evidence,
        value_scale=value_scale,
    )
    lineage_valid_periods = tuple(sorted(periods)) if facts is not None else ()
    if facts is None:
        aggregate = _aggregate_result(
            scoped_result,
            result_scope,
            periods,
            case,
            evidence,
            value_scale=value_scale,
        )
        if aggregate is not None:
            facts, lineage_valid_periods = aggregate
    if facts is None:
        aggregate = _conditional_aggregate_result(
            scoped_result,
            result_scope,
            case,
            evidence,
            value_scale=value_scale,
        )
        if aggregate is not None:
            facts, lineage_valid_periods = aggregate
    if coverage is None and facts is not None:
        coverage = _raw_result_time_coverage(statement, "greptime_timestamp", case)
    if coverage is None:
        return _rejected_verdict("time_coverage_invalid")
    evidence_scope = EvidenceScope(
        source_table=evidence.source_table,
        identity_bound=True,
        periods=coverage.periods,
        complete_periods=coverage.complete_periods,
        value_independent=(
            not _scope_filters_use_columns(source_scope, {evidence.value_column.lower()})
            and not _consumer_scope_filters_source(statement, source_scope)
        ),
        result_complete=(
            _result_scope_complete(statement, result)
            and _metric_having_preserves_complete_facts(source_scope, case, evidence)
        ),
        lineage_valid_periods=lineage_valid_periods,
    )
    return _claim_verdict(
        evidence_scope,
        facts,
        minimum_anomalous_observations=case.mechanism_evidence.minimum_anomalous_observations,
    )


def _metric_union_verdict(
    statement: exp.Union,
    result: QueryResult,
    case: TransferCaseSpec,
    evidence: SourceMechanismEvidence | AlternativeMetricSignal,
) -> ClaimVerdict:
    branches = _union_selects(statement)
    discriminator = _union_discriminator(branches, result)
    if discriminator is None:
        return _rejected_verdict("lineage_unproven")
    discriminator_index, branch_values = discriminator
    if any(
        not _row_covers(row, discriminator_index) or row[discriminator_index] not in branch_values
        for row in result.rows
    ):
        return _rejected_verdict("lineage_unproven")

    verdicts = []
    covered_periods: set[str] = set()
    for branch, value in zip(branches, branch_values, strict=True):
        normalized_branch = _union_branch_with_output_names(branch, result.columns)
        if not _uses_only_source_tables(normalized_branch, {evidence.source_table}):
            continue
        branch_result = result.model_copy(
            update={
                "rows": [
                    row
                    for row in result.rows
                    if _row_covers(row, discriminator_index) and row[discriminator_index] == value
                ]
            }
        )
        verdict = _metric_signal_verdict(normalized_branch, branch_result, case, evidence)
        periods = set(verdict.scope.periods) if verdict.scope is not None else set()
        if covered_periods & periods:
            return _rejected_verdict("row_multiplication_possible")
        covered_periods.update(periods)
        verdicts.append(verdict)
    if not verdicts:
        return _rejected_verdict("wrong_source")
    return _combine_union_verdicts(
        verdicts,
        result_complete=_result_scope_complete(statement, result),
        case=case,
        evidence=evidence,
    )


def _delay_verdict(
    statement: exp.Expression,
    result: QueryResult,
    case: TransferCaseSpec,
) -> ClaimVerdict:
    if not _uses_only_source_tables(statement, {"traces"}):
        return _rejected_verdict("wrong_source")
    tables = [table for table in statement.find_all(exp.Table) if table.name.lower() == "traces"]
    if len(tables) != 2:
        return _rejected_verdict("lineage_unproven")
    scope = tables[0].find_ancestor(exp.Select)
    if scope is None or any(table.find_ancestor(exp.Select) is not scope for table in tables):
        return _rejected_verdict("lineage_unproven")
    aliases = {table.alias_or_name.lower() for table in tables}
    if len(aliases) != 2 or case.edge_source is None or case.edge_destination is None:
        return _rejected_verdict("lineage_unproven")
    client_alias = _service_alias(scope, case.edge_source)
    server_alias = _service_alias(scope, case.edge_destination)
    if client_alias not in aliases or server_alias not in aliases or client_alias == server_alias:
        return _rejected_verdict("identity_missing")
    if not (
        _qualified_literal(scope, client_alias, "span_kind", "SPAN_KIND_CLIENT")
        and _qualified_literal(scope, server_alias, "span_kind", "SPAN_KIND_SERVER")
        and _qualified_column_equality(scope, client_alias, "trace_id", server_alias, "trace_id")
        and _qualified_column_equality(
            scope, server_alias, "parent_span_id", client_alias, "span_id"
        )
        and _qualified_allowed_operation(
            scope,
            {client_alias, server_alias},
            set(case.mechanism_evidence.allowed_operations),
        )
    ):
        return _rejected_verdict("lineage_unproven")
    where = scope.args.get("where")
    if where is None or any(where.find_all(exp.Or, exp.Not)):
        return _rejected_verdict("lineage_unproven")
    allowed = {
        "timestamp",
        "service_name",
        "span_kind",
        "trace_id",
        "span_id",
        "parent_span_id",
        "span_name",
    }
    if any(column.name.lower() not in allowed for column in where.find_all(exp.Column)):
        return _rejected_verdict("identity_not_scope_preserving")
    if not _delay_filters_are_complete(
        scope,
        client_alias,
        server_alias,
        set(case.mechanism_evidence.allowed_operations),
    ):
        return _rejected_verdict("lineage_unproven")
    coverage = _time_coverage(scope, "timestamp", case, table_alias=client_alias)
    if coverage is None:
        return _rejected_verdict("time_coverage_invalid")
    periods = set(coverage.periods)
    facts = _raw_delay_result(result, scope, periods, case, client_alias, server_alias)
    lineage_valid_periods = tuple(sorted(periods)) if facts is not None else ()
    if facts is None:
        aggregate = _aggregate_result(
            result,
            scope,
            periods,
            case,
            case.mechanism_evidence,
            value_scale=lambda node: _delay_value_scale(node, client_alias, server_alias),
            period_time_column="timestamp",
            period_table_alias=client_alias,
        )
        if aggregate is not None:
            facts, lineage_valid_periods = aggregate
    evidence_scope = EvidenceScope(
        source_table="traces",
        identity_bound=True,
        periods=coverage.periods,
        complete_periods=coverage.complete_periods,
        value_independent=(
            not _delay_value_filtered(scope, client_alias, server_alias)
            and not _consumer_scope_filters_source(statement, scope)
        ),
        result_complete=_result_scope_complete(statement, result),
        lineage_valid_periods=lineage_valid_periods,
    )
    return _claim_verdict(
        evidence_scope,
        facts,
        minimum_anomalous_observations=case.mechanism_evidence.minimum_anomalous_observations,
    )


def _rejected_verdict(code: str) -> ClaimVerdict:
    return ClaimVerdict(
        baseline_rejection_codes=(code,),
        anomaly_rejection_codes=(code,),
        rejection_codes=(code,),
    )


def _claim_verdict(
    scope: EvidenceScope,
    facts: PeriodEvidence | None,
    *,
    minimum_anomalous_observations: int,
) -> ClaimVerdict:
    baseline_rejections = []
    if "normal" not in scope.periods:
        baseline_rejections.append("period_not_covered")
    elif "normal" not in scope.complete_periods:
        baseline_rejections.append("time_coverage_incomplete")
    if not scope.value_independent:
        baseline_rejections.append("value_filtered_for_universal_claim")
    if not scope.result_complete:
        baseline_rejections.append("result_incomplete")
    if "normal" not in scope.lineage_valid_periods or facts is None:
        baseline_rejections.append("lineage_unproven")
    elif facts.normal_count < 1:
        baseline_rejections.append("baseline_observations_missing")
    elif facts.normal_high_count:
        baseline_rejections.append("baseline_threshold_violation")

    anomaly_rejections = []
    if "abnormal" not in scope.periods:
        anomaly_rejections.append("period_not_covered")
    if "abnormal" not in scope.lineage_valid_periods or facts is None:
        anomaly_rejections.append("lineage_unproven")
    elif facts.abnormal_high_count < minimum_anomalous_observations:
        anomaly_rejections.append("insufficient_anomalous_observations")

    baseline_clear = not baseline_rejections
    anomaly_present = not anomaly_rejections
    rejection_codes = (
        tuple(dict.fromkeys([*baseline_rejections, *anomaly_rejections]))
        if not baseline_clear and not anomaly_present
        else ()
    )
    return ClaimVerdict(
        scope=scope,
        facts=facts,
        baseline_clear=baseline_clear,
        anomaly_present=anomaly_present,
        baseline_rejection_codes=tuple(baseline_rejections),
        anomaly_rejection_codes=tuple(anomaly_rejections),
        rejection_codes=rejection_codes,
    )


def _identity_filter_binds_target(
    where: exp.Expression,
    identity_column: str,
    identity_value: str,
    equivalent: dict[str, str],
    domains: dict[str, tuple[str, ...]],
) -> bool:
    bindings = {identity_column: identity_value, **equivalent}
    return any(
        _identity_term_binds_target(term, bindings, domains) for term in _conjunction_terms(where)
    )


def _identity_term_binds_target(
    term: exp.Expression,
    bindings: dict[str, str],
    domains: dict[str, tuple[str, ...]],
) -> bool:
    if isinstance(term, exp.Paren):
        return _identity_term_binds_target(term.this, bindings, domains)
    if isinstance(term, exp.EQ):
        return any(
            isinstance(column, exp.Column)
            and column.name.lower() == column_name
            and isinstance(literal, exp.Literal)
            and literal.is_string
            and str(literal.this) == value
            for column_name, value in bindings.items()
            for column, literal in (
                (term.this, term.expression),
                (term.expression, term.this),
            )
        )
    if isinstance(term, exp.In) and isinstance(term.this, exp.Column):
        expected = bindings.get(term.this.name.lower())
        return (
            expected is not None
            and len(term.expressions) == 1
            and isinstance(term.expressions[0], exp.Literal)
            and term.expressions[0].is_string
            and str(term.expressions[0].this) == expected
        )
    if isinstance(term, (exp.Like, exp.ILike)):
        for column, literal in (
            (term.this, term.expression),
            (term.expression, term.this),
        ):
            if not (
                isinstance(column, exp.Column)
                and isinstance(literal, exp.Literal)
                and literal.is_string
            ):
                continue
            expected = str(literal.this)
            value = bindings.get(column.name.lower())
            if value is None:
                continue
            flags = re.IGNORECASE if isinstance(term, exp.ILike) else 0
            pattern = "".join(
                ".*" if character == "%" else "." if character == "_" else re.escape(character)
                for character in expected
            )
            matches = [
                candidate
                for candidate in domains.get(column.name.lower(), ())
                if re.fullmatch(pattern, candidate, flags=flags) is not None
            ]
            if matches == [value]:
                return True
    return False


def _identity_filters_preserve_target(
    where: exp.Expression,
    identity_values: dict[str, str],
) -> bool:
    for term in _conjunction_terms(where):
        columns = {column.name.lower() for column in term.find_all(exp.Column)}
        identity_columns = columns & set(identity_values)
        if not identity_columns:
            continue
        if (
            columns != identity_columns
            or _identity_expression_value(term, identity_values) is not True
        ):
            return False
    return True


def _conjunction_terms(expression: exp.Expression) -> tuple[exp.Expression, ...]:
    if isinstance(expression, exp.And):
        return (*_conjunction_terms(expression.this), *_conjunction_terms(expression.expression))
    return (expression,)


def _identity_expression_value(
    expression: exp.Expression,
    values: dict[str, str],
) -> bool | None:
    if isinstance(expression, exp.Paren):
        return _identity_expression_value(expression.this, values)
    if isinstance(expression, exp.And):
        left = _identity_expression_value(expression.this, values)
        right = _identity_expression_value(expression.expression, values)
        return left and right if left is not None and right is not None else None
    if isinstance(expression, exp.Or):
        left = _identity_expression_value(expression.this, values)
        right = _identity_expression_value(expression.expression, values)
        return left or right if left is not None and right is not None else None
    if isinstance(expression, (exp.EQ, exp.Like, exp.ILike)):
        for column, literal in (
            (expression.this, expression.expression),
            (expression.expression, expression.this),
        ):
            if not (
                isinstance(column, exp.Column)
                and isinstance(literal, exp.Literal)
                and literal.is_string
            ):
                continue
            value = values.get(column.name.lower())
            if value is None:
                return None
            expected = str(literal.this)
            if isinstance(expression, exp.EQ):
                return value == expected
            flags = re.IGNORECASE if isinstance(expression, exp.ILike) else 0
            pattern = "".join(
                ".*" if character == "%" else "." if character == "_" else re.escape(character)
                for character in expected
            )
            return re.fullmatch(pattern, value, flags=flags) is not None
    if isinstance(expression, exp.In) and isinstance(expression.this, exp.Column):
        value = values.get(expression.this.name.lower())
        literals = [
            str(item.this)
            for item in expression.expressions
            if isinstance(item, exp.Literal) and item.is_string
        ]
        if value is not None and len(literals) == len(expression.expressions):
            return value in literals
    return None


def _metric_identity_result(
    result: QueryResult,
    scope: exp.Select,
    identity_values: dict[str, str],
) -> tuple[QueryResult, bool]:
    columns = [column.lower() for column in result.columns]
    projected = []
    for source_column, expected in identity_values.items():
        alias = _projection_alias(scope, source_column)
        if alias is None or columns.count(alias) != 1:
            continue
        projected.append((columns.index(alias), expected))
    if projected:
        rows = [
            row
            for row in result.rows
            if all(
                _row_covers(row, index) and row[index] == expected for index, expected in projected
            )
        ]
        return result.model_copy(update={"rows": rows}), bool(rows)
    return result, False


def _source_branch_result(
    result: QueryResult,
    statement: exp.Expression,
    scope: exp.Select,
) -> QueryResult | None:
    columns = [column.lower() for column in result.columns]
    rows = result.rows
    selects = list(statement.find_all(exp.Select)) if isinstance(statement, exp.Union) else [scope]
    for projection_index, projection in enumerate(scope.expressions):
        if not isinstance(projection, exp.Alias) or not isinstance(projection.this, exp.Literal):
            continue
        alias = projection.alias_or_name.lower()
        if not projection.this.is_string or columns.count(alias) != 1:
            continue
        index = columns.index(alias)
        expected = str(projection.this.this)
        if any(
            projection_index >= len(other.expressions)
            or not isinstance(other.expressions[projection_index], exp.Alias)
            or not isinstance(other.expressions[projection_index].this, exp.Literal)
            or not other.expressions[projection_index].this.is_string
            or str(other.expressions[projection_index].this.this) == expected
            for other in selects
            if other is not scope
        ):
            return None
        rows = [row for row in rows if _row_covers(row, index) and row[index] == expected]
    return result.model_copy(update={"rows": rows})


def _union_selects(statement: exp.Expression) -> tuple[exp.Select, ...]:
    if isinstance(statement, exp.Select):
        return (statement,)
    if not isinstance(statement, exp.Union):
        return ()
    left = _union_selects(statement.this)
    right = _union_selects(statement.expression)
    return (*left, *right) if left and right else ()


def _union_discriminator(
    branches: tuple[exp.Select, ...],
    result: QueryResult,
) -> tuple[int, tuple[str, ...]] | None:
    if len(branches) < 2:
        return None
    columns = [column.lower() for column in result.columns]
    for index, first_projection in enumerate(branches[0].expressions):
        first_node = (
            first_projection.this if isinstance(first_projection, exp.Alias) else first_projection
        )
        if not (
            isinstance(first_node, exp.Literal)
            and first_node.is_string
            and first_projection.alias_or_name
        ):
            continue
        alias = first_projection.alias_or_name.lower()
        if columns.count(alias) != 1:
            continue
        values = []
        for branch in branches:
            if index >= len(branch.expressions):
                break
            projection = branch.expressions[index]
            node = projection.this if isinstance(projection, exp.Alias) else projection
            if not isinstance(node, exp.Literal) or not node.is_string:
                break
            values.append(str(node.this))
        if len(values) == len(branches) and len(set(values)) == len(values):
            return columns.index(alias), tuple(values)
    return None


def _union_branch_with_output_names(
    branch: exp.Select,
    output_columns: list[str],
) -> exp.Select:
    normalized = branch.copy()
    expressions = []
    for index, projection in enumerate(normalized.expressions):
        if isinstance(projection, exp.Alias) or index >= len(output_columns):
            expressions.append(projection)
        else:
            expressions.append(exp.alias_(projection, output_columns[index], quoted=False))
    normalized.set("expressions", expressions)
    return normalized


def _combine_union_verdicts(
    verdicts: list[ClaimVerdict],
    *,
    result_complete: bool,
    case: TransferCaseSpec,
    evidence: SourceMechanismEvidence | AlternativeMetricSignal,
) -> ClaimVerdict:
    scopes = [verdict.scope for verdict in verdicts if verdict.scope is not None]
    facts = [verdict.facts for verdict in verdicts if verdict.facts is not None]
    if not scopes or not facts:
        codes = tuple(
            dict.fromkeys(code for verdict in verdicts for code in verdict.rejection_codes)
        )
        return _rejected_verdict(codes[0] if codes else "lineage_unproven")
    combined_scope = EvidenceScope(
        source_table=evidence.source_table,
        identity_bound=all(scope.identity_bound for scope in scopes),
        periods=tuple(sorted({period for scope in scopes for period in scope.periods})),
        complete_periods=tuple(
            sorted({period for scope in scopes for period in scope.complete_periods})
        ),
        value_independent=all(scope.value_independent for scope in scopes),
        result_complete=result_complete and all(scope.result_complete for scope in scopes),
        lineage_valid_periods=tuple(
            sorted({period for scope in scopes for period in scope.lineage_valid_periods})
        ),
    )
    combined_facts = PeriodEvidence(
        normal_count=sum(item.normal_count for item in facts),
        normal_high_count=sum(item.normal_high_count for item in facts),
        abnormal_count=sum(item.abnormal_count for item in facts),
        abnormal_high_count=sum(item.abnormal_high_count for item in facts),
    )
    return _claim_verdict(
        combined_scope,
        combined_facts,
        minimum_anomalous_observations=case.mechanism_evidence.minimum_anomalous_observations,
    )


def _row_selecting_clauses(scope: exp.Select) -> tuple[exp.Expression, ...]:
    """Every clause of this scope that can drop rows.

    A join condition and QUALIFY select rows exactly like WHERE does, so a
    filter moved into one of them has to be read the same way. A join carries
    its condition as either `on` or `using`, and an inner join drops the rows
    that fail to match under both spellings. `args` is used instead of
    `find_all` because a nested SELECT's joins belong to that scope, not to
    this one.
    """
    clauses = [scope.args.get(key) for key in ("where", "having", "qualify")]
    for join in scope.args.get("joins") or ():
        clauses.append(join.args.get("on"))
        clauses.extend(join.args.get("using") or ())
    return tuple(clause for clause in clauses if clause is not None)


def _scope_filters_use_columns(scope: exp.Select, columns: set[str]) -> bool:
    return any(
        _expression_uses_columns(clause, columns) for clause in _row_selecting_clauses(scope)
    )


def _consumer_scope_filters_source(
    statement: exp.Expression,
    source_scope: exp.Select,
) -> bool:
    root = build_scope(statement)
    if root is None:
        return True
    scopes = list(root.traverse())
    source_scopes = [scope for scope in scopes if scope.expression is source_scope]
    if len(source_scopes) != 1:
        return True
    consumers = {id(source_scopes[0])}
    changed = True
    while changed:
        changed = False
        for scope in scopes:
            if id(scope) in consumers:
                continue
            if any(id(source) in consumers for source in scope.sources.values()):
                consumers.add(id(scope))
                changed = True
    # Any consumer filter is disqualifying, not just one naming the value
    # column: the source scope projects the value under an alias a consumer can
    # filter on, and a consumer restricted by time can hide a baseline gap.
    return any(
        id(scope) in consumers
        and scope.expression is not source_scope
        and isinstance(scope.expression, exp.Select)
        and _row_selecting_clauses(scope.expression)
        for scope in scopes
    )


def _expression_uses_columns(expression: exp.Expression, columns: set[str]) -> bool:
    return any(column.name.lower() in columns for column in expression.find_all(exp.Column))


def _time_filters_supported(where: exp.Expression, column_name: str) -> bool:
    for predicate in where.find_all(
        exp.EQ,
        exp.NEQ,
        exp.GT,
        exp.GTE,
        exp.LT,
        exp.LTE,
        exp.In,
        exp.Like,
        exp.ILike,
        exp.Between,
    ):
        if not any(column.name.lower() == column_name for column in predicate.find_all(exp.Column)):
            continue
        if not isinstance(predicate, (exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between)):
            return False
        if not _time_bounds(predicate, column_name, filters_only=False):
            return False
    return True


def _result_scope_complete(statement: exp.Expression, result: QueryResult) -> bool:
    # OFFSET discards rows outright, so a row count under the row cap proves
    # nothing about the rows that were skipped before the cap applied.
    for offset in statement.find_all(exp.Offset):
        skipped = _numeric_literal(offset.expression)
        if skipped is None or skipped > 0:
            return False
    # FETCH FIRST is a row cap the parser does not report as a Limit.
    caps: list[exp.Expression | None] = [
        limit.expression for limit in statement.find_all(exp.Limit)
    ]
    caps.extend(fetch.args.get("count") for fetch in statement.find_all(exp.Fetch))
    if not caps:
        return True
    if len(caps) != 1:
        return False
    cap = _numeric_literal(caps[0]) if caps[0] is not None else None
    return cap is not None and cap >= 0 and len(result.rows) < cap


def _metric_having_preserves_complete_facts(
    scope: exp.Select,
    case: TransferCaseSpec,
    evidence: SourceMechanismEvidence | AlternativeMetricSignal,
) -> bool:
    if scope.args.get("having") is None:
        return True
    group = scope.args.get("group")
    if group is None:
        return True
    allowed_identity_columns = {
        column.lower()
        for column in (
            evidence.identity_column,
            *(predicate.column for predicate in evidence.identity_equivalent_predicates),
            *(predicate.column for predicate in evidence.scope_preserving_predicates),
        )
        if column
    }
    projections = {
        projection.alias_or_name.lower(): (
            projection.this if isinstance(projection, exp.Alias) else projection
        )
        for projection in scope.expressions
        if projection.alias_or_name
    }
    for expression in group.expressions:
        node = expression
        if isinstance(expression, exp.Column) and not expression.table:
            node = projections.get(expression.name.lower(), expression)
        if isinstance(node, exp.Literal):
            continue
        if isinstance(node, exp.Column) and node.name.lower() in allowed_identity_columns:
            continue
        period = node if isinstance(node, exp.Case) else node.find(exp.Case)
        if period is not None and _period_case_matches(
            period,
            {"normal", "abnormal"},
            case,
            time_column="greptime_timestamp",
            table_alias=None,
        ):
            continue
        return False
    return True


def _delay_clause_filters_value(
    clause: exp.Expression,
    client_alias: str,
    server_alias: str,
) -> bool:
    return any(
        _delay_value_scale(node, client_alias, server_alias) is not None
        for predicate in clause.find_all(
            exp.EQ,
            exp.NEQ,
            exp.GT,
            exp.GTE,
            exp.LT,
            exp.LTE,
            exp.Between,
        )
        for node in predicate.walk()
    )


def _delay_value_filtered(
    scope: exp.Select,
    client_alias: str,
    server_alias: str,
) -> bool:
    return any(
        _delay_clause_filters_value(clause, client_alias, server_alias)
        for clause in _row_selecting_clauses(scope)
    )


def _raw_metric_result(
    result: QueryResult,
    scope: exp.Select,
    periods: set[str],
    case: TransferCaseSpec,
    evidence: SourceMechanismEvidence | AlternativeMetricSignal,
    *,
    value_scale: Callable[[exp.Expression], float | None],
) -> PeriodEvidence | None:
    time_column = _projection_alias(scope, "greptime_timestamp")
    value_projection = _scaled_projection(scope, value_scale)
    if value_projection is None:
        value_column = _projection_alias(scope, evidence.value_column)
        if value_column is not None:
            value_projection = (value_column, 1.0)
    if value_projection is None:
        return None
    value_column, canonical_scale = value_projection
    return _raw_values_result(
        result,
        periods,
        case,
        evidence.threshold,
        time_column,
        value_column,
        canonical_scale,
    )


def _raw_delay_result(
    result: QueryResult,
    scope: exp.Select,
    periods: set[str],
    case: TransferCaseSpec,
    client_alias: str,
    server_alias: str,
) -> PeriodEvidence | None:
    client_time = _qualified_projection_alias(scope, client_alias, "timestamp")
    server_time = _qualified_projection_alias(scope, server_alias, "timestamp")
    columns = [column.lower() for column in result.columns]
    if (
        client_time
        and server_time
        and columns.count(client_time) == 1
        and columns.count(server_time) == 1
    ):
        client_index = columns.index(client_time)
        server_index = columns.index(server_time)
        values: list[tuple[object, object]] = []
        for row in result.rows:
            if not _row_covers(row, client_index, server_index):
                return None
            left = _timestamp_ns(row[client_index])
            right = _timestamp_ns(row[server_index])
            if left is None or right is None:
                return None
            values.append((left, right - left))
        return _period_evidence_from_rows(
            values,
            periods,
            case,
            case.mechanism_evidence.threshold,
        )
    gap_projections = [
        (projection.alias_or_name.lower(), scale)
        for projection in scope.expressions
        if (
            scale := _delay_value_scale(
                projection.this if isinstance(projection, exp.Alias) else projection,
                client_alias,
                server_alias,
            )
        )
        is not None
        if projection.alias_or_name
        and not any(isinstance(node, exp.AggFunc) for node in projection.find_all(exp.AggFunc))
    ]
    client_time = _qualified_projection_alias(scope, client_alias, "timestamp")
    if len(gap_projections) != 1 or client_time is None:
        return None
    gap_alias, canonical_scale = gap_projections[0]
    return _raw_values_result(
        result,
        periods,
        case,
        case.mechanism_evidence.threshold,
        client_time,
        gap_alias,
        canonical_scale,
    )


def _raw_values_result(
    result: QueryResult,
    periods: set[str],
    case: TransferCaseSpec,
    threshold: float,
    time_column: str | None,
    value_column: str | None,
    canonical_scale: float,
) -> PeriodEvidence | None:
    columns = [column.lower() for column in result.columns]
    if (
        time_column is None
        or value_column is None
        or columns.count(time_column) != 1
        or columns.count(value_column) != 1
    ):
        return None
    time_index = columns.index(time_column)
    value_index = columns.index(value_column)
    rows: list[tuple[object, object]] = []
    for row in result.rows:
        if not _row_covers(row, time_index, value_index):
            return None
        value = _strict_number(row[value_index])
        if value is None:
            return None
        rows.append((row[time_index], value * canonical_scale))
    return _period_evidence_from_rows(rows, periods, case, threshold)


def _period_evidence_from_rows(
    rows: list[tuple[object, object]],
    periods: set[str],
    case: TransferCaseSpec,
    threshold: float,
) -> PeriodEvidence | None:
    values: dict[str, list[float]] = {period: [] for period in periods}
    for raw_time, raw_value in rows:
        timestamp = _timestamp_ns(raw_time)
        value = _strict_number(raw_value)
        if timestamp is None or value is None:
            return None
        period = _timestamp_period(timestamp, case)
        if period not in values:
            return None
        values[period].append(value)
    if not any(values.values()):
        return None
    return _period_part(
        {
            period: (len(items), sum(value >= threshold for value in items))
            for period, items in values.items()
        }
    )


def _aggregate_result(
    result: QueryResult,
    scope: exp.Select,
    periods: set[str],
    case: TransferCaseSpec,
    evidence: SourceMechanismEvidence | AlternativeMetricSignal,
    *,
    value_scale: Callable[[exp.Expression], float | None],
    period_time_column: str = "greptime_timestamp",
    period_table_alias: str | None = None,
) -> PeriodEvidence | None:
    count_alias = _aggregate_alias(
        scope,
        lambda node: (
            isinstance(node, exp.Count)
            and (isinstance(node.this, exp.Star) or _numeric_literal(node.this) == 1)
        ),
    )
    minimum_projection = _scaled_aggregate_projection(
        scope,
        exp.Min,
        value_scale,
    )
    maximum_projection = _scaled_aggregate_projection(
        scope,
        exp.Max,
        value_scale,
    )
    average_projection = _scaled_aggregate_projection(
        scope,
        exp.Avg,
        value_scale,
    )
    minimum_alias = minimum_projection[0] if minimum_projection is not None else None
    maximum_alias = maximum_projection[0] if maximum_projection is not None else None
    average_alias = average_projection[0] if average_projection is not None else None
    high_projection = _high_count_alias(scope, value_scale, evidence.threshold)
    high_alias = high_projection[0] if high_projection is not None else None
    if count_alias is None or all(
        alias is None for alias in (minimum_alias, maximum_alias, average_alias, high_alias)
    ):
        return None
    period_alias = None
    period_values: dict[str, str] = {}
    if len(periods) == 2:
        period_projection = _period_projection(
            scope,
            periods,
            case,
            time_column=period_time_column,
            table_alias=period_table_alias,
        )
        if period_projection is None:
            return None
        period_alias, period_values = period_projection
    columns = [column.lower() for column in result.columns]
    required = [count_alias]
    required.extend(
        alias
        for alias in (minimum_alias, maximum_alias, average_alias, high_alias, period_alias)
        if alias
    )
    if any(columns.count(alias) != 1 for alias in required):
        return None
    indexes = {alias: columns.index(alias) for alias in required}
    observed: dict[str, tuple[int, int]] = {}
    valid_periods = set(periods)
    threshold = evidence.threshold
    for row in result.rows:
        if not _row_covers(row, *indexes.values()):
            return None
        period = next(iter(periods))
        if period_alias is not None:
            raw_period = row[indexes[period_alias]]
            if not isinstance(raw_period, str):
                return None
            period = period_values.get(raw_period)
        count = _strict_int(row[indexes[count_alias]])
        if period not in periods or count is None or count <= 0:
            return None
        high_count = None
        if high_projection is not None:
            _, baseline_valid, anomaly_valid = high_projection
            if (period == "normal" and baseline_valid) or (period == "abnormal" and anomaly_valid):
                high_count = _strict_int(row[indexes[high_alias]])
        minimum = _strict_number(row[indexes[minimum_alias]]) if minimum_alias else None
        maximum = _strict_number(row[indexes[maximum_alias]]) if maximum_alias else None
        average = _strict_number(row[indexes[average_alias]]) if average_alias else None
        if minimum is not None and minimum_projection is not None:
            minimum *= minimum_projection[1]
        if maximum is not None and maximum_projection is not None:
            maximum *= maximum_projection[1]
        if average is not None and average_projection is not None:
            average *= average_projection[1]
        inconsistent_summary = (
            minimum is not None and maximum is not None and minimum > maximum
        ) or (
            average is not None
            and (
                minimum is not None
                and average < minimum
                or maximum is not None
                and average > maximum
            )
        )
        if inconsistent_summary:
            return None
        if high_count is None:
            if minimum is not None and minimum >= threshold:
                high_count = count
            elif maximum is not None and maximum < threshold:
                high_count = 0
            elif average is not None and maximum is not None:
                high_count = _minimum_threshold_hits(count, average, maximum, threshold)
            else:
                valid_periods.discard(period)
                high_count = 0
        if high_count is None:
            valid_periods.discard(period)
            high_count = 0
        if not 0 <= high_count <= count:
            return None
        previous_count, previous_high = observed.get(period, (0, 0))
        observed[period] = (previous_count + count, previous_high + high_count)
    if not observed or not set(observed) <= periods:
        return None
    return _period_part(observed), tuple(sorted(valid_periods & set(observed)))


def _minimum_threshold_hits(
    count: int,
    average: float,
    maximum: float,
    threshold: float,
) -> int | None:
    if count <= 0 or maximum < average:
        return None
    if maximum < threshold:
        return 0
    if maximum == threshold:
        return count if average == threshold else 1
    if average <= threshold:
        return 1
    excess = count * (average - threshold)
    lower_bound = math.floor(excess / (maximum - threshold)) + 1
    return min(count, max(1, lower_bound))


def _conditional_aggregate_result(
    result: QueryResult,
    scope: exp.Select,
    case: TransferCaseSpec,
    evidence: SourceMechanismEvidence | AlternativeMetricSignal,
    *,
    value_scale: Callable[[exp.Expression], float | None],
) -> tuple[PeriodEvidence, tuple[str, ...]] | None:
    aliases: dict[tuple[str, str], tuple[str, float]] = {}
    for projection in scope.expressions:
        node = projection.this if isinstance(projection, exp.Alias) else projection
        if not projection.alias_or_name:
            continue
        for aggregate_type, kind in ((exp.Min, "minimum"), (exp.Max, "maximum")):
            match = _conditional_aggregate_scale(node, aggregate_type, value_scale, case)
            if match is not None:
                period, canonical_scale = match
                aliases[(period, kind)] = (
                    projection.alias_or_name.lower(),
                    canonical_scale,
                )
    if not aliases:
        return None
    columns = [column.lower() for column in result.columns]
    if any(columns.count(alias) != 1 for alias, _ in aliases.values()):
        return None
    threshold = evidence.threshold
    observed: dict[str, tuple[int, int]] = {}
    valid_periods = set()
    for period in ("normal", "abnormal"):
        minimum_projection = aliases.get((period, "minimum"))
        maximum_projection = aliases.get((period, "maximum"))
        if maximum_projection is None:
            continue
        maximum_alias, maximum_scale = maximum_projection
        minimum_alias, minimum_scale = minimum_projection or (None, 1.0)
        maximum_index = columns.index(maximum_alias)
        minimum_index = columns.index(minimum_alias) if minimum_alias else None
        if any(
            not _row_covers(row, maximum_index)
            or (minimum_index is not None and not _row_covers(row, minimum_index))
            for row in result.rows
        ):
            return None
        observation_floor = 0
        high_floor = 0
        for row in result.rows:
            maximum = _strict_number(row[maximum_index])
            minimum = _strict_number(row[minimum_index]) if minimum_index is not None else None
            if maximum is None:
                continue
            maximum *= maximum_scale
            if minimum is not None:
                minimum *= minimum_scale
            if period == "normal":
                observation_floor += 1
                high_floor += int(maximum >= threshold)
            elif minimum is not None and minimum >= threshold:
                row_floor = 2 if minimum != maximum else 1
                observation_floor += row_floor
                high_floor += row_floor
            elif maximum >= threshold:
                restart_floor = (
                    math.floor(maximum / threshold)
                    if isinstance(evidence, SourceMechanismEvidence)
                    and evidence.predicate == "workload_restart_transition"
                    and threshold > 0
                    else 1
                )
                observation_floor += restart_floor
                high_floor += restart_floor
        if observation_floor:
            observed[period] = (observation_floor, high_floor)
            valid_periods.add(period)
    if not observed:
        return None
    return _period_part(observed), tuple(sorted(valid_periods))


def _conditional_period(expression: exp.Expression, case: TransferCaseSpec) -> str | None:
    bounds = _time_bounds(expression, "greptime_timestamp", filters_only=False)
    if len(bounds) != 1:
        return None
    operator, epoch = next(iter(bounds))
    if operator in {"lt", "lte"} and case.normal_window[1] <= epoch <= case.abnormal_window[1]:
        return "normal"
    if operator in {"gte", "gt"} and case.abnormal_window[0] <= epoch < case.abnormal_window[1]:
        return "abnormal"
    return None


def _period_part(values: dict[str, tuple[int, int]]) -> PeriodEvidence:
    normal = values.get("normal", (0, 0))
    abnormal = values.get("abnormal", (0, 0))
    return PeriodEvidence(
        normal_count=normal[0],
        normal_high_count=normal[1],
        abnormal_count=abnormal[0],
        abnormal_high_count=abnormal[1],
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
    for scope in root.traverse():
        for source in scope.sources.values():
            if isinstance(source, exp.Table) and source.name.lower() not in allowed:
                return False
    return True


def _table_select_scopes(statement: exp.Expression, table_name: str) -> tuple[exp.Select, ...]:
    scopes: list[exp.Select] = []
    for table in statement.find_all(exp.Table):
        if table.name.lower() != table_name.lower():
            continue
        scope = table.find_ancestor(exp.Select)
        if scope is not None and all(scope is not item for item in scopes):
            scopes.append(scope)
    return tuple(scopes)


def _delay_filters_are_complete(
    scope: exp.Select,
    client_alias: str,
    server_alias: str,
    allowed_operations: set[str],
) -> bool:
    where = scope.args.get("where")
    if where is None:
        return False
    identity_predicates = _predicates_using_columns(
        scope,
        {"trace_id", "span_id", "parent_span_id"},
    )
    expected_identity_pairs = {
        frozenset({(client_alias, "trace_id"), (server_alias, "trace_id")}),
        frozenset({(client_alias, "span_id"), (server_alias, "parent_span_id")}),
    }
    observed_identity_pairs = []
    for predicate in identity_predicates:
        if not isinstance(predicate, exp.EQ):
            return False
        left = _column_identity(predicate.this)
        right = _column_identity(predicate.expression)
        if left is None or right is None:
            return False
        observed_identity_pairs.append(frozenset({left, right}))
    if set(observed_identity_pairs) != expected_identity_pairs:
        return False

    operation_predicates = _predicates_using_columns(scope, {"span_name"})
    if not operation_predicates:
        return False
    aliases = {client_alias, server_alias}
    for operation in operation_predicates:
        if isinstance(operation, exp.EQ):
            values = [
                str(literal.this)
                for column, literal in (
                    (operation.this, operation.expression),
                    (operation.expression, operation.this),
                )
                if isinstance(column, exp.Column)
                and column.table.lower() in aliases
                and column.name.lower() == "span_name"
                and isinstance(literal, exp.Literal)
                and literal.is_string
            ]
            if len(values) != 1 or values[0] not in allowed_operations:
                return False
            continue
        if isinstance(operation, exp.In):
            column = operation.this
            if not (
                isinstance(column, exp.Column)
                and column.table.lower() in aliases
                and column.name.lower() == "span_name"
                and bool(operation.expressions)
                and all(
                    isinstance(value, exp.Literal)
                    and value.is_string
                    and str(value.this) in allowed_operations
                    for value in operation.expressions
                )
            ):
                return False
            continue
        return False
    return True


def _predicates_using_columns(
    expression: exp.Expression, column_names: set[str]
) -> list[exp.Expression]:
    predicate_types = (
        exp.EQ,
        exp.NEQ,
        exp.GT,
        exp.GTE,
        exp.LT,
        exp.LTE,
        exp.In,
        exp.Like,
        exp.ILike,
        exp.Between,
    )
    return [
        predicate
        for predicate in expression.find_all(*predicate_types)
        if any(column.name.lower() in column_names for column in predicate.find_all(exp.Column))
        and _positive_filter(predicate)
    ]


def _column_identity(node: exp.Expression) -> tuple[str, str] | None:
    if not isinstance(node, exp.Column) or not node.table:
        return None
    return node.table.lower(), node.name.lower()


def _time_coverage(
    scope: exp.Expression,
    time_column: str,
    case: TransferCaseSpec,
    *,
    table_alias: str | None = None,
) -> _TimeCoverage | None:
    bounds = _time_bounds(scope, time_column, table_alias=table_alias)
    normal = case.normal_window
    abnormal = case.abnormal_window
    expected = {
        frozenset({("gte", normal[0]), ("lt", normal[1])}): {"normal"},
        frozenset({("gte", abnormal[0]), ("lt", abnormal[1])}): {"abnormal"},
        frozenset({("gte", normal[0]), ("lt", abnormal[1])}): {"normal", "abnormal"},
        frozenset({("gte", normal[0]), ("lte", normal[1])}): {"normal"},
        frozenset({("gte", abnormal[0]), ("lte", abnormal[1])}): {"abnormal"},
        frozenset({("gte", normal[0]), ("lte", abnormal[1])}): {"normal", "abnormal"},
    }
    complete = expected.get(frozenset(bounds))
    if complete is not None:
        periods = tuple(sorted(complete))
        return _TimeCoverage(periods=periods, complete_periods=periods)

    lower = [(operator, epoch) for operator, epoch in bounds if operator in {"gte", "gt"}]
    upper = [(operator, epoch) for operator, epoch in bounds if operator in {"lt", "lte"}]
    if len(lower) != 1 or len(upper) != 1:
        return None
    lower_operator, lower_epoch = lower[0]
    upper_operator, upper_epoch = upper[0]
    if lower_epoch >= upper_epoch:
        return None
    for period, window in (("normal", normal), ("abnormal", abnormal)):
        if window[0] <= lower_epoch and upper_epoch <= window[1]:
            is_complete = (
                lower_operator == "gte"
                and lower_epoch == window[0]
                and upper_operator in {"lt", "lte"}
                and upper_epoch == window[1]
            )
            return _TimeCoverage(
                periods=(period,),
                complete_periods=(period,) if is_complete else (),
            )
    return None


def _raw_result_time_coverage(
    expression: exp.Expression,
    column_name: str,
    case: TransferCaseSpec,
) -> _TimeCoverage | None:
    bounds = _time_bounds(expression, column_name)
    periods = ("abnormal", "normal")
    if not bounds:
        where = expression.find(exp.Where)
        if where is not None and _expression_uses_columns(where, {column_name.lower()}):
            return None
        return _TimeCoverage(periods=periods, complete_periods=periods)
    lower = [(operator, epoch) for operator, epoch in bounds if operator in {"gte", "gt"}]
    upper = [(operator, epoch) for operator, epoch in bounds if operator in {"lt", "lte"}]
    if len(lower) != 1 or len(upper) != 1:
        return None
    lower_operator, lower_epoch = lower[0]
    upper_operator, upper_epoch = upper[0]
    starts_before_window = lower_epoch < case.normal_window[0] or (
        lower_epoch == case.normal_window[0] and lower_operator == "gte"
    )
    ends_after_window = upper_epoch > case.abnormal_window[1] or (
        upper_epoch == case.abnormal_window[1] and upper_operator in {"lt", "lte"}
    )
    if not starts_before_window or not ends_after_window:
        return None
    return _TimeCoverage(periods=periods, complete_periods=periods)


def _time_bounds(
    expression: exp.Expression,
    column_name: str,
    *,
    table_alias: str | None = None,
    filters_only: bool = True,
) -> set[tuple[str, int]]:
    bounds: set[tuple[str, int]] = set()
    for kind, direct, reverse in (
        (exp.GTE, "gte", "lte"),
        (exp.GT, "gt", "lt"),
        (exp.LTE, "lte", "gte"),
        (exp.LT, "lt", "gt"),
    ):
        for comparison in expression.find_all(kind):
            if filters_only and not _positive_filter(comparison):
                continue
            for column, value, operator in (
                (comparison.this, comparison.expression, direct),
                (comparison.expression, comparison.this, reverse),
            ):
                if not _qualified_column(column, table_alias, column_name):
                    continue
                epoch = _timestamp_epoch(value)
                if epoch is not None:
                    bounds.add((operator, epoch))
    for between in expression.find_all(exp.Between):
        if (
            filters_only
            and not _positive_filter(between)
            or not _qualified_column(between.this, table_alias, column_name)
        ):
            continue
        lower = _timestamp_epoch(between.args["low"])
        upper = _timestamp_epoch(between.args["high"])
        if lower is not None and upper is not None:
            bounds.update({("gte", lower), ("lte", upper)})
    return bounds


def _positive_filter(node: exp.Expression) -> bool:
    current = node.parent
    while current is not None:
        if isinstance(current, (exp.Or, exp.Not)):
            return False
        if isinstance(current, (exp.Where, exp.Join, exp.Having)):
            return True
        if isinstance(current, exp.Select):
            return False
        current = current.parent
    return False


def _projection_alias(scope: exp.Select, source_column: str) -> str | None:
    matches = []
    for projection in scope.expressions:
        node = projection.this if isinstance(projection, exp.Alias) else projection
        if isinstance(node, exp.Star):
            matches.append(source_column.lower())
        elif isinstance(node, exp.Column) and node.name.lower() == source_column.lower():
            matches.append(projection.alias_or_name.lower())
    return matches[0] if len(matches) == 1 else None


def _scaled_projection(
    scope: exp.Select,
    value_scale: Callable[[exp.Expression], float | None],
) -> tuple[str, float] | None:
    matches = []
    for projection in scope.expressions:
        node = projection.this if isinstance(projection, exp.Alias) else projection
        canonical_scale = value_scale(node)
        if projection.alias_or_name and canonical_scale is not None:
            matches.append((projection.alias_or_name.lower(), canonical_scale))
    return matches[0] if len(matches) == 1 else None


def _qualified_projection_alias(
    scope: exp.Select, table_alias: str, source_column: str
) -> str | None:
    matches = []
    for projection in scope.expressions:
        node = projection.this if isinstance(projection, exp.Alias) else projection
        if _qualified_column(node, table_alias, source_column):
            matches.append(projection.alias_or_name.lower())
    return matches[0] if len(matches) == 1 else None


def _aggregate_alias(scope: exp.Select, predicate: Callable[[exp.Expression], bool]) -> str | None:
    matches = []
    for projection in scope.expressions:
        node = projection.this if isinstance(projection, exp.Alias) else projection
        if predicate(node) and projection.alias_or_name:
            matches.append(projection.alias_or_name.lower())
    return matches[0] if len(matches) == 1 else None


def _scaled_aggregate_projection(
    scope: exp.Select,
    aggregate_type: type[exp.AggFunc],
    value_scale: Callable[[exp.Expression], float | None],
) -> tuple[str, float] | None:
    matches = []
    for projection in scope.expressions:
        node = projection.this if isinstance(projection, exp.Alias) else projection
        canonical_scale = _linear_unit_scale(
            node,
            lambda candidate: (
                value_scale(candidate.this) if isinstance(candidate, aggregate_type) else None
            ),
        )
        if projection.alias_or_name and canonical_scale is not None:
            matches.append((projection.alias_or_name.lower(), canonical_scale))
    return matches[0] if len(matches) == 1 else None


def _conditional_aggregate_scale(
    node: exp.Expression,
    aggregate_type: type[exp.AggFunc],
    value_scale: Callable[[exp.Expression], float | None],
    case: TransferCaseSpec,
) -> tuple[str, float] | None:
    period = None

    def base_scale(candidate: exp.Expression) -> float | None:
        nonlocal period
        if not isinstance(candidate, aggregate_type) or not isinstance(candidate.this, exp.Case):
            return None
        branches = candidate.this.args.get("ifs") or []
        if len(branches) != 1:
            return None
        scale = value_scale(branches[0].args.get("true"))
        default = candidate.this.args.get("default")
        candidate_period = _conditional_period(branches[0].this, case)
        if (
            scale is None
            or candidate_period is None
            or default is not None
            and not isinstance(default, exp.Null)
        ):
            return None
        period = candidate_period
        return scale

    canonical_scale = _linear_unit_scale(node, base_scale)
    if canonical_scale is None or period is None:
        return None
    return period, canonical_scale


def _high_count_alias(
    scope: exp.Select,
    value_scale: Callable[[exp.Expression], float | None],
    threshold: float,
) -> tuple[str, bool, bool] | None:
    matches: list[tuple[str, bool, bool]] = []
    for projection in scope.expressions:
        node = projection.this if isinstance(projection, exp.Alias) else projection
        if not isinstance(node, exp.Sum):
            continue
        case = node.this if isinstance(node.this, exp.Case) else node.find(exp.Case)
        compatibility = (
            _case_counts_threshold(case, value_scale, threshold) if case is not None else None
        )
        if compatibility is None:
            continue
        if projection.alias_or_name:
            matches.append((projection.alias_or_name.lower(), *compatibility))
    return matches[0] if len(matches) == 1 else None


def _case_counts_threshold(
    case: exp.Case,
    value_scale: Callable[[exp.Expression], float | None],
    threshold: float,
) -> tuple[bool, bool] | None:
    branches = case.args.get("ifs") or []
    if len(branches) != 1 or _numeric_literal(case.args.get("default")) != 0:
        return None
    branch = branches[0]
    if _numeric_literal(branch.args.get("true")) != 1:
        return None
    condition = branch.this
    if not isinstance(condition, (exp.GTE, exp.GT)):
        return None
    number = _numeric_literal(condition.expression)
    canonical_scale = value_scale(condition.this)
    if canonical_scale is None or number is None:
        return None
    canonical_threshold = number * canonical_scale
    baseline_valid = (
        canonical_threshold <= threshold
        if isinstance(condition, exp.GTE)
        else canonical_threshold < threshold
    )
    anomaly_valid = canonical_threshold >= threshold
    return baseline_valid, anomaly_valid


def _period_projection(
    scope: exp.Select,
    periods: set[str],
    case: TransferCaseSpec,
    *,
    time_column: str,
    table_alias: str | None,
) -> tuple[str, dict[str, str]] | None:
    matches = []
    for projection in scope.expressions:
        if not projection.alias_or_name:
            continue
        node = projection.this if isinstance(projection, exp.Alias) else projection
        branch = node if isinstance(node, exp.Case) else node.find(exp.Case)
        if branch is None:
            continue
        mapping = _period_case_mapping(
            branch, periods, case, time_column=time_column, table_alias=table_alias
        )
        if mapping is not None:
            matches.append((projection.alias_or_name.lower(), mapping))
    return matches[0] if len(matches) == 1 else None


def _period_case_matches(
    case_expression: exp.Case,
    periods: set[str],
    case: TransferCaseSpec,
    *,
    time_column: str,
    table_alias: str | None,
) -> bool:
    return (
        _period_case_mapping(
            case_expression,
            periods,
            case,
            time_column=time_column,
            table_alias=table_alias,
        )
        is not None
    )


def _period_case_mapping(
    case_expression: exp.Case,
    periods: set[str],
    case: TransferCaseSpec,
    *,
    time_column: str,
    table_alias: str | None,
) -> dict[str, str] | None:
    if periods != {"normal", "abnormal"}:
        return None
    expected = {
        "normal": {("gte", case.normal_window[0]), ("lt", case.normal_window[1])},
        "abnormal": {("gte", case.abnormal_window[0]), ("lt", case.abnormal_window[1])},
    }
    observed: dict[str, set[tuple[str, int]]] = {}
    for branch in case_expression.args.get("ifs") or []:
        value = branch.args.get("true")
        if isinstance(value, exp.Literal) and value.is_string:
            observed[str(value.this)] = _time_bounds(
                branch.this,
                time_column,
                table_alias=table_alias,
                filters_only=False,
            )
    explicit_mapping = {
        label: period
        for label, bounds in observed.items()
        for period, expected_bounds in expected.items()
        if bounds == expected_bounds
    }
    if set(explicit_mapping.values()) == periods and len(explicit_mapping) == len(observed):
        return explicit_mapping
    default = case_expression.args.get("default")
    if not isinstance(default, exp.Literal) or not default.is_string or len(observed) != 1:
        return None
    label, bounds = next(iter(observed.items()))
    default_label = str(default.this)
    if label == default_label:
        return None
    matching_periods = [
        period for period, expected_bounds in expected.items() if bounds == expected_bounds
    ]
    if len(matching_periods) == 1:
        period = matching_periods[0]
        other_period = next(candidate for candidate in periods if candidate != period)
        return {label: period, default_label: other_period}
    if len(bounds) != 1:
        return None
    boundary = next(iter(bounds))[1]
    boundary_is_observed_onset = case.normal_window[1] <= boundary < case.abnormal_window[1]
    if not boundary_is_observed_onset:
        return None
    if bounds == {("lt", boundary)}:
        return {label: "normal", default_label: "abnormal"}
    if bounds == {("gte", boundary)}:
        return {label: "abnormal", default_label: "normal"}
    return None


def _service_alias(scope: exp.Select, service: str) -> str | None:
    matches = []
    for equality in scope.find_all(exp.EQ):
        for column, literal in (
            (equality.this, equality.expression),
            (equality.expression, equality.this),
        ):
            if (
                isinstance(column, exp.Column)
                and column.name.lower() == "service_name"
                and column.table
                and isinstance(literal, exp.Literal)
                and literal.is_string
                and str(literal.this) == service
                and _positive_filter(equality)
            ):
                matches.append(column.table.lower())
    return matches[0] if len(set(matches)) == 1 else None


def _qualified_literal(scope: exp.Select, table_alias: str, column_name: str, value: str) -> bool:
    return any(
        _positive_filter(equality)
        and any(
            _qualified_column(column, table_alias, column_name)
            and isinstance(literal, exp.Literal)
            and literal.is_string
            and str(literal.this) == value
            for column, literal in (
                (equality.this, equality.expression),
                (equality.expression, equality.this),
            )
        )
        for equality in scope.find_all(exp.EQ)
    )


def _qualified_column_equality(
    scope: exp.Select,
    left_alias: str,
    left_column: str,
    right_alias: str,
    right_column: str,
) -> bool:
    return any(
        _positive_filter(equality)
        and (
            _qualified_column(equality.this, left_alias, left_column)
            and _qualified_column(equality.expression, right_alias, right_column)
            or _qualified_column(equality.expression, left_alias, left_column)
            and _qualified_column(equality.this, right_alias, right_column)
        )
        for equality in scope.find_all(exp.EQ)
    )


def _qualified_allowed_operation(scope: exp.Select, aliases: set[str], allowed: set[str]) -> bool:
    if not allowed:
        return False
    for equality in scope.find_all(exp.EQ):
        for column, literal in (
            (equality.this, equality.expression),
            (equality.expression, equality.this),
        ):
            if (
                isinstance(column, exp.Column)
                and column.name.lower() == "span_name"
                and column.table.lower() in aliases
                and isinstance(literal, exp.Literal)
                and literal.is_string
                and str(literal.this) in allowed
                and _positive_filter(equality)
            ):
                return True
    for membership in scope.find_all(exp.In):
        column = membership.this
        values = membership.expressions
        if (
            isinstance(column, exp.Column)
            and column.name.lower() == "span_name"
            and column.table.lower() in aliases
            and values
            and all(
                isinstance(value, exp.Literal) and value.is_string and str(value.this) in allowed
                for value in values
            )
            and _positive_filter(membership)
        ):
            return True
    return False


def _source_value_scale(node: exp.Expression, source_column: str) -> float | None:
    return _linear_unit_scale(
        node,
        lambda candidate: (
            1.0
            if isinstance(candidate, exp.Column) and candidate.name.lower() == source_column.lower()
            else None
        ),
    )


def _delay_value_scale(
    node: exp.Expression,
    client_alias: str,
    server_alias: str,
) -> float | None:
    return _linear_unit_scale(
        node,
        lambda candidate: _base_delay_value_scale(candidate, client_alias, server_alias),
    )


def _linear_unit_scale(
    node: exp.Expression,
    base_scale: Callable[[exp.Expression], float | None],
) -> float | None:
    direct = base_scale(node)
    if direct is not None:
        return direct if math.isfinite(direct) and direct > 0 else None
    if isinstance(node, exp.Paren):
        return _linear_unit_scale(node.this, base_scale)
    if isinstance(node, exp.Div):
        scale = _linear_unit_scale(node.this, base_scale)
        divisor = _numeric_literal(node.expression)
        if scale is None or divisor is None or not math.isfinite(divisor) or divisor <= 0:
            return None
        return scale * divisor
    if isinstance(node, exp.Mul):
        for value, factor_node in (
            (node.this, node.expression),
            (node.expression, node.this),
        ):
            scale = _linear_unit_scale(value, base_scale)
            factor = _numeric_literal(factor_node)
            if scale is not None and factor is not None and math.isfinite(factor) and factor > 0:
                return scale / factor
    return None


def _base_delay_value_scale(
    node: exp.Expression,
    client_alias: str,
    server_alias: str,
) -> float | None:
    direct = _timestamp_difference_scale(node, client_alias, server_alias)
    if direct is not None:
        return direct
    if isinstance(node, exp.Extract):
        unit = node.this
        difference = node.expression
        if isinstance(difference, exp.Paren):
            difference = difference.this
        if (
            str(getattr(unit, "this", unit)).lower() == "epoch"
            and isinstance(difference, exp.Sub)
            and _qualified_column(difference.this, server_alias, "timestamp")
            and _qualified_column(difference.expression, client_alias, "timestamp")
        ):
            return 1_000_000_000.0
    if (
        isinstance(node, exp.Sub)
        and _timestamp_bigint_cast(node.this, server_alias)
        and _timestamp_bigint_cast(node.expression, client_alias)
    ):
        return 1.0
    if not isinstance(node, exp.Mul):
        return None
    for extracted, multiplier in (
        (node.this, node.expression),
        (node.expression, node.this),
    ):
        factor = _numeric_literal(multiplier)
        if factor is None or not isinstance(extracted, exp.Extract):
            continue
        unit = extracted.this
        difference = extracted.expression
        if isinstance(difference, exp.Paren):
            difference = difference.this
        if (
            str(getattr(unit, "this", unit)).lower() == "epoch"
            and isinstance(difference, exp.Sub)
            and _qualified_column(difference.this, server_alias, "timestamp")
            and _qualified_column(difference.expression, client_alias, "timestamp")
        ):
            return 1_000_000_000 / factor
    return None


def _timestamp_difference_scale(
    node: exp.Expression,
    client_alias: str,
    server_alias: str,
) -> float | None:
    if isinstance(node, exp.Paren):
        return _timestamp_difference_scale(node.this, client_alias, server_alias)
    if isinstance(node, exp.Cast) and str(node.to).upper() == "BIGINT":
        return _timestamp_difference_scale(node.this, client_alias, server_alias)
    if not isinstance(node, exp.Sub):
        return None
    if _qualified_column(node.this, server_alias, "timestamp") and _qualified_column(
        node.expression, client_alias, "timestamp"
    ):
        return 1.0
    if _epoch_timestamp(node.this, server_alias) and _epoch_timestamp(
        node.expression, client_alias
    ):
        return 1_000_000_000.0
    return None


def _epoch_timestamp(node: exp.Expression, table_alias: str) -> bool:
    if isinstance(node, exp.Anonymous) and node.name.lower() == "to_unixtime":
        return len(node.expressions) == 1 and _qualified_column(
            node.expressions[0], table_alias, "timestamp"
        )
    return (
        isinstance(node, exp.Extract)
        and str(getattr(node.this, "this", node.this)).lower() == "epoch"
        and _qualified_column(node.expression, table_alias, "timestamp")
    )


def _timestamp_bigint_cast(node: exp.Expression, table_alias: str) -> bool:
    return (
        isinstance(node, exp.Cast)
        and str(node.to).upper() == "BIGINT"
        and _qualified_column(node.this, table_alias, "timestamp")
    )


def _qualified_column(node: exp.Expression, table_alias: str | None, column_name: str) -> bool:
    return (
        isinstance(node, exp.Column)
        and node.name.lower() == column_name.lower()
        and (table_alias is None or node.table.lower() == table_alias.lower())
    )


def _timestamp_period(timestamp_ns: int, case: TransferCaseSpec) -> str | None:
    epoch = timestamp_ns // 1_000_000_000
    if case.normal_window[0] <= epoch < case.normal_window[1]:
        return "normal"
    if case.abnormal_window[0] <= epoch < case.abnormal_window[1]:
        return "abnormal"
    return None


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
        return int(parsed.timestamp() * 1_000_000_000)
    return None


def _timestamp_epoch(node: exp.Expression) -> int | None:
    if isinstance(node, exp.Cast):
        node = node.this
    if not isinstance(node, exp.Literal):
        return None
    value = str(node.this)
    if value.isdigit() and len(value) == 10:
        return int(value)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


def _numeric_literal(node: exp.Expression | None) -> float | None:
    if not isinstance(node, exp.Literal) or node.is_string:
        return None
    try:
        return float(node.this)
    except (TypeError, ValueError):
        return None


def _strict_number(value: object) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _strict_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _row_covers(row: list[object], *indexes: int) -> bool:
    return not indexes or len(row) > max(indexes)
