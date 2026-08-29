from __future__ import annotations

import math
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

import sqlglot
from pydantic import BaseModel, ConfigDict, Field
from sqlglot import exp

from semantic_rca_bench.agent import _investigation_tools, run_structured_api_agent
from semantic_rca_bench.contracts import (
    AgentRunner,
    AgentUsage,
    CaseInput,
    QueryResult,
    RejectedToolCall,
    ToolTrace,
    Visibility,
)
from semantic_rca_bench.greptimedb.client import GreptimeClient
from semantic_rca_bench.greptimedb.profile import TableProfiler
from semantic_rca_bench.greptimedb.visibility import QueryGateway
from semantic_rca_bench.subscription import run_structured_subscription_agent


class DiscoveryWindow(BaseModel):
    model_config = ConfigDict(frozen=True)

    start: int
    end: int


class DiscoveryFixture(BaseModel):
    model_config = ConfigDict(frozen=True)

    fixture_id: str
    source_case: str
    component: str
    signal: str
    boundary: int
    target_table: str
    component_column: str
    time_column: str
    value_column: str
    baseline: DiscoveryWindow
    incident: DiscoveryWindow
    comparison_field: str
    minimum_ratio: float = Field(gt=0)


class DiscoveryAnswer(BaseModel):
    table: str
    evidence_query_id: str
    component: str
    signal: str
    claim: str


class DiscoveryAgentRun(BaseModel):
    run_id: str
    visibility: Visibility
    model: str
    runner: AgentRunner
    answer: DiscoveryAnswer | None
    error: str | None = None
    tool_calls: list[ToolTrace]
    rejected_tool_calls: list[RejectedToolCall] = Field(default_factory=list)
    tool_calls_requested: int = 0
    tool_budget_exhausted: bool = False
    usage: AgentUsage
    elapsed_seconds: float
    responses: list[dict[str, object]]
    turn_limit: int | None
    turn_limit_enforced: bool


class DiscoveryEvaluation(BaseModel):
    success: bool
    table_match: bool
    component_match: bool
    signal_match: bool
    citation_valid: bool
    query_reads_only_target: bool
    component_scope_valid: bool
    window_scope_valid: bool
    result_shape_valid: bool
    canonical_result_match: bool
    predicate_match: bool
    failure_reasons: list[str]
    tool_calls_through_evidence: int | None
    rows_returned_through_evidence: int | None
    discovery_calls_through_evidence: int | None


class DiscoveryAudit(BaseModel):
    fixture_id: str
    canonical_query: str
    canonical_result: QueryResult
    predicate_match: bool
    catalog_query: str
    catalog_matched_table_count: int
    catalog_target_rank: int | None
    catalog_target_in_top_five: bool
    catalog_top_five: list[str]


DEVELOPMENT_FIXTURES = {
    "openrca-market-node-write-io": DiscoveryFixture(
        fixture_id="openrca-market-node-write-io",
        source_case="Market/cloudbed-1@2022-03-21T03:30",
        component="node-6",
        signal="node disk write I/O",
        boundary=1_647_805_154,
        target_table="system_io_w_s",
        component_column="cmdb_id",
        time_column="greptime_timestamp",
        value_column="greptime_value",
        baseline=DiscoveryWindow(start=1_647_804_600, end=1_647_805_080),
        incident=DiscoveryWindow(start=1_647_805_080, end=1_647_805_260),
        comparison_field="max_value",
        minimum_ratio=4.0,
    ),
    "openrca-telecom-container-cpu": DiscoveryFixture(
        fixture_id="openrca-telecom-container-cpu",
        source_case="Telecom@2020-05-27T05:00",
        component="docker_001",
        signal="container CPU usage",
        boundary=1_590_527_340,
        target_table="container_cpu_used",
        component_column="cmdb_id",
        time_column="greptime_timestamp",
        value_column="greptime_value",
        baseline=DiscoveryWindow(start=1_590_526_800, end=1_590_527_340),
        incident=DiscoveryWindow(start=1_590_527_580, end=1_590_528_000),
        comparison_field="mean_value",
        minimum_ratio=3.0,
    ),
}

DISCOVERY_MAX_TOOL_CALLS = 12


