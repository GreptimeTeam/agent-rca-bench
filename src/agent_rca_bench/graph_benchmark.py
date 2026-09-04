from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

import sqlglot
from pydantic import BaseModel, ConfigDict, Field
from sqlglot import exp
from sqlglot.optimizer.scope import build_scope

from agent_rca_bench.agent import (
    _investigation_tools,
    _semantic_graph_query,
    run_structured_api_agent,
)
from agent_rca_bench.contracts import (
    AgentRunner,
    AgentUsage,
    ApiTransport,
    CaseInput,
    QueryResult,
    RejectedToolCall,
    ToolTrace,
    Visibility,
)
from agent_rca_bench.greptimedb.client import GreptimeClient
from agent_rca_bench.greptimedb.visibility import QueryGateway


class GraphFixture(BaseModel):
    model_config = ConfigDict(frozen=True)

    fixture_id: str
    source_case: str
    window_start: int
    window_end: int
    caller: str
    trace_table: str
    expected_callee: str
    expected_request_count: int = Field(gt=0)
    expected_error_count: int = Field(ge=0)


class GraphAnswer(BaseModel):
    src_type: str
    src_id: str
    dst_type: str
    dst_id: str
    rel_type: str
    provenance: str
    request_count: int = Field(ge=0)
    error_count: int = Field(ge=0)
    evidence_query_id: str
    claim: str


class GraphAgentRun(BaseModel):
    run_id: str
    visibility: Visibility
    model: str
    runner: AgentRunner
    api_transport: ApiTransport | None = None
    reasoning_effort: str | None = None
    max_output_tokens: int | None = None
    answer: GraphAnswer | None
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


class GraphEvaluation(BaseModel):
    success: bool
    answer_match: bool
    citation_valid: bool
    evidence_scope_valid: bool
    evidence_result_match: bool
    unique_winner: bool
    failure_reasons: list[str]
    tool_calls_through_evidence: int | None
    rows_returned_through_evidence: int | None


class GraphAudit(BaseModel):
    fixture_id: str
    trace_query: str
    trace_result: QueryResult
    graph_query: str
    graph_result: QueryResult
    edge_sets_match: bool
    unique_winner: bool
    expected_winner_match: bool


DEVELOPMENT_GRAPH_FIXTURES = {
    "rca100-t002-frontend-callee-errors": GraphFixture(
        fixture_id="rca100-t002-frontend-callee-errors",
        source_case="t002",
        window_start=1_776_913_200,
        window_end=1_776_913_800,
        caller="frontend",
        trace_table="traces",
        expected_callee="cart",
        expected_request_count=9_423,
        expected_error_count=1,
    ),
    "openrca2-hotel-frontend-callee-errors": GraphFixture(
        fixture_id="openrca2-hotel-frontend-callee-errors",
        source_case="hs1-geo-pod-failure-drdmjj",
        window_start=1_777_686_600,
        window_end=1_777_687_200,
        caller="frontend",
        trace_table="traces",
        expected_callee="search",
        expected_request_count=2_304,
        expected_error_count=1_095,
    ),
}

GRAPH_MAX_TOOL_CALLS = 12


def fixture_for_source_case(
    source_case: str,
    fixture_path: str | None = None,
) -> GraphFixture:
    if fixture_path is not None:
        fixture = GraphFixture.model_validate_json(Path(fixture_path).read_text())
        if fixture.source_case != source_case:
            raise ValueError(
                "graph fixture source case does not match the smoke report: "
                f"{fixture.source_case} != {source_case}"
            )
        return fixture
    fixture = next(
        (item for item in DEVELOPMENT_GRAPH_FIXTURES.values() if item.source_case == source_case),
        None,
    )
    if fixture is None:
        raise ValueError(f"no frozen graph fixture for source case: {source_case}")
    return fixture


def validate_source_window(
    fixture: GraphFixture,
    source_start: int,
    source_end: int,
) -> None:
    if fixture.window_start % 60 or fixture.window_end % 60:
        raise ValueError("graph fixture window must align to whole minutes")
    if fixture.window_start != _ceil_minute(source_start):
        raise ValueError("graph fixture start does not match the source window ceiling")
    if fixture.window_end != _ceil_minute(source_end):
        raise ValueError("graph fixture end does not match the source window ceiling")
    if fixture.window_start >= fixture.window_end:
        raise ValueError("graph fixture window must be non-empty")


