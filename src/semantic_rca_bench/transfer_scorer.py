from __future__ import annotations

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
    CausalScope,
    EvidenceClaimType,
    MechanismCode,
    QueryResult,
    ToolTrace,
)
from semantic_rca_bench.datasets.openrca2_transfer import TransferCaseSpec
from semantic_rca_bench.evaluation import component_matches
from semantic_rca_bench.evidence import is_valid_evidence_trace


class ClaimGrounding(BaseModel):
    required: bool
    grounded: bool
    supporting_query_ids: list[str]


class TransferEvaluation(BaseModel):
    diagnosis_correct: bool
    causal_locus_match: bool
    causal_scope_match: bool
    fault_category_match: bool
    mechanism_code_match: bool
    causal_operation_match: bool | None
    causal_locus_evidence_match: bool
    baseline_evidence_match: bool
    anomaly_evidence_match: bool
    mechanism_evidence_match: bool
    required_evidence_covered: bool
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
    if case.causal_scope is CausalScope.COMPONENT:
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
    mechanism_code_match = diagnosis is not None and diagnosis.mechanism_code is case.mechanism_code
    allowed_operations = set(case.mechanism_evidence.allowed_operations)
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
    baseline_ids = [query_id for query_id, _, verdict in verdicts if verdict.baseline_clear]
    anomaly_ids = [query_id for query_id, _, verdict in verdicts if verdict.anomaly_present]
    mechanism_evidence_match = bool(baseline_ids) and bool(anomaly_ids)
    mechanism_ids = (
        list(dict.fromkeys([*baseline_ids, *anomaly_ids])) if mechanism_evidence_match else []
    )
    locus_ids = list(dict.fromkeys(anomaly_ids))
    causal_locus_evidence_match = bool(locus_ids)
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
        typed_evidence and causal_locus_evidence_match and mechanism_evidence_match
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
    efficiency_eligible = diagnosis_correct and required_evidence_covered and execution_reliability
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
    checks = {
        "runner contract mismatch": runner_contract_match,
        "causal scope mismatch": causal_scope_match,
        "causal locus mismatch": causal_locus_match,
        "fault category mismatch": fault_category_match,
        "mechanism code mismatch": mechanism_code_match,
        "causal locus lacks incident-local evidence": causal_locus_evidence_match,
        "baseline-clear evidence is missing": bool(baseline_ids),
        "anomalous mechanism evidence is missing": bool(anomaly_ids),
        "fault mechanism lacks complete transition evidence": mechanism_evidence_match,
        "required evidence claims are missing": typed_evidence,
        "citation integrity failed": citations_execution_valid,
        "execution reliability failed": execution_reliability,
    }
    return TransferEvaluation(
        diagnosis_correct=diagnosis_correct,
        causal_locus_match=causal_locus_match,
        causal_scope_match=causal_scope_match,
        fault_category_match=fault_category_match,
        mechanism_code_match=mechanism_code_match,
        causal_operation_match=causal_operation_match,
        causal_locus_evidence_match=causal_locus_evidence_match,
        baseline_evidence_match=bool(baseline_ids),
        anomaly_evidence_match=bool(anomaly_ids),
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
        failure_reasons=[reason for reason, passed in checks.items() if not passed],
        correct_completion_tool_calls=len(run.tool_calls) if efficiency_eligible else None,
        tool_calls_through_required_evidence=(
            support_index + 1 if support_index is not None else None
        ),
        rows_returned_through_required_evidence=rows,
    )


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
    if case.mechanism_code is MechanismCode.CALL_PATH_DELAY:
        return _delay_verdict(statement, result, case)
    return _metric_verdict(statement, result, case)