def run_discovery_agent(
    gateway: QueryGateway,
    fixture: DiscoveryFixture,
    visibility: Visibility,
    *,
    runner: AgentRunner,
    model: str,
    max_tool_calls: int = DISCOVERY_MAX_TOOL_CALLS,
) -> DiscoveryAgentRun:
    if visibility not in {Visibility.RAW, Visibility.SEMANTIC_GRAPH}:
        raise ValueError("discovery benchmark supports only raw and semantic_graph")
    case_input = CaseInput(
        case_token=uuid.uuid4().hex,
        time_start=min(fixture.baseline.start, fixture.incident.start),
        time_end=max(fixture.baseline.end, fixture.incident.end),
        alert_time=fixture.boundary,
        database=gateway.client.database,
    )
    tools = _investigation_tools(visibility, [], None)
    system_prompt = discovery_system_prompt()
    user_prompt = discovery_task_prompt(case_input.database, fixture, max_tool_calls)
    schema = discovery_output_schema()
    if runner is AgentRunner.API:
        output_tool = {
            "name": "submit_discovery_result",
            "description": "Submit the discovered table and cited temporal evidence query.",
            "input_schema": schema,
        }
        result = run_structured_api_agent(
            gateway,
            case_input,
            visibility,
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            investigation_tools=tools,
            output_tool=output_tool,
            validate_output=lambda value: DiscoveryAnswer.model_validate(value).model_dump(
                mode="json"
            ),
            max_tool_calls=max_tool_calls,
            max_turns=max_tool_calls + 10,
        )
        turn_limit = max_tool_calls + 10
        turn_limit_enforced = True
    else:
        allowed_tools = ", ".join(sorted(str(tool["name"]) for tool in tools))
        subscription_prompt = (
            user_prompt
            + "\n\nThe only MCP server is named semantic_rca. Its allowed tools for this run are: "
            + allowed_tools
            + ". Do not call any other MCP server or use shell, files, web search, browser, or "
            "other tools. Return the final JSON object required by the output schema."
        )
        result = run_structured_subscription_agent(
            gateway,
            case_input,
            visibility,
            runner=runner,
            model=model,
            system_prompt=system_prompt,
            user_prompt=subscription_prompt,
            investigation_tools=tools,
            output_schema=schema,
            validate_output=lambda value: DiscoveryAnswer.model_validate(value).model_dump(
                mode="json"
            ),
            max_tool_calls=max_tool_calls,
        )
        turn_limit = None
        turn_limit_enforced = False
    answer = DiscoveryAnswer.model_validate(result.output) if result.output is not None else None
    return DiscoveryAgentRun(
        run_id=uuid.uuid4().hex,
        visibility=visibility,
        model=model,
        runner=runner,
        answer=answer,
        error=result.error,
        tool_calls=result.tool_calls,
        rejected_tool_calls=result.rejected_tool_calls,
        tool_calls_requested=result.tool_calls_requested,
        tool_budget_exhausted=result.tool_budget_exhausted,
        usage=result.usage,
        elapsed_seconds=result.elapsed_seconds,
        responses=result.responses,
        turn_limit=turn_limit,
        turn_limit_enforced=turn_limit_enforced,
    )


def failed_discovery_run(
    visibility: Visibility,
    runner: AgentRunner,
    model: str,
    error: str,
    *,
    max_tool_calls: int = DISCOVERY_MAX_TOOL_CALLS,
) -> DiscoveryAgentRun:
    return DiscoveryAgentRun(
        run_id=uuid.uuid4().hex,
        visibility=visibility,
        model=model,
        runner=runner,
        answer=None,
        error=error,
        tool_calls=[],
        tool_calls_requested=0,
        usage=AgentUsage(),
        elapsed_seconds=0,
        responses=[],
        turn_limit=max_tool_calls + 10 if runner is AgentRunner.API else None,
        turn_limit_enforced=runner is AgentRunner.API,
    )


