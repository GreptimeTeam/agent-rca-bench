from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import anthropic
from pydantic import ValidationError

from semantic_rca_bench.contracts import (
    AgentRun,
    AgentRunner,
    AgentUsage,
    CaseInput,
    DatabaseLoad,
    Diagnosis,
    FaultCategory,
    QueryResult,
    RejectedToolCall,
    ToolTrace,
    Visibility,
)
from semantic_rca_bench.greptimedb.profile import TableProfiler
from semantic_rca_bench.greptimedb.visibility import QueryGateway


class AgentError(RuntimeError):
    pass


@dataclass(frozen=True)
class ToolInvocation:
    content: str
    is_error: bool
    remaining: int


@dataclass
class StructuredAgentResult:
    output: dict[str, object] | None
    error: str | None
    tool_calls: list[ToolTrace]
    rejected_tool_calls: list[RejectedToolCall]
    tool_calls_requested: int
    tool_budget_exhausted: bool
    usage: AgentUsage
    elapsed_seconds: float
    responses: list[dict[str, object]]


ANTHROPIC_KEYCHAIN_SERVICE = "semantic-rca-bench-anthropic"
DEEPSEEK_KEYCHAIN_SERVICE = "semantic-rca-bench-deepseek"
DEEPSEEK_ANTHROPIC_BASE_URL = "https://api.deepseek.com/anthropic"

TABLE_SEMANTICS_GUIDE = """
Semantic profile usage: signal_type says whether the table contains metrics, logs, traces, or
events; source and source_version identify the ingestion protocol; pipeline identifies the schema
or transformation that shaped the rows. semantic_options contains signal-specific facts such as
metric type, unit, temporality, original name, or trace conventions. metadata_quality says whether
metric metadata was declared by the source or inferred; it describes semantic-metadata certainty,
not telemetry quality. entity_declarations lists entities contributed by the table: entity_type is
the kind, id is the ordered list of identifying columns, id_qualifier optionally scopes the first
id value, descriptive lists non-identifying attributes, origin says declared or convention-derived,
and superseded_by records a preferred declaration. Null or missing semantic fields mean unknown,
not an opposite fact. Use the profile to map generic observability concepts to physical columns;
do not treat it as incident evidence by itself.
""".strip()

SEMANTIC_GRAPH_GUIDE = """
Semantic Graph usage: always restrict observed_at to the incident window. semantic_entities is a
time-windowed node observation table, not current state: entity_type is the OTel-style kind;
entity_id is the canonical identifying value; entity_id_attrs maps that id back to identifying
attribute names; scope is its namespace; descriptive contains non-identifying attributes; and
source_tables names the telemetry tables that witnessed it. window_start/window_end bound the
observation and fresh_until says how long it is considered present. Deduplicate entity snapshots by
(entity_type, entity_id) when asking which entities exist.

semantic_relationships is a time-windowed witnessed-edge table. Direction is src_type/src_id to
dst_type/dst_id and rel_type names the relationship. provenance says how it was obtained: trace,
attribute, declared, or agent. confidence is derivation certainty, not entity health, RCA
confidence, or correction for sampling. request_count, error_count, duration_sum, and
duration_count are windowed RED observations; duration_sum/duration_count gives mean duration when
duration_count is nonzero. attributes contains edge-specific facts. Deduplicate topology by
(src_type, src_id, dst_type, dst_id, rel_type, provenance) across windows. Missing edges can result
from no dependency, missing instrumentation, sampling, access filtering, or a narrow time window;
absence alone does not prove entities are unrelated.
""".strip()