def _metric_verdict(
    statement: exp.Expression,
    result: QueryResult,
    case: TransferCaseSpec,
) -> ClaimVerdict:
    if isinstance(statement, exp.Union):
        return _metric_union_verdict(statement, result, case)
    evidence = case.mechanism_evidence
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
    where = source_scope.args.get("where")
    if where is None or any(where.find_all(exp.Not)):
        return _rejected_verdict("identity_missing")
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
    filtered_columns = {column.name.lower() for column in where.find_all(exp.Column)}
    if any(column not in allowed_columns for column in filtered_columns):
        return _rejected_verdict("identity_not_scope_preserving")
    identity_bound = _identity_filter_binds_target(
        where.this,
        identity_column.lower(),
        identity_value,
        equivalent_predicates,
    )
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
    facts = _raw_metric_result(scoped_result, result_scope, periods, case)
    lineage_valid_periods = tuple(sorted(periods)) if facts is not None else ()
    if coverage is None and facts is not None:
        coverage = _raw_result_time_coverage(statement, "greptime_timestamp", case)
    if coverage is None:
        return _rejected_verdict("time_coverage_invalid")
    if facts is None:
        aggregate = _aggregate_result(
            scoped_result,
            result_scope,
            periods,
            case,
            value_expression=lambda node: (
                isinstance(node, exp.Column) and node.name.lower() == evidence.value_column.lower()
            ),
        )
        if aggregate is not None:
            facts, lineage_valid_periods = aggregate
    if facts is None:
        aggregate = _conditional_aggregate_result(
            scoped_result,
            result_scope,
            case,
            value_expression=lambda node: (
                isinstance(node, exp.Column) and node.name.lower() == evidence.value_column.lower()
            ),
        )
        if aggregate is not None:
            facts, lineage_valid_periods = aggregate
    evidence_scope = EvidenceScope(
        source_table=evidence.source_table,
        identity_bound=True,
        periods=coverage.periods,
        complete_periods=coverage.complete_periods,
        value_independent=not _scope_filters_use_columns(
            source_scope, {evidence.value_column.lower()}
        ),
        result_complete=(
            _result_scope_complete(statement, result)
            and _metric_having_preserves_complete_facts(source_scope, case)
        ),
        lineage_valid_periods=lineage_valid_periods,
    )
    return _claim_verdict(evidence_scope, facts, case)


def _metric_union_verdict(
    statement: exp.Union,
    result: QueryResult,
    case: TransferCaseSpec,
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
        if not _uses_only_source_tables(normalized_branch, {case.mechanism_evidence.source_table}):
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
        verdict = _metric_verdict(normalized_branch, branch_result, case)
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
            value_expression=lambda node: _gap_ns_expression(node, client_alias, server_alias),
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
        value_independent=not _delay_value_filtered(scope, client_alias, server_alias),
        result_complete=_result_scope_complete(statement, result),
        lineage_valid_periods=lineage_valid_periods,
    )
    return _claim_verdict(evidence_scope, facts, case)


def _rejected_verdict(code: str) -> ClaimVerdict:
    return ClaimVerdict(
        baseline_rejection_codes=(code,),
        anomaly_rejection_codes=(code,),
        rejection_codes=(code,),
    )


def _claim_verdict(
    scope: EvidenceScope,
    facts: PeriodEvidence | None,
    case: TransferCaseSpec,
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
    elif facts.abnormal_high_count < case.mechanism_evidence.minimum_anomalous_observations:
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
) -> bool:
    bindings = {identity_column: identity_value, **equivalent}
    return any(_identity_term_binds_target(term, bindings) for term in _conjunction_terms(where))