def discovery_system_prompt() -> str:
    return """You are performing a constrained telemetry-discovery task in GreptimeDB. Find the
physical metric table that represents the supplied signal for the supplied component, then execute
one UNION ALL query with one aggregate SELECT for each exact baseline and incident window. Do not
perform root-cause analysis, infer onset, or change the supplied windows. Discover tables and
columns before querying telemetry. Preserve identifier case and double-quote physical identifiers
exactly as returned by discovery tools. Use only the incident database and the tools exposed for
this treatment. The final
evidence query must return exactly the columns phase, sample_count, mean_value, and max_value, with
one non-empty baseline row and one non-empty incident row. Each SELECT must independently filter the
supplied component and its own half-open window. Copy the query_id from that successful, untruncated
SQL result into the final output."""


def discovery_task_prompt(
    database: str,
    fixture: DiscoveryFixture,
    max_tool_calls: int,
) -> str:
    return f"""Find temporal evidence for a supplied telemetry signal.

Database: {database}
Component: {fixture.component}
Signal concept: {fixture.signal}
Official incident boundary: {_iso_time(fixture.boundary)}
Baseline window: [{_iso_time(fixture.baseline.start)}, {_iso_time(fixture.baseline.end)})
Incident window: [{_iso_time(fixture.incident.start)}, {_iso_time(fixture.incident.end)})
Investigation budget: at most {max_tool_calls} database tool calls.

The physical table and columns are hidden. Find them through the tools available in this treatment.
The evidence query must use UNION ALL with one aggregate SELECT per supplied half-open window. Each
SELECT must filter the supplied component and return its phase, sample_count, mean_value, and
max_value aliases. Return the physical table name, the evidence query_id, the supplied component and
signal verbatim, and a concise claim supported by the cited result."""


def fixture_for_source_case(
    source_case: str,
    fixture_path: str | None = None,
) -> DiscoveryFixture:
    if fixture_path is not None:
        fixture = DiscoveryFixture.model_validate_json(Path(fixture_path).read_text())
        if fixture.source_case != source_case:
            raise ValueError(
                "discovery fixture source case does not match the smoke report: "
                f"{fixture.source_case} != {source_case}"
            )
        return fixture
    fixture = next(
        (item for item in DEVELOPMENT_FIXTURES.values() if item.source_case == source_case),
        None,
    )
    if fixture is None:
        raise ValueError(f"no frozen discovery fixture for source case: {source_case}")
    return fixture


def audit_discovery_fixture(
    client: GreptimeClient,
    fixture: DiscoveryFixture,
) -> DiscoveryAudit:
    query = canonical_evidence_query(fixture)
    result = client.query(query, max_rows=10)
    predicate_match = evidence_predicate_matches(result, fixture)
    catalog = TableProfiler(client, Visibility.SEMANTIC_GRAPH).search(
        fixture.signal,
        signal_type="metric",
        limit=50,
    )
    matches = catalog.get("matches")
    if not isinstance(matches, list):
        matches = []
    tables = [
        str(item.get("table"))
        for item in matches
        if isinstance(item, dict) and item.get("table") not in (None, "")
    ]
    target_rank = next(
        (index for index, table in enumerate(tables, start=1) if table == fixture.target_table),
        None,
    )
    return DiscoveryAudit(
        fixture_id=fixture.fixture_id,
        canonical_query=query,
        canonical_result=result,
        predicate_match=predicate_match,
        catalog_query=fixture.signal,
        catalog_matched_table_count=int(catalog.get("matched_table_count", 0)),
        catalog_target_rank=target_rank,
        catalog_target_in_top_five=target_rank is not None and target_rank <= 5,
        catalog_top_five=tables[:5],
    )


def canonical_evidence_query(fixture: DiscoveryFixture) -> str:
    table = _quote(fixture.target_table)
    component = _quote(fixture.component_column)
    timestamp = _quote(fixture.time_column)
    value = _quote(fixture.value_column)
    component_value = _literal(fixture.component)
    baseline_start = _time_literal(fixture.baseline.start)
    baseline_end = _time_literal(fixture.baseline.end)
    incident_start = _time_literal(fixture.incident.start)
    incident_end = _time_literal(fixture.incident.end)
    return (
        f"SELECT 'baseline' AS phase, COUNT(*) AS sample_count, "
        f"AVG({value}) AS mean_value, MAX({value}) AS max_value "
        f"FROM {table} WHERE {component} = {component_value} "
        f"AND {timestamp} >= {baseline_start} AND {timestamp} < {baseline_end} "
        "UNION ALL "
        f"SELECT 'incident' AS phase, COUNT(*) AS sample_count, "
        f"AVG({value}) AS mean_value, MAX({value}) AS max_value "
        f"FROM {table} WHERE {component} = {component_value} "
        f"AND {timestamp} >= {incident_start} AND {timestamp} < {incident_end} "
        "ORDER BY phase"
    )