SUBMIT_TOOL = {
    "name": "submit_diagnosis",
    "description": "Submit the final root-cause diagnosis and finish the investigation.",
    "input_schema": {
        "type": "object",
        "properties": {
            "affected_component": {
                "type": "string",
                "description": (
                    "The workload or infrastructure component directly affected by the causal "
                    "fault. For a dependency failure, name the caller that lost access here and "
                    "put the unavailable dependency in causal_dependency."
                ),
            },
            "causal_dependency": {
                "type": ["string", "null"],
                "description": (
                    "The downstream service, datastore, or external endpoint whose failure "
                    "caused the affected component to fail, or null when no dependency is "
                    "identified."
                ),
            },
            "fault_type": {
                "type": "string",
                "description": (
                    "The causal fault category or mechanism, not merely an observed symptom. "
                    "Keep it concise and do not include ruled-out alternatives."
                ),
            },
            "fault_category": {
                "type": "string",
                "enum": [category.value for category in FaultCategory],
                "description": (
                    "Canonical causal category: cpu for compute saturation or throttling; "
                    "delay for injected or degraded latency without a more specific resource "
                    "mechanism; disk for storage I/O; loss for packet or network loss; memory "
                    "for exhaustion, leaks, or OOM; socket for connection or socket exhaustion; "
                    "other only when the evidenced mechanism is outside this taxonomy."
                ),
            },
            "onset_time": {
                "type": ["string", "null"],
                "description": (
                    "The earliest telemetry-supported anomaly time in ISO 8601 form, or null "
                    "when the available evidence cannot establish it."
                ),
            },
            "confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": (
                    "Epistemic confidence in the joint component-and-fault conclusion, "
                    "calibrated against plausible alternatives and telemetry gaps."
                ),
            },
            "evidence": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "query_id": {"type": "string"},
                        "claim": {"type": "string"},
                    },
                    "required": ["query_id", "claim"],
                    "additionalProperties": False,
                },
            },
            "alternative_candidates": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 3,
            },
            "explanation": {"type": "string"},
        },
        "required": [
            "affected_component",
            "causal_dependency",
            "fault_category",
            "fault_type",
            "confidence",
            "evidence",
            "alternative_candidates",
            "explanation",
        ],
        "additionalProperties": False,
    },
}


class InvestigationSession:
    def __init__(
        self,
        gateway: QueryGateway,
        case_input: CaseInput,
        visibility: Visibility,
        *,
        max_tool_calls: int,
        semantic_coverage: dict[str, object] | None,
        investigation_tools: list[dict[str, object]] | None = None,
    ) -> None:
        self.gateway = gateway
        self.case_input = case_input
        self.visibility = visibility
        self.max_tool_calls = max_tool_calls
        self.semantic_coverage = semantic_coverage
        self.profiler = TableProfiler(gateway.client, visibility)
        self.tool_calls: list[ToolTrace] = []
        self.rejected_tool_calls: list[RejectedToolCall] = []
        self.tool_calls_requested = 0
        self.tool_budget_exhausted = False
        tools = (
            investigation_tools
            if investigation_tools is not None
            else _investigation_tools(
                visibility,
                case_input.fault_taxonomy,
                semantic_coverage,
            )
        )
        self.allowed_tools = {str(tool["name"]) for tool in tools}

    def invoke(self, tool_name: str, arguments: dict[str, object]) -> ToolInvocation:
        self.tool_calls_requested += 1
        if tool_name not in self.allowed_tools:
            error = f"unknown or unavailable tool: {tool_name}"
            self.rejected_tool_calls.append(
                RejectedToolCall(tool_name=tool_name, input=arguments, error=error)
            )
            return ToolInvocation(
                content=error,
                is_error=True,
                remaining=self.remaining,
            )
        if len(self.tool_calls) >= self.max_tool_calls:
            self.tool_budget_exhausted = True
            return ToolInvocation(
                content=f"tool call budget exhausted ({self.max_tool_calls})",
                is_error=True,
                remaining=0,
            )
        load_before = self._load_snapshot()
        try:
            output = self._execute(tool_name, arguments)
            database_load = _database_load_delta(load_before, self._load_snapshot())
            output, query_id = _citation_output(output, len(self.tool_calls) + 1)
            self.tool_calls.append(
                ToolTrace(
                    tool_name=tool_name,
                    input=arguments,
                    query_id=query_id,
                    output=output,
                    database_load=database_load,
                )
            )
            return ToolInvocation(
                content=json.dumps(output, separators=(",", ":"), default=str),
                is_error=False,
                remaining=self.remaining,
            )
        except Exception as error:
            database_load = _database_load_delta(load_before, self._load_snapshot())
            self.tool_calls.append(
                ToolTrace(
                    tool_name=tool_name,
                    input=arguments,
                    error=str(error),
                    database_load=database_load,
                )
            )
            return ToolInvocation(
                content=str(error),
                is_error=True,
                remaining=self.remaining,
            )

    @property
    def remaining(self) -> int:
        return max(0, self.max_tool_calls - len(self.tool_calls))

    def _execute(self, tool_name: str, arguments: dict[str, object]) -> dict[str, object]:
        if tool_name == "execute_sql":
            result = self.gateway.execute(str(arguments.get("query", "")))
            return result.model_dump(mode="json")
        if tool_name == "describe_table":
            return self.profiler.describe(
                str(arguments.get("table", "")),
                include_samples=bool(arguments.get("include_samples", False)),
                sample_limit=int(arguments.get("sample_limit", 1)),
            )
        if tool_name == "search_table_semantics":
            output = self.profiler.search(
                str(arguments.get("query", "")),
                signal_type=(
                    str(arguments["signal_type"]) if arguments.get("signal_type") else None
                ),
                limit=int(arguments.get("limit", 50)),
            )
            return _catalog_search_output(output, self.case_input.fault_taxonomy)
        result = self.gateway.execute(_semantic_graph_query(self.case_input, arguments))
        return _semantic_graph_output(result, arguments)

    def _load_snapshot(self) -> DatabaseLoad | None:
        snapshot = getattr(self.gateway.client, "query_load_snapshot", None)
        return snapshot() if callable(snapshot) else None