def run_graph_agent(
    gateway: QueryGateway,
    fixture: GraphFixture,
    visibility: Visibility,
    semantic_coverage: dict[str, object],
    *,
    runner: AgentRunner,
    model: str,
    api_transport: ApiTransport | None = None,
    reasoning_effort: str | None = None,
    max_output_tokens: int = 4096,
    max_tool_calls: int = GRAPH_MAX_TOOL_CALLS,
) -> GraphAgentRun:
    if visibility not in {Visibility.RAW, Visibility.SEMANTIC_GRAPH}:
        raise ValueError("graph benchmark supports only raw and semantic_graph")
    case_input = _case_input(gateway.client.database, fixture)
    tools = _investigation_tools(visibility, [], semantic_coverage)
    system_prompt = graph_system_prompt()
    user_prompt = graph_task_prompt(case_input.database, fixture, max_tool_calls)
    schema = graph_output_schema()

    def validate_output(value: dict[str, object]) -> dict[str, object]:
        return GraphAnswer.model_validate(value).model_dump(mode="json")

    if api_transport is None:
        raise ValueError("API graph runs require an explicit transport")
    output_tool = {
        "name": "submit_graph_result",
        "description": "Submit the highest-error direct callee and cited RED evidence.",
        "input_schema": schema,
    }
    result = run_structured_api_agent(
        gateway,
        case_input,
        visibility,
        model=model,
        api_transport=api_transport,
        reasoning_effort=reasoning_effort,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        investigation_tools=tools,
        output_tool=output_tool,
        validate_output=validate_output,
        max_tool_calls=max_tool_calls,
        max_turns=max_tool_calls + 10,
        max_output_tokens=max_output_tokens,
    )
    turn_limit = max_tool_calls + 10
    turn_limit_enforced = True
    answer = GraphAnswer.model_validate(result.output) if result.output is not None else None
    return GraphAgentRun(
        run_id=uuid.uuid4().hex,
        visibility=visibility,
        model=model,
        runner=runner,
        api_transport=api_transport,
        reasoning_effort=reasoning_effort,
        max_output_tokens=max_output_tokens if runner is AgentRunner.API else None,
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


def failed_graph_run(
    visibility: Visibility,
    runner: AgentRunner,
    model: str,
    error: str,
    *,
    max_tool_calls: int = GRAPH_MAX_TOOL_CALLS,
) -> GraphAgentRun:
    return GraphAgentRun(
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


def graph_system_prompt() -> str:
    return """You are performing a constrained service-call evidence task in GreptimeDB. Do not
perform root-cause analysis or infer the affected component, causal dependency, fault mechanism, or
onset. For the supplied caller and exact half-open window, identify its direct service callee with
the greatest error_count on a witnessed calls edge. A calls edge pairs a CLIENT span with its child
SERVER span by trace_id and parent_span_id = span_id. request_count counts paired server spans and
error_count counts paired server spans whose status is STATUS_CODE_ERROR.

Return evidence for every direct callee of the supplied caller, not a query prefiltered to the
winning destination. You may cite either a successful, untruncated relationships query or an
independent trace-table reconstruction when the corresponding tool is available. A relationships
query must cover service calls from the supplied caller with trace provenance. A trace query must
return exactly these columns: src_type, src_id, dst_type, dst_id, rel_type, provenance,
request_count, and error_count. Use the supplied half-open window for CLIENT spans.
Discover physical tables and columns before relying on them. Copy the real query_id from the cited
result into the final output."""


def graph_task_prompt(database: str, fixture: GraphFixture, max_tool_calls: int) -> str:
    return f"""Find the highest-error direct service callee for a supplied caller.

Database: {database}
Caller entity type: service
Caller entity ID: {fixture.caller}
Relationship type: calls
Relationship provenance: trace
Analysis window: [{_iso_time(fixture.window_start)}, {_iso_time(fixture.window_end)})
Investigation budget: at most {max_tool_calls} database tool calls.

Return the winning edge identity, its request_count and error_count, the evidence query_id, and a
concise claim. The destination and counts are hidden. The winner must be the unique greatest
error_count among all direct callees returned by the cited evidence."""


def audit_graph_fixture(client: GreptimeClient, fixture: GraphFixture) -> GraphAudit:
    trace_query = canonical_trace_query(fixture)
    trace_result = client.query(trace_query, max_rows=None)
    graph_query = canonical_graph_query(client.database, fixture)
    graph_result = client.query(graph_query, max_rows=None)
    edge_sets_match = edge_results_match(trace_result, graph_result)
    winner = _unique_winner(graph_result, fixture.caller)
    unique_winner = winner is not None
    expected_winner_match = winner == _expected_winner(fixture)
    return GraphAudit(
        fixture_id=fixture.fixture_id,
        trace_query=trace_query,
        trace_result=trace_result,
        graph_query=graph_query,
        graph_result=graph_result,
        edge_sets_match=edge_sets_match,
        unique_winner=unique_winner,
        expected_winner_match=expected_winner_match,
    )


def edge_results_match(left: QueryResult, right: QueryResult) -> bool:
    if left.truncated or right.truncated:
        return False
    left_edges = canonical_edge_set(left)
    return left_edges is not None and left_edges == canonical_edge_set(right)


def canonical_trace_query(fixture: GraphFixture) -> str:
    table = _quote(fixture.trace_table)
    caller = _literal(fixture.caller)
    start = _time_literal(fixture.window_start)
    end = _time_literal(fixture.window_end)
    server_start = _time_literal(fixture.window_start - 5 * 60)
    server_end = _time_literal(fixture.window_end + 60 * 60)
    return f"""SELECT 'service' AS src_type, c.service_name AS src_id,
       'service' AS dst_type, s.service_name AS dst_id,
       'calls' AS rel_type, 'trace' AS provenance,
       COUNT(*) AS request_count,
       SUM(CASE WHEN s.span_status_code = 'STATUS_CODE_ERROR' THEN 1 ELSE 0 END) AS error_count
FROM {table} c
JOIN {table} s
  ON c.trace_id = s.trace_id
 AND s.parent_span_id = c.span_id
 AND s.timestamp >= c.timestamp - INTERVAL '5 minutes'
 AND s.timestamp <= c.timestamp + INTERVAL '1 hour'
WHERE c.span_kind = 'SPAN_KIND_CLIENT'
  AND s.span_kind = 'SPAN_KIND_SERVER'
  AND c.service_name = {caller}
  AND c.timestamp >= {start} AND c.timestamp < {end}
  AND s.timestamp >= {server_start} AND s.timestamp < {server_end}
GROUP BY c.service_name, s.service_name
ORDER BY src_id, dst_id"""


def canonical_graph_query(database: str, fixture: GraphFixture) -> str:
    return _semantic_graph_query(
        _case_input(database, fixture),
        {
            "view": "relationships",
            "src_type": "service",
            "src_id": fixture.caller,
            "dst_type": "service",
            "rel_type": "calls",
            "provenance": "trace",
            "limit": 200,
        },
    )


def evaluate_graph_run(
    run: GraphAgentRun,
    fixture: GraphFixture,
    canonical_result: QueryResult,
    *,
    database: str | None = None,
) -> GraphEvaluation:
    expected = _expected_winner(fixture)
    answer_match = run.answer is not None and _answer_edge(run.answer) == expected
    evidence_index, trace = _evidence_trace(
        run,
        run.answer.evidence_query_id if run.answer else None,
    )
    allowed_tools = (
        {"execute_sql", "query_semantic_graph"}
        if run.visibility is Visibility.SEMANTIC_GRAPH
        else {"execute_sql"}
    )
    citation_valid = (
        trace is not None
        and trace.tool_name in allowed_tools
        and trace.error is None
        and isinstance(trace.output, dict)
        and not bool(trace.output.get("truncated"))
    )
    evidence_result = _query_result_from_trace(trace) if citation_valid else None
    if trace is not None and trace.tool_name == "query_semantic_graph":
        evidence_scope_valid = (
            _valid_graph_scope(
                trace.input,
                fixture,
                len(canonical_edge_set(canonical_result) or set()),
            )
            if trace is not None
            else False
        )
    elif trace is not None and trace.tool_name == "execute_sql":
        query = str(trace.input.get("query", "")) if trace is not None else ""
        evidence_scope_valid = _valid_trace_scope(query, fixture, database)
    else:
        evidence_scope_valid = False
    evidence_edges = canonical_edge_set(evidence_result) if evidence_result is not None else None
    canonical_edges = canonical_edge_set(canonical_result)
    evidence_result_match = (
        evidence_edges is not None
        and evidence_edges == canonical_edges
        and (
            (trace is not None and trace.tool_name == "query_semantic_graph")
            or _has_exact_trace_result_columns(evidence_result)
        )
    )
    unique_winner = _unique_winner(canonical_result, fixture.caller) == expected
    checks = {
        "submitted edge or RED counts do not match the frozen winner": answer_match,
        "missing, failed, truncated, or unavailable-tool evidence citation": citation_valid,
        "evidence query does not cover the frozen caller and complete destination set": (
            evidence_scope_valid
        ),
        "evidence result does not match the independently audited edge set": (
            evidence_result_match
        ),
        "canonical edge set has no unique frozen winner": unique_winner,
    }
    failure_reasons = [reason for reason, passed in checks.items() if not passed]
    if run.error:
        failure_reasons.insert(0, run.error)
    if run.tool_budget_exhausted:
        failure_reasons.insert(0, "investigation tool budget exhausted")
    calls_through_evidence = (
        run.tool_calls[: evidence_index + 1] if evidence_index is not None else None
    )
    rows_through_evidence = (
        sum(item.database_load.rows_returned for item in calls_through_evidence)
        if calls_through_evidence is not None
        and all(item.database_load is not None for item in calls_through_evidence)
        else None
    )
    return GraphEvaluation(
        success=not failure_reasons,
        answer_match=answer_match,
        citation_valid=citation_valid,
        evidence_scope_valid=evidence_scope_valid,
        evidence_result_match=evidence_result_match,
        unique_winner=unique_winner,
        failure_reasons=failure_reasons,
        tool_calls_through_evidence=evidence_index + 1 if evidence_index is not None else None,
        rows_returned_through_evidence=rows_through_evidence,
    )


def graph_output_schema() -> dict[str, object]:
    fields = {
        "src_type": {"type": "string"},
        "src_id": {"type": "string"},
        "dst_type": {"type": "string"},
        "dst_id": {"type": "string"},
        "rel_type": {"type": "string"},
        "provenance": {"type": "string"},
        "request_count": {"type": "integer", "minimum": 0},
        "error_count": {"type": "integer", "minimum": 0},
        "evidence_query_id": {"type": "string"},
        "claim": {"type": "string"},
    }
    return {
        "type": "object",
        "properties": fields,
        "required": list(fields),
        "additionalProperties": False,
    }


def _case_input(database: str, fixture: GraphFixture) -> CaseInput:
    return CaseInput(
        case_token=uuid.uuid4().hex,
        time_start=fixture.window_start,
        time_end=fixture.window_end,
        alert_time=fixture.window_start,
        database=database,
    )


def _expected_winner(fixture: GraphFixture) -> tuple[str, str, str, str, str, str, int, int]:
    return (
        "service",
        fixture.caller,
        "service",
        fixture.expected_callee,
        "calls",
        "trace",
        fixture.expected_request_count,
        fixture.expected_error_count,
    )


def _answer_edge(answer: GraphAnswer) -> tuple[str, str, str, str, str, str, int, int]:
    return (
        answer.src_type,
        answer.src_id,
        answer.dst_type,
        answer.dst_id,
        answer.rel_type,
        answer.provenance,
        answer.request_count,
        answer.error_count,
    )


def canonical_edge_set(
    result: QueryResult,
) -> set[tuple[str, str, str, str, str, str, int, int]] | None:
    required = [
        "src_type",
        "src_id",
        "dst_type",
        "dst_id",
        "rel_type",
        "provenance",
        "request_count",
        "error_count",
    ]
    columns = [column.lower() for column in result.columns]
    if any(columns.count(column) != 1 for column in required):
        return None
    indexes = [columns.index(column) for column in required]
    edges = set()
    for row in result.rows:
        if len(row) <= max(indexes):
            return None
        values = [row[index] for index in indexes]
        if not isinstance(values[6], int) or not isinstance(values[7], int):
            return None
        edges.add(
            (
                str(values[0]),
                str(values[1]),
                str(values[2]),
                str(values[3]),
                str(values[4]),
                str(values[5]),
                values[6],
                values[7],
            )
        )
    return edges if len(edges) == len(result.rows) and edges else None


def _unique_winner(
    result: QueryResult,
    caller: str,
) -> tuple[str, str, str, str, str, str, int, int] | None:
    edges = canonical_edge_set(result)
    if not edges:
        return None
    candidates = [
        edge
        for edge in edges
        if edge[0] == "service"
        and edge[1] == caller
        and edge[2] == "service"
        and edge[4] == "calls"
        and edge[5] == "trace"
    ]
    if not candidates:
        return None
    maximum = max(edge[7] for edge in candidates)
    winners = [edge for edge in candidates if edge[7] == maximum]
    return winners[0] if len(winners) == 1 else None


def _valid_graph_scope(
    arguments: dict[str, object],
    fixture: GraphFixture,
    canonical_edge_count: int,
) -> bool:
    limit = arguments.get("limit", 100)
    if not isinstance(limit, int):
        return False
    return (
        arguments.get("view") == "relationships"
        and arguments.get("src_type") in (None, "", "service")
        and arguments.get("src_id") == fixture.caller
        and arguments.get("dst_type") in (None, "", "service")
        and arguments.get("dst_id") in (None, "")
        and arguments.get("rel_type") == "calls"
        and arguments.get("provenance") == "trace"
        and limit >= canonical_edge_count
    )


def _has_exact_trace_result_columns(result: QueryResult) -> bool:
    return [column.lower() for column in result.columns] == [
        "src_type",
        "src_id",
        "dst_type",
        "dst_id",
        "rel_type",
        "provenance",
        "request_count",
        "error_count",
    ]


def _valid_trace_scope(query: str, fixture: GraphFixture, database: str | None) -> bool:
    try:
        statements = sqlglot.parse(query, read="mysql")
    except sqlglot.errors.ParseError:
        return False
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        return False
    statement = statements[0]
    if _valid_derived_trace_scope(statement, fixture, database):
        return True
    tables = list(statement.find_all(exp.Table))
    if len(tables) != 2 or not all(
        not table.catalog
        and table.name.lower() == fixture.trace_table.lower()
        and (not table.db or database is not None and table.db.lower() == database.lower())
        for table in tables
    ):
        return False
    aliases = {table.alias_or_name.lower() for table in tables}
    if len(aliases) != 2:
        return False
    literals = {
        str(literal.this) for literal in statement.find_all(exp.Literal) if literal.is_string
    }
    client_aliases = {
        alias
        for equality in statement.find_all(exp.EQ)
        if (alias := _column_literal_alias(equality, "span_kind", "SPAN_KIND_CLIENT")) is not None
        and _is_conjunctive(equality, statement)
    }
    server_aliases = {
        alias
        for equality in statement.find_all(exp.EQ)
        if (alias := _column_literal_alias(equality, "span_kind", "SPAN_KIND_SERVER")) is not None
        and _is_conjunctive(equality, statement)
    }
    if len(client_aliases) != 1 or len(server_aliases) != 1:
        return False
    client_alias = next(iter(client_aliases))
    server_alias = next(iter(server_aliases))
    if {client_alias, server_alias} != aliases:
        return False
    caller_scoped = any(
        _column_literal_alias(equality, "service_name", fixture.caller) == client_alias
        and _is_conjunctive(equality, statement)
        for equality in statement.find_all(exp.EQ)
    )
    bounds = {
        bound
        for comparison_type, operator in ((exp.GTE, ">="), (exp.LT, "<"))
        for comparison in statement.find_all(comparison_type)
        if (bound := _time_bound(comparison, operator)) is not None
        and _is_conjunctive(comparison, statement)
    }
    equalities = [
        equality for equality in statement.find_all(exp.EQ) if _is_conjunctive(equality, statement)
    ]
    has_trace_join = any(
        _qualified_column_pair(item) == {(client_alias, "trace_id"), (server_alias, "trace_id")}
        for item in equalities
    )
    has_parent_join = any(
        _qualified_column_pair(item)
        == {(client_alias, "span_id"), (server_alias, "parent_span_id")}
        for item in equalities
    )
    projections = {
        projection.alias_or_name.lower(): projection for projection in statement.expressions
    }
    destination = projections.get("dst_id")
    destination_columns = list(destination.find_all(exp.Column)) if destination is not None else []
    return (
        bool(list(statement.find_all(exp.Join)))
        and "STATUS_CODE_ERROR" in literals
        and caller_scoped
        and {
            (client_alias, ">=", fixture.window_start),
            (client_alias, "<", fixture.window_end),
        }
        <= bounds
        and has_trace_join
        and has_parent_join
        and len(destination_columns) == 1
        and destination_columns[0].table.lower() == server_alias
        and destination_columns[0].name.lower() == "service_name"
        and _projection_is_grouped(statement, destination, destination_columns[0])
    )


def _valid_derived_trace_scope(
    statement: exp.Select,
    fixture: GraphFixture,
    database: str | None,
) -> bool:
    if not (any(statement.find_all(exp.Subquery)) or any(statement.find_all(exp.CTE))):
        return False
    cte_names = {cte.alias_or_name.lower() for cte in statement.find_all(exp.CTE)}
    tables = [
        table for table in statement.find_all(exp.Table) if table.name.lower() not in cte_names
    ]
    if len(tables) != 2 or not all(
        not table.catalog
        and table.name.lower() == fixture.trace_table.lower()
        and (not table.db or database is not None and table.db.lower() == database.lower())
        for table in tables
    ):
        return False

    root = build_scope(statement)
    if root is None:
        return False
    scopes = list(root.traverse())
    client_scopes = [
        scope
        for scope in scopes
        if isinstance(scope.expression, exp.Select)
        and _valid_client_scope(scope.expression, fixture)
    ]
    server_scopes = [
        scope
        for scope in scopes
        if isinstance(scope.expression, exp.Select)
        and _select_has_literal_filter(scope.expression, "span_kind", "SPAN_KIND_SERVER")
    ]
    if len(client_scopes) != 1 or len(server_scopes) != 1:
        return False

    client_scope_ids = {id(scope) for scope in client_scopes}
    server_scope_ids = {id(scope) for scope in server_scopes}
    bound_aliases: tuple[set[str], set[str]] | None = None
    for scope in scopes:
        select = scope.expression
        if not isinstance(select, exp.Select) or not list(select.find_all(exp.Join)):
            continue
        client_aliases = {
            alias.lower()
            for alias, source in scope.sources.items()
            if id(source) in client_scope_ids
        }
        server_aliases = {
            alias.lower()
            for alias, source in scope.sources.items()
            if id(source) in server_scope_ids
        }
        server_filters = _select_literal_filter_aliases(select, "span_kind", "SPAN_KIND_SERVER")
        server_aliases.update(
            alias.lower()
            for alias, source in scope.sources.items()
            if isinstance(source, exp.Table) and alias.lower() in server_filters
        )
        if not client_aliases or not server_aliases:
            continue
        equalities = [
            equality
            for equality in select.find_all(exp.EQ)
            if equality.find_ancestor(exp.Select) is select and _is_conjunctive(equality, select)
        ]
        has_trace_join = any(
            _qualified_column_pair(equality)
            == {(client_alias, "trace_id"), (server_alias, "trace_id")}
            for equality in equalities
            for client_alias in client_aliases
            for server_alias in server_aliases
        )
        has_parent_join = any(
            _qualified_column_pair(equality)
            == {(client_alias, "span_id"), (server_alias, "parent_span_id")}
            for equality in equalities
            for client_alias in client_aliases
            for server_alias in server_aliases
        )
        if has_trace_join and has_parent_join:
            bound_aliases = client_aliases, server_aliases
            break
    if bound_aliases is None:
        return False

    projections = {
        projection.alias_or_name.lower(): projection for projection in statement.expressions
    }
    required = {
        "src_type",
        "src_id",
        "dst_type",
        "dst_id",
        "rel_type",
        "provenance",
        "request_count",
        "error_count",
    }
    if not required <= set(projections):
        return False
    destination = projections["dst_id"]
    destination_columns = list(destination.find_all(exp.Column))
    if len(destination_columns) != 1 or destination_columns[0].name.lower() not in {
        "service_name",
        "dst_service",
    }:
        return False
    request_count = projections["request_count"]
    error_count = projections["error_count"]
    if not list(request_count.find_all(exp.Count)):
        return False
    if (
        not list(error_count.find_all(exp.Count, exp.Sum))
        or not any(
            column.name.lower() == "span_status_code" for column in error_count.find_all(exp.Column)
        )
        or "STATUS_CODE_ERROR"
        not in {
            str(literal.this) for literal in error_count.find_all(exp.Literal) if literal.is_string
        }
    ):
        return False
    return _projection_is_grouped(statement, destination, destination_columns[0])


def _projection_is_grouped(
    statement: exp.Select,
    projection: exp.Expression,
    source_column: exp.Column,
) -> bool:
    group = statement.args.get("group")
    if not isinstance(group, exp.Group):
        return False
    position = statement.expressions.index(projection) + 1
    return any(
        (isinstance(item, exp.Literal) and not item.is_string and str(item.this) == str(position))
        or (
            isinstance(item, exp.Column)
            and item.name.lower() in {projection.alias_or_name.lower(), source_column.name.lower()}
            and (not item.table or not source_column.table or item.table == source_column.table)
        )
        for item in group.expressions
    )


def _select_has_literal_filter(select: exp.Select, column_name: str, value: str) -> bool:
    return bool(_select_literal_filter_aliases(select, column_name, value))


def _valid_client_scope(select: exp.Select, fixture: GraphFixture) -> bool:
    role_aliases = _select_literal_filter_aliases(select, "span_kind", "SPAN_KIND_CLIENT")
    caller_aliases = _select_literal_filter_aliases(select, "service_name", fixture.caller)
    return (
        len(role_aliases) == 1
        and role_aliases == caller_aliases
        and _select_has_window(select, fixture, next(iter(role_aliases)))
    )


def _select_literal_filter_aliases(
    select: exp.Select,
    column_name: str,
    value: str,
) -> set[str]:
    return {
        column.table.lower()
        for equality in select.find_all(exp.EQ)
        if equality.find_ancestor(exp.Select) is select and _is_conjunctive(equality, select)
        for column, literal in (
            (equality.this, equality.expression),
            (equality.expression, equality.this),
        )
        if isinstance(column, exp.Column)
        and column.name.lower() == column_name.lower()
        and isinstance(literal, exp.Literal)
        and literal.is_string
        and str(literal.this) == value
    }


def _select_has_window(select: exp.Select, fixture: GraphFixture, alias: str) -> bool:
    bounds = {
        bound
        for comparison_type, operator in ((exp.GTE, ">="), (exp.LT, "<"))
        for comparison in select.find_all(comparison_type)
        if comparison.find_ancestor(exp.Select) is select
        and (bound := _time_bound(comparison, operator)) is not None
        and _is_conjunctive(comparison, select)
    }
    return {
        (alias, ">=", fixture.window_start),
        (alias, "<", fixture.window_end),
    } <= bounds


def _qualified_column_pair(equality: exp.EQ) -> set[tuple[str, str]]:
    if isinstance(equality.this, exp.Column) and isinstance(equality.expression, exp.Column):
        return {
            (equality.this.table.lower(), equality.this.name.lower()),
            (equality.expression.table.lower(), equality.expression.name.lower()),
        }
    return set()


def _column_literal_alias(
    equality: exp.EQ,
    column_name: str,
    expected: str,
) -> str | None:
    for column, literal in (
        (equality.this, equality.expression),
        (equality.expression, equality.this),
    ):
        if (
            isinstance(column, exp.Column)
            and column.name.lower() == column_name.lower()
            and column.table
            and isinstance(literal, exp.Literal)
            and literal.is_string
            and str(literal.this) == expected
        ):
            return column.table.lower()
    return None


def _is_conjunctive(expression: exp.Expression, scope: exp.Select) -> bool:
    node = expression.parent
    while node is not None and node is not scope:
        if isinstance(node, (exp.Or, exp.Not)):
            return False
        node = node.parent
    return node is scope


def _time_bound(comparison: exp.Expression, operator: str) -> tuple[str, str, int] | None:
    left = comparison.this
    right = comparison.expression
    if not isinstance(left, exp.Column) or left.name.lower() != "timestamp":
        return None
    if isinstance(right, exp.Cast):
        right = right.this
    if not isinstance(right, exp.Literal) or not right.is_string:
        return None
    try:
        value = datetime.fromisoformat(str(right.this).replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return left.table.lower(), operator, int(value.timestamp())


def _evidence_trace(
    run: GraphAgentRun,
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


def _ceil_minute(epoch: int) -> int:
    return ((epoch + 59) // 60) * 60


def _quote(identifier: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", identifier):
        raise ValueError(f"invalid graph benchmark identifier: {identifier}")
    return identifier


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _time_literal(epoch: int) -> str:
    return _literal(datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%d %H:%M:%S"))


def _iso_time(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat()