def evaluate_discovery_run(
    run: DiscoveryAgentRun,
    fixture: DiscoveryFixture,
    canonical_result: QueryResult,
    *,
    database: str | None = None,
) -> DiscoveryEvaluation:
    answer = run.answer
    table_match = answer is not None and _matches_target_table(
        answer.table,
        fixture.target_table,
        database,
    )
    component_match = answer is not None and answer.component == fixture.component
    signal_match = answer is not None and answer.signal == fixture.signal
    evidence_index, trace = _evidence_trace(run, answer.evidence_query_id if answer else None)
    citation_valid = (
        trace is not None
        and trace.tool_name == "execute_sql"
        and trace.error is None
        and isinstance(trace.output, dict)
        and not bool(trace.output.get("truncated"))
    )
    query = str(trace.input.get("query", "")) if trace is not None else ""
    statement = _parse_query(query) if citation_valid else None
    query_reads_only_target = (
        _reads_only_target(statement, fixture.target_table, database)
        if statement is not None
        else False
    )
    component_scope_valid = (
        _has_component_scope(statement, fixture.component_column, fixture.component)
        if statement is not None
        else False
    )
    window_scope_valid = (
        _has_exact_window_bounds(statement, fixture) if statement is not None else False
    )
    cited_result = _query_result_from_trace(trace) if citation_valid else None
    result_shape_valid = _valid_evidence_shape(cited_result)
    canonical_result_match = (
        _same_evidence_rows(cited_result, canonical_result) if result_shape_valid else False
    )
    predicate_match = evidence_predicate_matches(canonical_result, fixture)
    checks = {
        "missing or incorrect target table": table_match,
        "submitted component does not match the supplied component": component_match,
        "submitted signal does not match the supplied signal": signal_match,
        "missing, failed, or truncated evidence citation": citation_valid,
        "evidence query reads a table outside the frozen target": query_reads_only_target,
        "evidence query does not constrain the frozen component": component_scope_valid,
        "evidence query does not use the frozen windows": window_scope_valid,
        "evidence result does not have the required two-row shape": result_shape_valid,
        "evidence result does not match the canonical query": canonical_result_match,
        "canonical telemetry does not satisfy the frozen predicate": predicate_match,
    }
    failure_reasons = [reason for reason, passed in checks.items() if not passed]
    if run.error:
        failure_reasons.insert(0, run.error)
    if run.tool_budget_exhausted:
        failure_reasons.insert(0, "investigation tool budget exhausted")
    success = not failure_reasons
    calls_through_evidence = (
        run.tool_calls[: evidence_index + 1] if evidence_index is not None else None
    )
    rows_through_evidence = (
        sum(trace.database_load.rows_returned for trace in calls_through_evidence)
        if calls_through_evidence is not None
        and all(trace.database_load is not None for trace in calls_through_evidence)
        else None
    )
    return DiscoveryEvaluation(
        success=success,
        table_match=table_match,
        component_match=component_match,
        signal_match=signal_match,
        citation_valid=citation_valid,
        query_reads_only_target=query_reads_only_target,
        component_scope_valid=component_scope_valid,
        window_scope_valid=window_scope_valid,
        result_shape_valid=result_shape_valid,
        canonical_result_match=canonical_result_match,
        predicate_match=predicate_match,
        failure_reasons=failure_reasons,
        tool_calls_through_evidence=evidence_index + 1 if evidence_index is not None else None,
        rows_returned_through_evidence=rows_through_evidence,
        discovery_calls_through_evidence=(
            sum(_is_discovery_trace(trace) for trace in calls_through_evidence)
            if calls_through_evidence is not None
            else None
        ),
    )