def _database_load_delta(
    before: DatabaseLoad | None,
    after: DatabaseLoad | None,
) -> DatabaseLoad | None:
    if before is None or after is None:
        return None
    query_count = after.query_count - before.query_count
    return DatabaseLoad(
        query_count=query_count,
        failed_query_count=after.failed_query_count - before.failed_query_count,
        rows_returned=after.rows_returned - before.rows_returned,
        query_elapsed_seconds=after.query_elapsed_seconds - before.query_elapsed_seconds,
        max_concurrency=after.max_concurrency if query_count else 0,
    )


def run_agent(
    gateway: QueryGateway,
    case_input: CaseInput,
    visibility: Visibility,
    *,
    model: str,
    max_tool_calls: int = 24,
    max_turns: int | None = None,
    max_tokens: int = 4096,
    semantic_coverage: dict[str, object] | None = None,
) -> AgentRun:
    tools = _investigation_tools(
        visibility,
        case_input.fault_taxonomy,
        semantic_coverage,
    )
    result = run_structured_api_agent(
        gateway,
        case_input,
        visibility,
        model=model,
        system_prompt=_system_prompt(),
        user_prompt=_incident_prompt(case_input, max_tool_calls),
        investigation_tools=tools,
        output_tool=_submit_tool(case_input.fault_taxonomy),
        validate_output=lambda value: Diagnosis.model_validate(value).model_dump(mode="json"),
        max_tool_calls=max_tool_calls,
        max_turns=max_turns,
        max_tokens=max_tokens,
        semantic_coverage=semantic_coverage,
    )
    diagnosis = Diagnosis.model_validate(result.output) if result.output is not None else None
    error = result.error
    if error and error.startswith("agent did not submit final output within "):
        error = error.replace("final output", "a diagnosis", 1)
    return AgentRun(
        run_id=uuid.uuid4().hex,
        visibility=visibility,
        model=model,
        runner=AgentRunner.API,
        diagnosis=diagnosis,
        error=error,
        tool_calls=result.tool_calls,
        rejected_tool_calls=result.rejected_tool_calls,
        tool_calls_requested=result.tool_calls_requested,
        tool_budget_exhausted=result.tool_budget_exhausted,
        usage=result.usage,
        elapsed_seconds=result.elapsed_seconds,
        responses=result.responses,
    )


