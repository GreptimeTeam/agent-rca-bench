from __future__ import annotations

import sqlglot
from sqlglot import exp

from semantic_rca_bench.contracts import QueryResult, ToolTrace
from semantic_rca_bench.split_query import NativeQueryResult

NATIVE_EVIDENCE_OPERATIONS = {
    "query_metrics": {"query", "query_range"},
    "query_logs": {"query_range"},
    "query_traces": {"search", "get_trace", "metrics_range"},
}


def is_valid_evidence_trace(matches: list[ToolTrace]) -> bool:
    if len(matches) != 1:
        return False
    trace = matches[0]
    if trace.error is not None:
        return False
    if trace.tool_name in NATIVE_EVIDENCE_OPERATIONS:
        if not isinstance(trace.output, dict):
            return False
        try:
            result = NativeQueryResult.model_validate(trace.output)
        except ValueError:
            return False
        return (
            result.query_id == trace.query_id
            and not result.truncated
            and result.operation in NATIVE_EVIDENCE_OPERATIONS[trace.tool_name]
        )
    if trace.tool_name not in {"execute_sql", "query_semantic_graph"}:
        return False
    if trace.tool_name == "execute_sql":
        query = str(trace.input.get("query") or trace.input.get("sql") or "")
        if not is_evidence_sql(query):
            return False
    if not isinstance(trace.output, dict):
        return False
    try:
        result = QueryResult.model_validate(trace.output)
    except ValueError:
        return False
    return result.query_id == trace.query_id and not result.truncated


def is_evidence_sql(query: str) -> bool:
    statement = None
    for dialect in ("postgres", "mysql"):
        try:
            statements = sqlglot.parse(query, read=dialect)
        except sqlglot.errors.ParseError:
            continue
        candidate = statements[0] if len(statements) == 1 else None
        while isinstance(candidate, exp.Subquery):
            candidate = candidate.this
        if isinstance(candidate, (exp.Select, exp.Union)):
            statement = candidate
            break
    if statement is None:
        return False
    tables = list(statement.find_all(exp.Table))
    if not tables:
        return False
    for table in tables:
        schema = table.db.lower()
        name = table.name.lower()
        if schema in {"information_schema", "pg_catalog"}:
            return False
        if schema == "greptime_private" and name not in {
            "semantic_entities",
            "semantic_relationships",
        }:
            return False
    return True