def evidence_predicate_matches(result: QueryResult, fixture: DiscoveryFixture) -> bool:
    rows = _evidence_rows(result)
    if rows is None:
        return False
    baseline = rows["baseline"][fixture.comparison_field]
    incident = rows["incident"][fixture.comparison_field]
    if baseline is None or incident is None:
        return False
    return float(incident) >= fixture.minimum_ratio * float(baseline)


def _evidence_trace(
    run: DiscoveryAgentRun,
    query_id: str | None,
) -> tuple[int | None, ToolTrace | None]:
    if not query_id:
        return None, None
    matches = [
        (index, trace) for index, trace in enumerate(run.tool_calls) if trace.query_id == query_id
    ]
    return matches[0] if len(matches) == 1 else (None, None)


def _query_result_from_trace(trace: ToolTrace | None) -> QueryResult | None:
    if trace is None or not isinstance(trace.output, dict):
        return None
    try:
        return QueryResult.model_validate(trace.output)
    except ValueError:
        return None


def _is_discovery_trace(trace: ToolTrace) -> bool:
    if trace.tool_name in {"describe_table", "search_table_semantics"}:
        return True
    if trace.tool_name != "execute_sql":
        return False
    query = str(trace.input.get("query", "")).lower()
    return bool(re.search(r"\b(show\s+tables|describe|desc\s+|information_schema\.)", query))


def _parse_query(query: str) -> exp.Expression | None:
    for dialect in ("postgres", "mysql"):
        try:
            statements = sqlglot.parse(query, read=dialect)
        except sqlglot.errors.ParseError:
            continue
        if len(statements) == 1 and isinstance(statements[0], (exp.Select, exp.Union)):
            return statements[0]
    return None


def _matches_target_table(value: str, target_table: str, database: str | None) -> bool:
    table = None
    for dialect in ("postgres", "mysql"):
        try:
            table = sqlglot.parse_one(value, read=dialect, into=exp.Table)
        except sqlglot.errors.ParseError:
            continue
        break
    if table is None or table.catalog or table.alias:
        return False
    if table.name.lower() != target_table.lower():
        return False
    if not table.db:
        return True
    return database is not None and table.db.lower() == database.lower()


def _reads_only_target(
    statement: exp.Expression,
    target_table: str,
    database: str | None,
) -> bool:
    tables = list(statement.find_all(exp.Table))
    return bool(tables) and all(
        not table.catalog
        and table.name.lower() == target_table.lower()
        and (not table.db or database is not None and table.db.lower() == database.lower())
        for table in tables
    )


def _has_component_scope(
    statement: exp.Expression,
    column_name: str,
    expected: str,
) -> bool:
    selects = _target_selects(statement)
    return bool(selects) and all(
        any(
            _nearest_select(equality) is select
            and _is_column_literal(equality, column_name, expected)
            and _is_conjunctive_within(equality, select)
            for equality in select.find_all(exp.EQ)
        )
        for select in selects
    )


def _is_column_literal(equality: exp.EQ, column_name: str, expected: str) -> bool:
    for column, literal in (
        (equality.this, equality.expression),
        (equality.expression, equality.this),
    ):
        if (
            isinstance(column, exp.Column)
            and column.name.lower() == column_name.lower()
            and isinstance(literal, exp.Literal)
            and literal.is_string
            and str(literal.this) == expected
        ):
            return True
    return False


def _has_exact_window_bounds(statement: exp.Expression, fixture: DiscoveryFixture) -> bool:
    selects = _target_selects(statement)
    if len(selects) != 2:
        return False
    operators = (
        (exp.GTE, ">="),
        (exp.GT, ">"),
        (exp.LTE, "<="),
        (exp.LT, "<"),
    )
    actual: list[frozenset[tuple[str, int]]] = []
    for select in selects:
        bounds: set[tuple[str, int]] = set()
        for expression_type, operator in operators:
            for comparison in select.find_all(expression_type):
                if _nearest_select(comparison) is not select:
                    continue
                bound = _time_comparison(comparison, fixture.time_column, operator)
                if bound is not None and _is_conjunctive_within(comparison, select):
                    bounds.add(bound)
        time_columns = [
            column
            for column in select.find_all(exp.Column)
            if _nearest_select(column) is select
            and column.name.lower() == fixture.time_column.lower()
        ]
        if len(time_columns) != 2:
            return False
        actual.append(frozenset(bounds))
    expected = {
        frozenset({(">=", fixture.baseline.start), ("<", fixture.baseline.end)}),
        frozenset({(">=", fixture.incident.start), ("<", fixture.incident.end)}),
    }
    return len(actual) == 2 and set(actual) == expected