def run_structured_api_agent(
    gateway: QueryGateway,
    case_input: CaseInput,
    visibility: Visibility,
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    investigation_tools: list[dict[str, object]],
    output_tool: dict[str, object],
    validate_output: Callable[[object], dict[str, object]],
    max_tool_calls: int,
    max_turns: int | None,
    max_tokens: int = 4096,
    semantic_coverage: dict[str, object] | None = None,
) -> StructuredAgentResult:
    if max_turns is None:
        max_turns = max_tool_calls + 10
    if max_turns < max_tool_calls + 1:
        raise ValueError("max_turns must allow every tool call and a final output turn")
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_prompt}]
    session = InvestigationSession(
        gateway,
        case_input,
        visibility,
        max_tool_calls=max_tool_calls,
        semantic_coverage=semantic_coverage,
        investigation_tools=investigation_tools,
    )
    responses: list[dict[str, object]] = []
    usage = AgentUsage()
    started = time.monotonic()
    output_tool_name = str(output_tool["name"])
    try:
        client = _anthropic_client(model)
    except Exception as error:
        return _structured_result(
            session,
            usage,
            responses,
            started,
            error=f"agent provider failed: {error}",
        )

    for _ in range(max_turns):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system_prompt,
                tools=[*investigation_tools, output_tool],
                messages=messages,
            )
        except Exception as error:
            return _structured_result(
                session,
                usage,
                responses,
                started,
                error=f"agent provider failed: {error}",
            )
        responses.append(response.model_dump(mode="json"))
        usage.input_tokens += response.usage.input_tokens
        usage.output_tokens += response.usage.output_tokens
        messages.append({"role": "assistant", "content": response.content})

        tool_uses = [block for block in response.content if block.type == "tool_use"]
        output_block = next(
            (block for block in tool_uses if block.name == output_tool_name),
            None,
        )
        output_error: str | None = None
        if output_block is not None:
            try:
                output = validate_output(output_block.input)
            except (ValidationError, ValueError, TypeError) as error:
                output_error = str(error)
            else:
                return _structured_result(
                    session,
                    usage,
                    responses,
                    started,
                    output=output,
                )

        if not tool_uses:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"Continue the investigation or call {output_tool_name}. "
                        f"You have {session.remaining} investigation "
                        "tool calls remaining."
                    ),
                }
            )
            continue

        tool_results: list[dict[str, object]] = []
        for tool_use in tool_uses:
            if tool_use.name == output_tool_name:
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "content": f"invalid final output: {output_error}",
                        "is_error": True,
                    }
                )
                continue
            invocation = session.invoke(tool_use.name, dict(tool_use.input))
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": invocation.content,
                    "is_error": invocation.is_error,
                }
            )
        tool_results.append(
            {
                "type": "text",
                "text": (
                    f"Investigation budget: {session.remaining} tool calls remain. "
                    f"Call {output_tool_name} before the budget reaches zero."
                ),
            }
        )
        messages.append({"role": "user", "content": tool_results})

    return _structured_result(
        session,
        usage,
        responses,
        started,
        error=f"agent did not submit final output within {max_turns} turns",
    )


def _structured_result(
    session: InvestigationSession,
    usage: AgentUsage,
    responses: list[dict[str, object]],
    started: float,
    *,
    output: dict[str, object] | None = None,
    error: str | None = None,
) -> StructuredAgentResult:
    return StructuredAgentResult(
        output=output,
        error=error,
        tool_calls=session.tool_calls,
        rejected_tool_calls=session.rejected_tool_calls,
        tool_calls_requested=session.tool_calls_requested,
        tool_budget_exhausted=session.tool_budget_exhausted,
        usage=usage,
        elapsed_seconds=time.monotonic() - started,
        responses=responses,
    )