def _identity_term_binds_target(
    term: exp.Expression,
    bindings: dict[str, str],
) -> bool:
    if isinstance(term, exp.Paren):
        return _identity_term_binds_target(term.this, bindings)
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
            if value is not None and "%" not in expected and "_" not in expected:
                return (
                    value.lower() == expected.lower()
                    if isinstance(term, exp.ILike)
                    else value == expected
                )
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
) -> ClaimVerdict:
    scopes = [verdict.scope for verdict in verdicts if verdict.scope is not None]
    facts = [verdict.facts for verdict in verdicts if verdict.facts is not None]
    if not scopes or not facts:
        codes = tuple(
            dict.fromkeys(code for verdict in verdicts for code in verdict.rejection_codes)
        )
        return _rejected_verdict(codes[0] if codes else "lineage_unproven")
    combined_scope = EvidenceScope(
        source_table=case.mechanism_evidence.source_table,
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
    return _claim_verdict(combined_scope, combined_facts, case)


def _scope_filters_use_columns(scope: exp.Select, columns: set[str]) -> bool:
    return any(
        clause is not None and _expression_uses_columns(clause, columns)
        for clause in (scope.args.get("where"), scope.args.get("having"))
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
    limits = list(statement.find_all(exp.Limit))
    if not limits:
        return True
    if len(limits) != 1:
        return False
    limit = _numeric_literal(limits[0].expression)
    return limit is not None and limit >= 0 and len(result.rows) < limit


def _metric_having_preserves_complete_facts(
    scope: exp.Select,
    case: TransferCaseSpec,
) -> bool:
    if scope.args.get("having") is None:
        return True
    group = scope.args.get("group")
    if group is None:
        return True
    evidence = case.mechanism_evidence
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


def _delay_value_filtered(
    scope: exp.Select,
    client_alias: str,
    server_alias: str,
) -> bool:
    where = scope.args.get("where")
    if where is None:
        return False
    return any(
        _gap_ns_expression(node, client_alias, server_alias)
        for predicate in where.find_all(
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


def _raw_metric_result(
    result: QueryResult,
    scope: exp.Select,
    periods: set[str],
    case: TransferCaseSpec,
) -> PeriodEvidence | None:
    time_column = _projection_alias(scope, "greptime_timestamp")
    value_column = _projection_alias(scope, case.mechanism_evidence.value_column)
    return _raw_values_result(result, periods, case, time_column, value_column)


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
        return _period_evidence_from_rows(values, periods, case)
    gap_aliases = [
        projection.alias_or_name.lower()
        for projection in scope.expressions
        if projection.alias_or_name
        and _gap_ns_expression(
            projection.this if isinstance(projection, exp.Alias) else projection,
            client_alias,
            server_alias,
        )
        and not any(isinstance(node, exp.AggFunc) for node in projection.find_all(exp.AggFunc))
    ]
    client_time = _qualified_projection_alias(scope, client_alias, "timestamp")
    if len(gap_aliases) != 1 or client_time is None:
        return None
    return _raw_values_result(result, periods, case, client_time, gap_aliases[0])


def _raw_values_result(
    result: QueryResult,
    periods: set[str],
    case: TransferCaseSpec,
    time_column: str | None,
    value_column: str | None,
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
        rows.append((row[time_index], row[value_index]))
    return _period_evidence_from_rows(rows, periods, case)


def _period_evidence_from_rows(
    rows: list[tuple[object, object]],
    periods: set[str],
    case: TransferCaseSpec,
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
    threshold = case.mechanism_evidence.threshold
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
    *,
    value_expression: Callable[[exp.Expression], bool],
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
    minimum_alias = _aggregate_alias(
        scope,
        lambda node: isinstance(node, exp.Min) and value_expression(node.this),
    )
    maximum_alias = _aggregate_alias(
        scope,
        lambda node: isinstance(node, exp.Max) and value_expression(node.this),
    )
    high_projection = _high_count_alias(scope, value_expression, case.mechanism_evidence.threshold)
    high_alias = high_projection[0] if high_projection is not None else None
    if count_alias is None or all(
        alias is None for alias in (minimum_alias, maximum_alias, high_alias)
    ):
        return None
    period_alias = None
    if len(periods) == 2:
        period_alias = _period_projection_alias(
            scope,
            periods,
            case,
            time_column=period_time_column,
            table_alias=period_table_alias,
        )
        if period_alias is None:
            return None
    columns = [column.lower() for column in result.columns]
    required = [count_alias]
    required.extend(
        alias for alias in (minimum_alias, maximum_alias, high_alias, period_alias) if alias
    )
    if any(columns.count(alias) != 1 for alias in required):
        return None
    indexes = {alias: columns.index(alias) for alias in required}
    observed: dict[str, tuple[int, int]] = {}
    valid_periods = set(periods)
    threshold = case.mechanism_evidence.threshold
    for row in result.rows:
        if not _row_covers(row, *indexes.values()):
            return None
        period = next(iter(periods))
        if period_alias is not None:
            raw_period = row[indexes[period_alias]]
            if not isinstance(raw_period, str):
                return None
            period = _canonical_period(raw_period)
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
        if high_count is None:
            if minimum is not None and minimum >= threshold:
                high_count = count
            elif maximum is not None and maximum < threshold:
                high_count = 0
            else:
                valid_periods.discard(period)
                high_count = 0
        if not 0 <= high_count <= count:
            return None
        previous_count, previous_high = observed.get(period, (0, 0))
        observed[period] = (previous_count + count, previous_high + high_count)
    if not observed or not set(observed) <= periods:
        return None
    return _period_part(observed), tuple(sorted(valid_periods & set(observed)))


def _conditional_aggregate_result(
    result: QueryResult,
    scope: exp.Select,
    case: TransferCaseSpec,
    *,
    value_expression: Callable[[exp.Expression], bool],
) -> tuple[PeriodEvidence, tuple[str, ...]] | None:
    aliases: dict[tuple[str, str], str] = {}
    for projection in scope.expressions:
        node = projection.this if isinstance(projection, exp.Alias) else projection
        if not isinstance(node, (exp.Min, exp.Max)) or not projection.alias_or_name:
            continue
        branch = node.this
        if not isinstance(branch, exp.Case):
            continue
        ifs = branch.args.get("ifs") or []
        if len(ifs) != 1 or not value_expression(ifs[0].args.get("true")):
            continue
        default = branch.args.get("default")
        if default is not None and not isinstance(default, exp.Null):
            continue
        period = _conditional_period(ifs[0].this, case)
        if period is None:
            continue
        kind = "minimum" if isinstance(node, exp.Min) else "maximum"
        aliases[(period, kind)] = projection.alias_or_name.lower()
    if not aliases:
        return None
    columns = [column.lower() for column in result.columns]
    if any(columns.count(alias) != 1 for alias in aliases.values()):
        return None
    threshold = case.mechanism_evidence.threshold
    observed: dict[str, tuple[int, int]] = {}
    valid_periods = set()
    for period in ("normal", "abnormal"):
        minimum_alias = aliases.get((period, "minimum"))
        maximum_alias = aliases.get((period, "maximum"))
        if maximum_alias is None:
            continue
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
            if period == "normal":
                observation_floor += 1
                high_floor += int(maximum >= threshold)
            elif minimum is not None and minimum >= threshold:
                row_floor = 2 if minimum != maximum else 1
                observation_floor += row_floor
                high_floor += row_floor
            elif maximum >= threshold:
                observation_floor += 1
                high_floor += 1
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


def _canonical_period(value: str) -> str:
    return {"baseline": "normal", "anomalous": "abnormal"}.get(value.lower(), value.lower())


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
    if len(operation_predicates) != 1:
        return False
    operation = operation_predicates[0]
    if isinstance(operation, exp.EQ):
        values = [
            str(literal.this)
            for column, literal in (
                (operation.this, operation.expression),
                (operation.expression, operation.this),
            )
            if isinstance(column, exp.Column)
            and column.table.lower() in {client_alias, server_alias}
            and column.name.lower() == "span_name"
            and isinstance(literal, exp.Literal)
            and literal.is_string
        ]
        return len(values) == 1 and values[0] in allowed_operations
    if isinstance(operation, exp.In):
        column = operation.this
        return (
            isinstance(column, exp.Column)
            and column.table.lower() in {client_alias, server_alias}
            and column.name.lower() == "span_name"
            and bool(operation.expressions)
            and all(
                isinstance(value, exp.Literal)
                and value.is_string
                and str(value.this) in allowed_operations
                for value in operation.expressions
            )
        )
    return False


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


def _high_count_alias(
    scope: exp.Select,
    value_expression: Callable[[exp.Expression], bool],
    threshold: float,
) -> tuple[str, bool, bool] | None:
    matches: list[tuple[str, bool, bool]] = []
    for projection in scope.expressions:
        node = projection.this if isinstance(projection, exp.Alias) else projection
        if not isinstance(node, exp.Sum):
            continue
        case = node.this if isinstance(node.this, exp.Case) else node.find(exp.Case)
        compatibility = (
            _case_counts_threshold(case, value_expression, threshold) if case is not None else None
        )
        if compatibility is None:
            continue
        if projection.alias_or_name:
            matches.append((projection.alias_or_name.lower(), *compatibility))
    return matches[0] if len(matches) == 1 else None


def _case_counts_threshold(
    case: exp.Case,
    value_expression: Callable[[exp.Expression], bool],
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
    if not value_expression(condition.this) or number is None:
        return None
    baseline_valid = number <= threshold if isinstance(condition, exp.GTE) else number < threshold
    anomaly_valid = number >= threshold
    return baseline_valid, anomaly_valid


def _period_projection_alias(
    scope: exp.Select,
    periods: set[str],
    case: TransferCaseSpec,
    *,
    time_column: str,
    table_alias: str | None,
) -> str | None:
    matches = []
    for projection in scope.expressions:
        if not projection.alias_or_name:
            continue
        node = projection.this if isinstance(projection, exp.Alias) else projection
        branch = node if isinstance(node, exp.Case) else node.find(exp.Case)
        if branch is not None and _period_case_matches(
            branch,
            periods,
            case,
            time_column=time_column,
            table_alias=table_alias,
        ):
            matches.append(projection.alias_or_name.lower())
    return matches[0] if len(matches) == 1 else None


def _period_case_matches(
    case_expression: exp.Case,
    periods: set[str],
    case: TransferCaseSpec,
    *,
    time_column: str,
    table_alias: str | None,
) -> bool:
    if periods != {"normal", "abnormal"}:
        return False
    expected = {
        "normal": {("gte", case.normal_window[0]), ("lt", case.normal_window[1])},
        "abnormal": {("gte", case.abnormal_window[0]), ("lt", case.abnormal_window[1])},
    }
    observed: dict[str, set[tuple[str, int]]] = {}
    for branch in case_expression.args.get("ifs") or []:
        value = branch.args.get("true")
        if isinstance(value, exp.Literal) and value.is_string:
            observed[_canonical_period(str(value.this))] = _time_bounds(
                branch.this,
                time_column,
                table_alias=table_alias,
                filters_only=False,
            )
    if all(observed.get(period) == bounds for period, bounds in expected.items()):
        return True
    default = case_expression.args.get("default")
    if not isinstance(default, exp.Literal) or not default.is_string or len(observed) != 1:
        return False
    period, bounds = next(iter(observed.items()))
    default_period = _canonical_period(str(default.this))
    boundary = case.abnormal_window[0]
    return (
        period == "normal" and default_period == "abnormal" and bounds == {("lt", boundary)}
    ) or (period == "abnormal" and default_period == "normal" and bounds == {("gte", boundary)})


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


def _gap_ns_expression(node: exp.Expression, client_alias: str, server_alias: str) -> bool:
    if (
        isinstance(node, exp.Sub)
        and _timestamp_bigint_cast(node.this, server_alias)
        and _timestamp_bigint_cast(node.expression, client_alias)
    ):
        return True
    if not isinstance(node, exp.Mul):
        return False
    for extracted, multiplier in (
        (node.this, node.expression),
        (node.expression, node.this),
    ):
        if _numeric_literal(multiplier) != 1_000_000_000 or not isinstance(extracted, exp.Extract):
            continue
        unit = extracted.this
        difference = extracted.expression
        if (
            str(getattr(unit, "this", unit)).lower() == "epoch"
            and isinstance(difference, exp.Sub)
            and _qualified_column(difference.this, server_alias, "timestamp")
            and _qualified_column(difference.expression, client_alias, "timestamp")
        ):
            return True
    return False


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
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _strict_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _row_covers(row: list[object], *indexes: int) -> bool:
    return not indexes or len(row) > max(indexes)