def _target_selects(statement: exp.Expression) -> list[exp.Select]:
    selects: list[exp.Select] = []
    for table in statement.find_all(exp.Table):
        select = _nearest_select(table)
        if select is not None and select not in selects:
            selects.append(select)
    return selects


def _nearest_select(expression: exp.Expression) -> exp.Select | None:
    node: exp.Expression | None = expression
    while node is not None:
        if isinstance(node, exp.Select):
            return node
        node = node.parent
    return None


def _is_conjunctive_within(expression: exp.Expression, scope: exp.Select) -> bool:
    node = expression.parent
    while node is not None and node is not scope:
        if isinstance(node, (exp.Or, exp.Not)):
            return False
        node = node.parent
    return node is scope


def _time_comparison(
    comparison: exp.Expression,
    column_name: str,
    operator: str,
) -> tuple[str, int] | None:
    left = comparison.this
    right = comparison.expression
    if not isinstance(left, exp.Column) or left.name.lower() != column_name.lower():
        return None
    if isinstance(right, exp.Cast):
        data_type = getattr(right.to.this, "value", "")
        if data_type not in {"DATETIME", "TIMESTAMP", "TIMESTAMPTZ"}:
            return None
        right = right.this
    if not isinstance(right, exp.Literal) or not right.is_string:
        return None
    try:
        timestamp = datetime.fromisoformat(str(right.this).replace("Z", "+00:00"))
    except ValueError:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return operator, int(timestamp.timestamp())


def _valid_evidence_shape(result: QueryResult | None) -> bool:
    return _evidence_rows(result) is not None if result is not None else False


def _evidence_rows(
    result: QueryResult,
) -> dict[str, dict[str, float | int | None]] | None:
    expected_columns = ["phase", "sample_count", "mean_value", "max_value"]
    if [column.lower() for column in result.columns] != expected_columns or len(result.rows) != 2:
        return None
    output: dict[str, dict[str, float | int | None]] = {}
    for row in result.rows:
        if len(row) != 4 or str(row[0]) not in {"baseline", "incident"}:
            return None
        phase = str(row[0])
        if phase in output or not isinstance(row[1], int) or row[1] <= 0:
            return None
        if row[2] is not None and not isinstance(row[2], (int, float)):
            return None
        if row[3] is not None and not isinstance(row[3], (int, float)):
            return None
        output[phase] = {
            "sample_count": row[1],
            "mean_value": row[2],
            "max_value": row[3],
        }
    return output if set(output) == {"baseline", "incident"} else None


def _same_evidence_rows(left: QueryResult, right: QueryResult) -> bool:
    left_rows = _evidence_rows(left)
    right_rows = _evidence_rows(right)
    if left_rows is None or right_rows is None:
        return False
    for phase in ("baseline", "incident"):
        for field in ("sample_count", "mean_value", "max_value"):
            left_value = left_rows[phase][field]
            right_value = right_rows[phase][field]
            if left_value is None or right_value is None or field == "sample_count":
                if left_value != right_value:
                    return False
            elif not math.isclose(
                float(left_value),
                float(right_value),
                rel_tol=1e-9,
                abs_tol=1e-9,
            ):
                return False
    return True


def discovery_output_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "table": {"type": "string"},
            "evidence_query_id": {"type": "string"},
            "component": {"type": "string"},
            "signal": {"type": "string"},
            "claim": {"type": "string"},
        },
        "required": ["table", "evidence_query_id", "component", "signal", "claim"],
        "additionalProperties": False,
    }


def _quote(identifier: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", identifier):
        raise ValueError(f"invalid discovery identifier: {identifier}")
    return f'"{identifier}"'


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _time_literal(epoch: int) -> str:
    value = datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%d %H:%M:%S")
    return _literal(value)


def _iso_time(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat()