def _anthropic_client(model: str) -> anthropic.Anthropic:
    if model.startswith("deepseek-"):
        api_key = _api_credential("DEEPSEEK_API_KEY", DEEPSEEK_KEYCHAIN_SERVICE)
        return anthropic.Anthropic(
            api_key=api_key,
            base_url=DEEPSEEK_ANTHROPIC_BASE_URL,
        )
    api_key = _api_credential("ANTHROPIC_API_KEY", ANTHROPIC_KEYCHAIN_SERVICE)
    return anthropic.Anthropic(api_key=api_key)


def _api_credential(environment_variable: str, keychain_service: str) -> str:
    api_key = os.environ.get(environment_variable)
    if not api_key and sys.platform == "darwin":
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", keychain_service, "-w"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            api_key = result.stdout.strip()
    if not api_key:
        raise AgentError(
            f"credential not found in {environment_variable} or macOS Keychain "
            f"service {keychain_service}"
        )
    return api_key


def _agent_tools(
    visibility: Visibility,
    fault_taxonomy: list[str],
    semantic_coverage: dict[str, object] | None,
) -> list[dict[str, object]]:
    tools = [
        _execute_sql_tool(visibility, semantic_coverage),
        _describe_table_tool(visibility),
    ]
    if visibility is not Visibility.RAW:
        tools.append(_search_table_semantics_tool())
    if visibility is Visibility.SEMANTIC_GRAPH and _graph_status(semantic_coverage) != "empty":
        tools.append(_semantic_graph_tool(semantic_coverage))
    tools.append(_submit_tool(fault_taxonomy))
    return tools


def _investigation_tools(
    visibility: Visibility,
    fault_taxonomy: list[str],
    semantic_coverage: dict[str, object] | None,
) -> list[dict[str, object]]:
    return [
        tool
        for tool in _agent_tools(visibility, fault_taxonomy, semantic_coverage)
        if tool["name"] != "submit_diagnosis"
    ]


def _execute_sql_tool(
    visibility: Visibility,
    semantic_coverage: dict[str, object] | None = None,
) -> dict[str, object]:
    description = (
        "Execute one read-only GreptimeDB SQL statement in the incident database. "
        "Use MySQL dialect and INFORMATION_SCHEMA for ordinary schema discovery."
    )
    if visibility is Visibility.SEMANTIC_GRAPH:
        graph = semantic_coverage.get("graph", {}) if semantic_coverage else {}
        status = _graph_status(semantic_coverage)
        if status == "empty":
            description += (
                " The coverage snapshot contains no Semantic Graph entities or relationships "
                "for this incident. Do not query graph tables or infer topology."
            )
        else:
            description += (
                " Semantic Graph is available: query computed entity identities from "
                "greptime_private.semantic_entities and witnessed relationships from "
                "greptime_private.semantic_relationships. Use observed_at to restrict graph "
                f"queries to the incident window. {SEMANTIC_GRAPH_GUIDE}"
            )
            if isinstance(graph, dict):
                entity_count = graph.get("distinct_entity_count", "unknown")
                relationship_count = graph.get("distinct_relationship_count", "unknown")
                description += (
                    f" Coverage snapshot: status={status}, distinct_entities={entity_count}, "
                    f"distinct_relationships={relationship_count}."
                )
                if status == "entity-only":
                    description += (
                        " No witnessed relationship is available in this window; do not infer "
                        "that entities are unrelated from the absence of edges."
                    )
    description += " Results include a query_id that can be cited as evidence."
    return {
        "name": "execute_sql",
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "One SELECT, SHOW, or DESCRIBE statement.",
                }
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    }


def _describe_table_tool(visibility: Visibility) -> dict[str, object]:
    description = "Get a table profile containing column schema and optional sample rows."
    if visibility is not Visibility.RAW:
        description += (
            " The profile also includes table semantic metadata: signal type, ingestion "
            "source and version, pipeline, metadata quality, semantic options, and entity "
            "declarations. Use this tool when deciding how to query an unfamiliar table. "
            f"{TABLE_SEMANTICS_GUIDE}"
        )
    description += " Results include a query_id that can be cited as evidence."
    return {
        "name": "describe_table",
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": "Table name in table or schema.table form.",
                },
                "include_samples": {"type": "boolean", "default": False},
                "sample_limit": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 3,
                    "default": 1,
                },
            },
            "required": ["table"],
            "additionalProperties": False,
        },
    }


def _search_table_semantics_tool() -> dict[str, object]:
    return {
        "name": "search_table_semantics",
        "description": (
            "Search semantic metadata across all tables in the incident database. Use this before "
            "describe_table when the schema is wide or the relevant table name is unknown. The "
            "query should contain telemetry concepts such as 'redis memory usage' or 'request "
            "latency'; it searches table names, semantic options, and entity declarations, then "
            "ranks tables by matched terms. Do not use generic business-domain words when "
            "looking for resource telemetry. For a generic alert, search concrete signal or "
            "fault-mechanism concepts from the allowed fault taxonomy early, before spending "
            "the tool budget on one signal. Retry with a different concrete concept if a search "
            "returns no matches. It does not search telemetry row values, so use the returned "
            "table schema and actual labels to "
            "locate a specific entity. Results include signal type, source, metadata quality, "
            "matched terms, and a query_id."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 200},
                "signal_type": {
                    "type": "string",
                    "enum": ["metric", "log", "trace", "event"],
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 50},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    }


def _catalog_search_output(
    output: dict[str, object],
    fault_taxonomy: list[str],
) -> dict[str, object]:
    if output.get("matched_table_count") == 0:
        mechanisms = ", ".join(fault_taxonomy) if fault_taxonomy else "the incident symptoms"
        output["guidance"] = (
            "No table metadata matched. Retry with one concrete telemetry or resource mechanism "
            f"instead of application-domain words. Candidate mechanisms: {mechanisms}."
        )
    return output


def _citation_output(
    output: dict[str, object],
    tool_index: int,
) -> tuple[dict[str, object], str]:
    query_id = f"q{tool_index:02d}"
    output["query_id"] = query_id
    return output, query_id


def _semantic_graph_tool(
    semantic_coverage: dict[str, object] | None = None,
) -> dict[str, object]:
    description = (
        "Query deduplicated Semantic Graph nodes or witnessed edges for the incident window. "
        "The tool supplies the greptime_private schema and time filter. Relationship rows are "
        "deduplicated per observation window before RED fields are summed. Entity identity "
        "fields are entity_type, entity_id, entity_id_attrs, and scope. Relationship direction "
        "is src_type/src_id to dst_type/dst_id; rel_type and provenance describe the edge; "
        "confidence is derivation certainty; request_count, error_count, duration_sum, and "
        "duration_count are windowed observations. Missing rows do not prove no relationship. "
        "Identifiers from alerts or telemetry providers are not Semantic Graph entity IDs unless "
        "an entity query returns that exact ID."
    )
    graph = semantic_coverage.get("graph", {}) if semantic_coverage else {}
    status = graph.get("status") if isinstance(graph, dict) else None
    if status == "relational":
        description += (
            " This incident has witnessed relationships. Start with view=relationships and no "
            "src_id or dst_id filter to discover canonical endpoints; use rel_type=calls to study "
            "request propagation. Filter by an endpoint only after the graph returned its ID."
        )
    elif status == "entity-only":
        description += (
            " This incident has entities but no witnessed relationships. Start with an unfiltered "
            "view=entities query to discover canonical IDs."
        )

    return {
        "name": "query_semantic_graph",
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": {
                "view": {"type": "string", "enum": ["entities", "relationships"]},
                "entity_type": {"type": "string"},
                "entity_id": {"type": "string"},
                "rel_type": {"type": "string"},
                "src_type": {"type": "string"},
                "src_id": {"type": "string"},
                "dst_type": {"type": "string"},
                "dst_id": {"type": "string"},
                "provenance": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 100},
            },
            "required": ["view"],
            "additionalProperties": False,
        },
    }


def _graph_status(semantic_coverage: dict[str, object] | None) -> str:
    if not semantic_coverage:
        return "unknown"
    graph = semantic_coverage.get("graph")
    if not isinstance(graph, dict):
        return "unknown"
    return str(graph.get("status") or "unknown")


def _semantic_graph_output(
    result: QueryResult,
    arguments: dict[str, object],
) -> dict[str, object]:
    output = result.model_dump(mode="json")
    id_filters = ("entity_id", "src_id", "dst_id")
    if not result.rows and any(arguments.get(field) not in (None, "") for field in id_filters):
        view = str(arguments.get("view") or "entities")
        output["guidance"] = (
            "No canonical graph ID matched this filter. Alert-provider and telemetry IDs are not "
            "interchangeable with Semantic Graph IDs. Retry "
            f"view={view} without entity_id, src_id, or dst_id, then filter using an ID returned "
            "by that query."
        )
    return output


def _semantic_graph_query(case_input: CaseInput, arguments: dict[str, object]) -> str:
    view = str(arguments.get("view") or "")
    if view not in {"entities", "relationships"}:
        raise AgentError("semantic graph view must be entities or relationships")
    start = datetime.fromtimestamp(case_input.time_start, UTC).strftime("%Y-%m-%d %H:%M:%S")
    end = datetime.fromtimestamp(case_input.time_end, UTC).strftime("%Y-%m-%d %H:%M:%S")
    limit = max(1, min(int(arguments.get("limit", 100)), 200))
    predicates = [f"observed_at >= '{start}'", f"observed_at < '{end}'"]

    if view == "entities":
        _append_graph_filters(predicates, arguments, ("entity_type", "entity_id"))
        where = " AND ".join(predicates)
        return f"""
            SELECT entity_type, entity_id, entity_id_attrs, scope,
                   MAX(observed_at) AS latest_observed_at,
                   MAX(fresh_until) AS fresh_until
            FROM greptime_private.semantic_entities
            WHERE {where}
            GROUP BY entity_type, entity_id, entity_id_attrs, scope
            ORDER BY entity_type, entity_id
            LIMIT {limit}
        """

    _append_graph_filters(
        predicates,
        arguments,
        ("rel_type", "src_type", "src_id", "dst_type", "dst_id", "provenance"),
    )
    where = " AND ".join(predicates)
    return f"""
        SELECT src_type, src_id, dst_type, dst_id, rel_type, provenance,
               MAX(confidence) AS confidence,
               SUM(request_count) AS request_count,
               SUM(error_count) AS error_count,
               SUM(duration_sum) AS duration_sum,
               SUM(duration_count) AS duration_count
        FROM (
            SELECT window_start, window_end, src_type, src_id, dst_type, dst_id,
                   rel_type, provenance,
                   MAX(confidence) AS confidence,
                   MAX(request_count) AS request_count,
                   MAX(error_count) AS error_count,
                   MAX(duration_sum) AS duration_sum,
                   MAX(duration_count) AS duration_count
            FROM greptime_private.semantic_relationships
            WHERE {where}
            GROUP BY window_start, window_end, src_type, src_id, dst_type, dst_id,
                     rel_type, provenance
        ) distinct_windows
        GROUP BY src_type, src_id, dst_type, dst_id, rel_type, provenance
        ORDER BY src_type, src_id, dst_type, dst_id, rel_type, provenance
        LIMIT {limit}
    """


def _append_graph_filters(
    predicates: list[str],
    arguments: dict[str, object],
    fields: tuple[str, ...],
) -> None:
    for field in fields:
        value = arguments.get(field)
        if value not in (None, ""):
            escaped = str(value).replace("'", "''")
            predicates.append(f"{field} = '{escaped}'")


def _submit_tool(fault_taxonomy: list[str]) -> dict[str, object]:
    tool = deepcopy(SUBMIT_TOOL)
    if fault_taxonomy:
        fault_type = tool["input_schema"]["properties"]["fault_type"]
        fault_type["description"] = (
            "Canonical causal mechanism from the dataset taxonomy. Choose the mechanism "
            "supported by telemetry, not a downstream symptom. Answers outside these labels "
            f"are recorded and scored as incorrect: {', '.join(fault_taxonomy)}."
        )
    return tool


def _system_prompt() -> str:
    return """You are the on-call SRE investigating an incident from telemetry in GreptimeDB.
Determine the single most likely root-cause component and causal fault type. Do not report a
surface symptom such as latency when evidence identifies a different causal mechanism. Work from
query evidence, not naming alone. Compare baseline and anomalous periods when the telemetry window
contains a known baseline, and correlate metrics, logs, and traces when present. Infer the change
point from telemetry rather than the alert time. Discover the schema before relying on column
names, and use semantic capabilities explicitly exposed by the tools when available. Stay inside
the named incident database.

When semantic catalog search is available and the alert is generic, search concrete resource or
signal mechanisms from the allowed fault taxonomy early. A zero-result catalog search is not
evidence that no relevant metric exists: retry with another concrete concept. Do not exhaust the
tool budget on traces or logs before checking plausible resource metrics.

GreptimeDB SQL notes:
- Compare Timestamp columns with timestamp string literals, not integer Unix epochs.
- Use date_bin('1 minute', timestamp_column) for time buckets.
- Quote identifiers containing dots with double quotes.
- In UNION queries, put ORDER BY only after the combined query, or run separate queries.
- Confirm tables and columns through discovery tools before querying them. table_schema is an
  INFORMATION_SCHEMA column, not a telemetry-table column.

Final diagnosis contract:
- affected_component is the workload or infrastructure component directly affected by the
  causal fault, not a downstream component that merely exhibits propagated symptoms. For a
  dependency failure, report the caller that lost access rather than replacing it with the
  unavailable dependency.
- causal_dependency is the downstream service, datastore, or external endpoint whose failure
  caused affected_component to fail. Return null when the evidence does not identify one.
- fault_type names the causal mechanism. Do not substitute a symptom such as high latency when
  evidence supports CPU saturation, memory pressure, disk I/O, packet loss, or socket exhaustion.
- fault_category is the canonical category for that same causal mechanism. Classify the positive
  diagnosis, not ruled-out alternatives mentioned in fault_type or explanation.
- onset_time is the earliest time at which telemetry supports a deviation from baseline. It is not
  the supplied incident detection time. Return null when the evidence cannot establish onset.
- confidence is epistemic confidence in the joint component-and-fault conclusion: around 0.5 means
  merely more likely than alternatives, around 0.7 means supported by multiple consistent facts,
  and above 0.9 requires direct, cross-signal evidence with plausible alternatives ruled out.
  Missing telemetry and ambiguous identity or relationships must reduce confidence.
- each evidence claim must state only what the cited query result directly supports.

Every final evidence item must copy an exact query_id returned by an investigation tool. Do not ask
the user questions. The incident prompt states a fixed investigation budget and each tool response
states the remainder. Reserve enough budget to synthesize the result; exhausting calls is not an
objective. Call submit_diagnosis once the available evidence supports the best answer."""


def _incident_prompt(case_input: CaseInput, max_tool_calls: int) -> str:
    start = datetime.fromtimestamp(case_input.time_start, UTC).isoformat()
    alert_time = datetime.fromtimestamp(case_input.alert_time, UTC).isoformat()
    end = datetime.fromtimestamp(case_input.time_end, UTC).isoformat()
    alert_text = case_input.alert_text or "Generic telemetry anomaly"
    return f"""Investigate this production incident.

Database: {case_input.database}
Telemetry window: {start} through {end}
Alert: {alert_text}
Alert fired at: {alert_time} (Unix {case_input.alert_time})
Investigation budget: at most {max_tool_calls} tool calls. Submit the best-supported diagnosis
before the budget reaches zero.

Find the affected component, causal dependency when present, fault type, and onset time. The
database is the only source of
incident evidence. Treat the alert as the observed symptom; its named entity is not necessarily the
root cause. INFORMATION_SCHEMA row queries must include
table_schema = '{case_input.database}' so results stay scoped to this incident."""
