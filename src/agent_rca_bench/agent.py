from __future__ import annotations

import hashlib
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
from urllib.parse import urlsplit

import anthropic
import openai
from pydantic import ValidationError

from agent_rca_bench.contracts import (
    AgentRun,
    AgentRunner,
    AgentUsage,
    ApiTransport,
    CaseInput,
    CausalScope,
    DatabaseLoad,
    Diagnosis,
    EvidenceClaimType,
    FaultCategory,
    MechanismCode,
    QueryResult,
    RejectedToolCall,
    ToolTrace,
    Visibility,
)
from agent_rca_bench.evidence import is_valid_evidence_trace
from agent_rca_bench.gemini_rate_limit import GEMINI_RATE_LIMITER
from agent_rca_bench.greptimedb.profile import TableProfiler
from agent_rca_bench.greptimedb.visibility import (
    DEFAULT_QUERY_MAX_ROWS,
    MAX_QUERY_MAX_ROWS,
    QueryGateway,
)
from agent_rca_bench.split_query import (
    METRIC_OPERATIONS,
    METRIC_OPERATIONS_WITHOUT_SEMANTICS,
    SplitQueryGateway,
    metrics_query_tool,
    split_investigation_tools,
)


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


ANTHROPIC_KEYCHAIN_SERVICE = "agent-rca-bench-anthropic"
DEEPSEEK_KEYCHAIN_SERVICE = "agent-rca-bench-deepseek"
OPENAI_KEYCHAIN_SERVICE = "agent-rca-bench-openai"
BIGMODEL_KEYCHAIN_SERVICE = "agent-rca-bench-bigmodel"
GEMINI_KEYCHAIN_SERVICE = "agent-rca-bench-gemini"
DASHSCOPE_KEYCHAIN_SERVICE = "agent-rca-bench-dashscope"
DASHSCOPE_BASE_URL_KEYCHAIN_SERVICE = "agent-rca-bench-dashscope-base-url"
DEEPSEEK_ANTHROPIC_BASE_URL = "https://api.deepseek.com/anthropic"
BIGMODEL_CHAT_COMPLETIONS_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
GEMINI_OPENAI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
GEMINI_MAX_RETRIES = 10
DASHSCOPE_BASE_URL_ENV = "DASHSCOPE_BASE_URL"

RESPONSES_TRANSPORTS = frozenset(
    {
        ApiTransport.OPENAI_RESPONSES,
        ApiTransport.DASHSCOPE_CN_BEIJING_RESPONSES,
    }
)
CHAT_COMPLETIONS_TRANSPORTS = frozenset(
    {
        ApiTransport.BIGMODEL_CHAT_COMPLETIONS,
        ApiTransport.GEMINI_OPENAI_CHAT_COMPLETIONS,
    }
)

TABLE_SEMANTICS_GUIDE = """
Semantic profile usage: signal_type says whether the table contains metrics, logs, traces, or
events; source and source_version identify the ingestion protocol; pipeline identifies the schema
or transformation that shaped the rows. semantic_options contains signal-specific facts such as
metric type, unit, temporality, original name, or trace conventions. metadata_quality says whether
metric metadata was declared by the source or inferred; it describes semantic-metadata certainty,
not telemetry quality. entity_declarations lists entities contributed by the table: entity_type is
the kind; id is the ordered list of identifying columns; id_qualifier names one optional column
folded into the first id component; scope lists namespace or environment columns exposed separately
from identity; descriptive lists non-identifying attributes; origin says declared or
convention-derived; and superseded_by records a preferred declaration. Null or missing semantic
fields mean unknown, not an opposite fact. Use the profile to map generic observability concepts to
physical columns; do not treat it as incident evidence by itself.
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
duration_count is nonzero. unmatched_count reports client spans without a paired server span;
it is not generally additive to request_count because an unmatched-only window uses that same
client population for both fields. duration_max is the longest request in the same population as
duration_sum and duration_count. attributes contains edge-specific facts. Deduplicate topology by
(src_type, src_id, dst_type, dst_id, rel_type, provenance) across windows. Missing edges can result
from no dependency, missing instrumentation, sampling, access filtering, or a narrow time window;
absence alone does not prove entities are unrelated.

Both graph tables join to each other and back to telemetry: semantic_entities.entity_id matches
semantic_relationships.src_id or dst_id for the same entity_type, and source_tables names the
telemetry tables to query next. Reach an entity two or more hops away by self-joining
semantic_relationships on a.dst_id = b.src_id, applying the observed_at window to every instance.
WITH RECURSIVE over these two tables is not supported and fails at plan or execution time.
""".strip()


SUBMIT_TOOL = {
    "name": "submit_diagnosis",
    "description": "Submit the final root-cause diagnosis and finish the investigation.",
    "input_schema": {
        "type": "object",
        "properties": {
            "causal_component": {
                "type": ["string", "null"],
                "description": (
                    "Exactly one entity where the causal mechanism is local: a component for "
                    "component scope, the node for infrastructure_node scope. Null for "
                    "dependency_edge scope."
                ),
            },
            "edge_source": {
                "type": ["string", "null"],
                "description": (
                    "The caller or upstream endpoint of the directed causal edge. Required for "
                    "dependency_edge scope and null for component scope."
                ),
            },
            "edge_destination": {
                "type": ["string", "null"],
                "description": (
                    "The callee or downstream endpoint of the directed causal edge. Required "
                    "for dependency_edge scope and null for component scope."
                ),
            },
            "impacted_component": {
                "type": ["string", "null"],
                "description": (
                    "One component that exhibits propagated impact, or null when not separately "
                    "identified. This does not define the root-cause locus."
                ),
            },
            "causal_scope": {
                "type": "string",
                "enum": [scope.value for scope in CausalScope],
                "description": (
                    "component when the mechanism is local to causal_component; "
                    "dependency_edge when it occurs on the directed path from edge_source to "
                    "edge_destination; infrastructure_node when it is local to the host or node "
                    "the affected workloads run on, in which case causal_component names that "
                    "node. Choose infrastructure_node only when the evidence separates the node "
                    "from the workloads it carries."
                ),
            },
            "causal_operation": {
                "type": ["string", "null"],
                "description": (
                    "The single operation or endpoint on the causal path when telemetry "
                    "identifies one, otherwise null."
                ),
            },
            "mechanism_code": {
                "type": "string",
                "enum": [mechanism.value for mechanism in MechanismCode],
                "description": (
                    "The case-independent structured causal mechanism. Use resource-specific "
                    "codes only when that resource is causal; call_path_delay for delay located "
                    "between a caller and callee; connection_failure for established connection "
                    "failure rather than its downstream 5xx symptom; dependency_contract_failure "
                    "for request/response contract changes; application_error, "
                    "configuration_error, data_semantics_error, or workload_restart for those "
                    "local mechanisms; "
                    "unknown only when evidence does not discriminate a listed mechanism."
                ),
            },
            "fault_type": {
                "type": "string",
                "description": (
                    "A concise free-text description consistent with mechanism_code. Put detail "
                    "and ruled-out alternatives in explanation."
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
                        "claim_types": {
                            "type": "array",
                            "description": (
                                "Structured claims directly supported by this cited result: "
                                "causal_locus identifies where the mechanism occurs; "
                                "fault_mechanism discriminates the mechanism; onset establishes "
                                "the observed change time; propagated_impact describes a "
                                "downstream symptom; exclusion rules out an alternative."
                            ),
                            "items": {
                                "type": "string",
                                "enum": [claim.value for claim in EvidenceClaimType],
                            },
                            "minItems": 1,
                            "uniqueItems": True,
                        },
                    },
                    "required": ["query_id", "claim", "claim_types"],
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
            "causal_scope",
            "causal_component",
            "edge_source",
            "edge_destination",
            "impacted_component",
            "causal_operation",
            "fault_category",
            "mechanism_code",
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
        gateway: QueryGateway | SplitQueryGateway,
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
        self.profiler = (
            None
            if isinstance(gateway, SplitQueryGateway)
            else TableProfiler(gateway.client, visibility)
        )
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

    def reject_unexecuted(self, tool_name: str, arguments: dict[str, object], error: str) -> None:
        self.tool_calls_requested += 1
        self.rejected_tool_calls.append(
            RejectedToolCall(
                tool_name=tool_name,
                input=arguments,
                error=error,
                reason_code="superseded_by_final_output",
            )
        )

    @property
    def remaining(self) -> int:
        return max(0, self.max_tool_calls - len(self.tool_calls))

    def _execute(self, tool_name: str, arguments: dict[str, object]) -> dict[str, object]:
        if isinstance(self.gateway, SplitQueryGateway):
            return self.gateway.execute_tool(tool_name, arguments)
        if tool_name == "execute_sql":
            requested_max_rows = arguments.get("max_rows")
            result = self.gateway.execute(
                str(arguments.get("query", "")),
                **({"max_rows": requested_max_rows} if requested_max_rows is not None else {}),
            )
            return result.model_dump(mode="json")
        if tool_name == "query_metrics":
            return self.gateway.execute_metrics(arguments)
        if tool_name == "describe_table":
            if self.profiler is None:
                raise AgentError("describe_table is unavailable in split_pillars")
            return self.profiler.describe(
                str(arguments.get("table", "")),
                include_samples=bool(arguments.get("include_samples", False)),
                sample_limit=int(arguments.get("sample_limit", 1)),
            )
        if tool_name == "search_table_semantics":
            if self.profiler is None:
                raise AgentError("search_table_semantics is unavailable in split_pillars")
            output = self.profiler.search(
                str(arguments.get("query", "")),
                signal_type=(
                    str(arguments["signal_type"]) if arguments.get("signal_type") else None
                ),
                limit=int(arguments.get("limit", 50)),
            )
            return _catalog_search_output(output, self.case_input.fault_taxonomy)
        result = self.gateway.execute(
            _semantic_graph_query(
                self.case_input,
                arguments,
                window=self.gateway.semantic_graph_window,
            ),
            max_rows=_semantic_graph_limit(arguments),
        )
        return _semantic_graph_output(result, arguments)

    def _load_snapshot(self) -> DatabaseLoad | None:
        snapshot = getattr(self.gateway.client, "query_load_snapshot", None)
        return snapshot() if callable(snapshot) else None

    def validate_diagnosis_citations(self, output: dict[str, object]) -> None:
        diagnosis = Diagnosis.model_validate(output)
        traces_by_query_id: dict[str, list[ToolTrace]] = {}
        for trace in self.tool_calls:
            if trace.query_id is not None:
                traces_by_query_id.setdefault(trace.query_id, []).append(trace)
        invalid: list[str] = []
        seen_query_ids: set[str] = set()
        for evidence in diagnosis.evidence:
            if evidence.query_id in seen_query_ids:
                invalid.append(evidence.query_id)
                continue
            seen_query_ids.add(evidence.query_id)
            matches = traces_by_query_id.get(evidence.query_id, [])
            if not evidence.claim.strip() or not is_valid_evidence_trace(matches):
                invalid.append(evidence.query_id)
        if invalid:
            query_ids = ", ".join(dict.fromkeys(invalid))
            raise ValueError(
                "evidence citations must reference one successful, non-truncated data query "
                f"result; replace invalid citations: {query_ids}"
            )


def _database_load_delta(
    before: DatabaseLoad | None,
    after: DatabaseLoad | None,
) -> DatabaseLoad | None:
    if before is None or after is None:
        return None
    query_count = after.query_count - before.query_count
    # A tool invocation executes synchronously in the API loop and under the
    # subscription broker lock. Run-level load measurement retains the actual peak.
    return DatabaseLoad(
        query_count=query_count,
        failed_query_count=after.failed_query_count - before.failed_query_count,
        # None where a returned row is not a database row, as in the split arm.
        # Subtracting it would raise inside the tool loop, and again inside the
        # handler that is meant to turn a tool failure into a tool error.
        rows_returned=(
            None
            if after.rows_returned is None or before.rows_returned is None
            else after.rows_returned - before.rows_returned
        ),
        query_elapsed_seconds=after.query_elapsed_seconds - before.query_elapsed_seconds,
        max_concurrency=1 if query_count else 0,
    )


def run_agent(
    gateway: QueryGateway,
    case_input: CaseInput,
    visibility: Visibility,
    *,
    model: str,
    api_transport: ApiTransport,
    reasoning_effort: str | None = None,
    max_tool_calls: int = 24,
    max_turns: int | None = None,
    max_output_tokens: int = 4096,
    semantic_coverage: dict[str, object] | None = None,
) -> AgentRun:
    tools = _investigation_tools(
        visibility,
        case_input.fault_taxonomy,
        semantic_coverage,
        promql=True,
    )
    result = run_structured_api_agent(
        gateway,
        case_input,
        visibility,
        model=model,
        api_transport=api_transport,
        reasoning_effort=reasoning_effort,
        system_prompt=_system_prompt(visibility),
        user_prompt=_incident_prompt(case_input, max_tool_calls, visibility),
        investigation_tools=tools,
        output_tool=_submit_tool(case_input.fault_taxonomy),
        validate_output=_validate_diagnosis_output,
        max_tool_calls=max_tool_calls,
        max_turns=max_turns,
        max_output_tokens=max_output_tokens,
        semantic_coverage=semantic_coverage,
        prompt_cache=True,
    )
    return _diagnosis_agent_run(
        result,
        visibility=visibility,
        model=model,
        api_transport=api_transport,
        reasoning_effort=reasoning_effort,
        max_output_tokens=max_output_tokens,
    )


def run_split_agent(
    gateway: SplitQueryGateway,
    case_input: CaseInput,
    *,
    model: str,
    api_transport: ApiTransport,
    reasoning_effort: str,
    max_tool_calls: int = 48,
    max_turns: int | None = None,
    max_output_tokens: int = 16384,
) -> AgentRun:
    result = run_structured_api_agent(
        gateway,
        case_input,
        Visibility.SPLIT_PILLARS,
        model=model,
        api_transport=api_transport,
        reasoning_effort=reasoning_effort,
        system_prompt=_system_prompt(Visibility.SPLIT_PILLARS),
        user_prompt=_incident_prompt(case_input, max_tool_calls, Visibility.SPLIT_PILLARS),
        investigation_tools=split_investigation_tools(),
        output_tool=_submit_tool(case_input.fault_taxonomy),
        validate_output=_validate_diagnosis_output,
        max_tool_calls=max_tool_calls,
        max_turns=max_turns,
        max_output_tokens=max_output_tokens,
        semantic_coverage=None,
        prompt_cache=True,
    )
    return _diagnosis_agent_run(
        result,
        visibility=Visibility.SPLIT_PILLARS,
        model=model,
        api_transport=api_transport,
        reasoning_effort=reasoning_effort,
        max_output_tokens=max_output_tokens,
    )


def _diagnosis_agent_run(
    result: StructuredAgentResult,
    *,
    visibility: Visibility,
    model: str,
    api_transport: ApiTransport,
    reasoning_effort: str | None,
    max_output_tokens: int,
) -> AgentRun:
    diagnosis = Diagnosis.model_validate(result.output) if result.output is not None else None
    error = result.error
    if error and error.startswith("agent did not submit final output within "):
        error = error.replace("final output", "a diagnosis", 1)
    return AgentRun(
        run_id=uuid.uuid4().hex,
        visibility=visibility,
        model=model,
        runner=AgentRunner.API,
        api_transport=api_transport,
        reasoning_effort=reasoning_effort,
        max_output_tokens=max_output_tokens,
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
    gateway: QueryGateway | SplitQueryGateway,
    case_input: CaseInput,
    visibility: Visibility,
    *,
    model: str,
    api_transport: ApiTransport,
    reasoning_effort: str | None = None,
    system_prompt: str,
    user_prompt: str,
    investigation_tools: list[dict[str, object]],
    output_tool: dict[str, object],
    validate_output: Callable[[object], dict[str, object]],
    max_tool_calls: int,
    max_turns: int | None,
    max_output_tokens: int = 4096,
    semantic_coverage: dict[str, object] | None = None,
    prompt_cache: bool = False,
) -> StructuredAgentResult:
    if max_turns is None:
        max_turns = max_tool_calls + 10
    if max_turns < max_tool_calls + 1:
        raise ValueError("max_turns must allow every tool call and a final output turn")
    if max_output_tokens < 1:
        raise ValueError("max_output_tokens must be positive")
    if api_transport in RESPONSES_TRANSPORTS:
        if reasoning_effort is None:
            raise ValueError("Responses API runs require an explicit reasoning effort")
        return _run_responses_structured_api_agent(
            gateway,
            case_input,
            visibility,
            model=model,
            api_transport=api_transport,
            reasoning_effort=reasoning_effort,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            investigation_tools=investigation_tools,
            output_tool=output_tool,
            validate_output=validate_output,
            max_tool_calls=max_tool_calls,
            max_turns=max_turns,
            max_output_tokens=max_output_tokens,
            semantic_coverage=semantic_coverage,
            prompt_cache=prompt_cache,
        )
    if api_transport in CHAT_COMPLETIONS_TRANSPORTS:
        if reasoning_effort is None:
            raise ValueError("reasoning Chat Completions runs require an explicit effort")
        return _run_chat_completions_structured_api_agent(
            gateway,
            case_input,
            visibility,
            model=model,
            api_transport=api_transport,
            reasoning_effort=reasoning_effort,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            investigation_tools=investigation_tools,
            output_tool=output_tool,
            validate_output=validate_output,
            max_tool_calls=max_tool_calls,
            max_turns=max_turns,
            max_output_tokens=max_output_tokens,
            semantic_coverage=semantic_coverage,
        )
    if reasoning_effort is not None and api_transport not in {
        ApiTransport.ANTHROPIC_MESSAGES,
        ApiTransport.ANTHROPIC_COMPATIBLE_MESSAGES,
    }:
        raise ValueError("reasoning effort is not supported by this API transport")
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
        client = _anthropic_client(api_transport)
    except Exception as error:
        return _structured_result(
            session,
            usage,
            responses,
            started,
            error=f"agent provider failed: {error}",
        )

    for _ in range(max_turns):
        request: dict[str, object] = {
            "model": model,
            "max_tokens": max_output_tokens,
            "system": system_prompt,
            "tools": [*investigation_tools, output_tool],
            "messages": messages,
        }
        if prompt_cache and api_transport is ApiTransport.ANTHROPIC_MESSAGES:
            request["cache_control"] = {"type": "ephemeral"}
        if reasoning_effort is not None:
            request["output_config"] = {"effort": reasoning_effort}
        try:
            response = client.messages.create(
                **request,
            )
        except Exception as error:
            return _structured_result(
                session,
                usage,
                responses,
                started,
                error=f"agent provider failed: {error}",
            )
        try:
            raw_response = response.model_dump(mode="json")
            responses.append(raw_response)
            usage.input_tokens += _provider_total_input_tokens(raw_response)
            usage.output_tokens += int(response.usage.output_tokens)
            usage.reasoning_tokens += _reasoning_tokens(raw_response)
            if raw_response.get("stop_reason") == "refusal":
                return _structured_result(
                    session,
                    usage,
                    responses,
                    started,
                    error="agent provider refused the request",
                )
            content = response.content
        except Exception as error:
            return _structured_result(
                session,
                usage,
                responses,
                started,
                error=f"invalid provider response: {error}",
            )
        messages.append({"role": "assistant", "content": content})

        tool_uses = [block for block in content if block.type == "tool_use"]
        output_block = next(
            (block for block in tool_uses if block.name == output_tool_name),
            None,
        )
        output_error: str | None = None
        if output_block is not None:
            try:
                output = validate_output(output_block.input)
                if output_tool_name == "submit_diagnosis":
                    session.validate_diagnosis_citations(output)
            except (ValidationError, ValueError, TypeError) as error:
                output_error = str(error)
            else:
                for tool_use in tool_uses:
                    if tool_use.name != output_tool_name:
                        session.reject_unexecuted(
                            tool_use.name,
                            dict(tool_use.input),
                            "not executed because the same response submitted valid final output",
                        )
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


def _run_chat_completions_structured_api_agent(
    gateway: QueryGateway,
    case_input: CaseInput,
    visibility: Visibility,
    *,
    model: str,
    api_transport: ApiTransport,
    reasoning_effort: str,
    system_prompt: str,
    user_prompt: str,
    investigation_tools: list[dict[str, object]],
    output_tool: dict[str, object],
    validate_output: Callable[[object], dict[str, object]],
    max_tool_calls: int,
    max_turns: int,
    max_output_tokens: int,
    semantic_coverage: dict[str, object] | None,
) -> StructuredAgentResult:
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
    messages: list[dict[str, object]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    tools = [_chat_completions_function_tool(tool) for tool in [*investigation_tools, output_tool]]
    try:
        client = _chat_completions_client(api_transport)
    except Exception as error:
        return _structured_result(
            session,
            usage,
            responses,
            started,
            error=f"agent provider failed: {error}",
        )

    for _ in range(max_turns):
        request = _chat_completions_request(
            api_transport,
            model=model,
            messages=messages,
            tools=tools,
            max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort,
        )
        try:
            response = client.chat.completions.create(**request)
        except Exception as error:
            return _structured_result(
                session,
                usage,
                responses,
                started,
                error=f"agent provider failed: {error}",
            )
        try:
            raw_response = response.model_dump(mode="json")
            responses.append(raw_response)
            usage.input_tokens += _provider_total_input_tokens(raw_response)
            infer_reasoning_from_total = (
                api_transport is ApiTransport.GEMINI_OPENAI_CHAT_COMPLETIONS
            )
            usage.output_tokens += _provider_output_tokens(
                raw_response,
                infer_reasoning_from_total=infer_reasoning_from_total,
            )
            usage.reasoning_tokens += _reasoning_tokens(
                raw_response,
                infer_reasoning_from_total=infer_reasoning_from_total,
            )
            if len(response.choices) != 1:
                raise AgentError("Chat Completions response must contain exactly one choice")
            choice = response.choices[0]
            if choice.finish_reason not in {"stop", "tool_calls"}:
                raise AgentError(f"Chat Completions finish_reason is {choice.finish_reason!r}")
            message = choice.message
            raw_message = message.model_dump(mode="json", exclude_none=True)
            messages.append(raw_message)
            tool_calls = list(message.tool_calls or [])
            parsed_calls = [_chat_completions_function_call(call) for call in tool_calls]
        except Exception as error:
            return _structured_result(
                session,
                usage,
                responses,
                started,
                error=f"invalid provider response: {error}",
            )

        output_call = next(
            (item for item in parsed_calls if item[1] == output_tool_name),
            None,
        )
        output_error: str | None = None
        if output_call is not None:
            _, _, output_arguments = output_call
            try:
                output = validate_output(output_arguments)
                if output_tool_name == "submit_diagnosis":
                    session.validate_diagnosis_citations(output)
            except (ValidationError, ValueError, TypeError) as error:
                output_error = str(error)
            else:
                for _, tool_name, arguments in parsed_calls:
                    if tool_name != output_tool_name:
                        session.reject_unexecuted(
                            tool_name,
                            arguments,
                            "not executed because the same response submitted valid final output",
                        )
                return _structured_result(
                    session,
                    usage,
                    responses,
                    started,
                    output=output,
                )

        if not parsed_calls:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"Continue the investigation or call {output_tool_name}. "
                        f"You have {session.remaining} investigation tool calls remaining."
                    ),
                }
            )
            continue

        for call_id, tool_name, arguments in parsed_calls:
            if tool_name == output_tool_name:
                invocation = ToolInvocation(
                    content=f"invalid final output: {output_error}",
                    is_error=True,
                    remaining=session.remaining,
                )
            else:
                invocation = session.invoke(tool_name, arguments)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": _responses_tool_output(invocation),
                }
            )
        messages.append(
            {
                "role": "user",
                "content": (
                    f"Investigation budget: {session.remaining} tool calls remain. "
                    f"Call {output_tool_name} before the budget reaches zero."
                ),
            }
        )

    return _structured_result(
        session,
        usage,
        responses,
        started,
        error=f"agent did not submit final output within {max_turns} turns",
    )


def _run_responses_structured_api_agent(
    gateway: QueryGateway,
    case_input: CaseInput,
    visibility: Visibility,
    *,
    model: str,
    api_transport: ApiTransport,
    reasoning_effort: str,
    system_prompt: str,
    user_prompt: str,
    investigation_tools: list[dict[str, object]],
    output_tool: dict[str, object],
    validate_output: Callable[[object], dict[str, object]],
    max_tool_calls: int,
    max_turns: int,
    max_output_tokens: int,
    semantic_coverage: dict[str, object] | None,
    prompt_cache: bool,
) -> StructuredAgentResult:
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
    input_items: list[dict[str, object]] = [{"role": "user", "content": user_prompt}]
    tools = [_responses_function_tool(tool) for tool in [*investigation_tools, output_tool]]
    try:
        client = _responses_client(api_transport)
    except Exception as error:
        return _structured_result(
            session,
            usage,
            responses,
            started,
            error=f"agent provider failed: {error}",
        )

    for _ in range(max_turns):
        request = _responses_request(
            api_transport,
            visibility,
            model=model,
            system_prompt=system_prompt,
            input_items=input_items,
            tools=tools,
            max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort,
            prompt_cache=prompt_cache,
        )
        try:
            response = client.responses.create(**request)
        except Exception as error:
            return _structured_result(
                session,
                usage,
                responses,
                started,
                error=f"agent provider failed: {error}",
            )
        try:
            raw_response = response.model_dump(mode="json")
            responses.append(raw_response)
            usage.input_tokens += _provider_total_input_tokens(raw_response)
            usage.output_tokens += int(response.usage.output_tokens)
            usage.reasoning_tokens += _reasoning_tokens(raw_response)
            _validate_responses_status(raw_response)
            output_items = list(response.output)
            raw_output_items = raw_response.get("output")
            if not isinstance(raw_output_items, list):
                raise AgentError("Responses API response has no output item list")
            input_items.extend(_responses_continuation_items(raw_output_items))
        except Exception as error:
            return _structured_result(
                session,
                usage,
                responses,
                started,
                error=f"invalid provider response: {error}",
            )

        tool_calls = [
            item for item in output_items if getattr(item, "type", None) == "function_call"
        ]
        try:
            parsed_calls = [_responses_function_call(item) for item in tool_calls]
        except AgentError as error:
            return _structured_result(
                session,
                usage,
                responses,
                started,
                error=f"invalid provider response: {error}",
            )
        output_call = next(
            (item for item in parsed_calls if item[1] == output_tool_name),
            None,
        )
        output_error: str | None = None
        if output_call is not None:
            _, _, output_arguments = output_call
            try:
                output = validate_output(output_arguments)
                if output_tool_name == "submit_diagnosis":
                    session.validate_diagnosis_citations(output)
            except (ValidationError, ValueError, TypeError) as error:
                output_error = str(error)
            else:
                for _, tool_name, arguments in parsed_calls:
                    if tool_name != output_tool_name:
                        session.reject_unexecuted(
                            tool_name,
                            arguments,
                            "not executed because the same response submitted valid final output",
                        )
                return _structured_result(
                    session,
                    usage,
                    responses,
                    started,
                    output=output,
                )

        if not tool_calls:
            input_items.append(
                {
                    "role": "user",
                    "content": (
                        f"Continue the investigation or call {output_tool_name}. "
                        f"You have {session.remaining} investigation tool calls remaining."
                    ),
                }
            )
            continue

        for call_id, tool_name, arguments in parsed_calls:
            if tool_name == output_tool_name:
                invocation = ToolInvocation(
                    content=f"invalid final output: {output_error}",
                    is_error=True,
                    remaining=session.remaining,
                )
            else:
                invocation = session.invoke(tool_name, arguments)
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": _responses_tool_output(invocation),
                }
            )
        input_items.append(
            {
                "role": "user",
                "content": (
                    f"Investigation budget: {session.remaining} tool calls remain. "
                    f"Call {output_tool_name} before the budget reaches zero."
                ),
            }
        )

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


def _anthropic_client(api_transport: ApiTransport) -> anthropic.Anthropic:
    if api_transport is ApiTransport.ANTHROPIC_COMPATIBLE_MESSAGES:
        api_key = _api_credential("DEEPSEEK_API_KEY", DEEPSEEK_KEYCHAIN_SERVICE)
        return anthropic.Anthropic(
            api_key=api_key,
            base_url=DEEPSEEK_ANTHROPIC_BASE_URL,
            http_client=anthropic.DefaultHttpxClient(trust_env=False),
        )
    if api_transport is not ApiTransport.ANTHROPIC_MESSAGES:
        raise AgentError(f"unsupported Anthropic transport: {api_transport.value}")
    api_key = _api_credential("ANTHROPIC_API_KEY", ANTHROPIC_KEYCHAIN_SERVICE)
    return anthropic.Anthropic(api_key=api_key)


def _chat_completions_client(api_transport: ApiTransport) -> openai.OpenAI:
    client_options: dict[str, object] = {}
    http_options: dict[str, object] = {}
    if api_transport is ApiTransport.BIGMODEL_CHAT_COMPLETIONS:
        api_key = _api_credential("BIGMODEL_API_KEY", BIGMODEL_KEYCHAIN_SERVICE)
        base_url = BIGMODEL_CHAT_COMPLETIONS_BASE_URL
        trust_env = False
    elif api_transport is ApiTransport.GEMINI_OPENAI_CHAT_COMPLETIONS:
        api_key = _api_credential("GEMINI_API_KEY", GEMINI_KEYCHAIN_SERVICE)
        base_url = GEMINI_OPENAI_BASE_URL
        trust_env = True
        client_options["max_retries"] = GEMINI_MAX_RETRIES
        http_options["event_hooks"] = {
            "request": [GEMINI_RATE_LIMITER.before_request],
            "response": [GEMINI_RATE_LIMITER.after_response],
        }
    else:
        raise AgentError(f"unsupported Chat Completions transport: {api_transport.value}")
    return openai.OpenAI(
        api_key=api_key,
        base_url=base_url,
        http_client=openai.DefaultHttpxClient(trust_env=trust_env, **http_options),
        **client_options,
    )


def _chat_completions_function_tool(tool: dict[str, object]) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["input_schema"],
        },
    }


def _chat_completions_request(
    api_transport: ApiTransport,
    *,
    model: str,
    messages: list[dict[str, object]],
    tools: list[dict[str, object]],
    max_output_tokens: int,
    reasoning_effort: str,
) -> dict[str, object]:
    request: dict[str, object] = {
        "model": model,
        "messages": deepcopy(messages),
        "tools": tools,
        "max_tokens": max_output_tokens,
    }
    if api_transport is ApiTransport.BIGMODEL_CHAT_COMPLETIONS:
        request["extra_body"] = {
            "thinking": {"type": "enabled"},
            "reasoning_effort": reasoning_effort,
        }
    elif api_transport is ApiTransport.GEMINI_OPENAI_CHAT_COMPLETIONS:
        request["reasoning_effort"] = reasoning_effort
    else:
        raise AgentError(f"unsupported Chat Completions transport: {api_transport.value}")
    return request


def _chat_completions_function_call(
    item: object,
) -> tuple[str, str, dict[str, object]]:
    call_id = getattr(item, "id", None)
    function = getattr(item, "function", None)
    name = getattr(function, "name", None)
    arguments = getattr(function, "arguments", None)
    if not isinstance(call_id, str) or not call_id:
        raise AgentError("Chat Completions function call has no id")
    if not isinstance(name, str) or not name:
        raise AgentError("Chat Completions function call has no name")
    if not isinstance(arguments, str):
        raise AgentError("Chat Completions function call arguments are not JSON text")
    try:
        decoded = json.loads(arguments)
    except json.JSONDecodeError as error:
        raise AgentError(
            f"Chat Completions function call arguments are invalid JSON: {error}"
        ) from error
    if not isinstance(decoded, dict):
        raise AgentError("Chat Completions function call arguments must decode to an object")
    return call_id, name, decoded


def _responses_client(api_transport: ApiTransport) -> openai.OpenAI:
    if api_transport is ApiTransport.OPENAI_RESPONSES:
        api_key = _api_credential("OPENAI_API_KEY", OPENAI_KEYCHAIN_SERVICE)
        return openai.OpenAI(api_key=api_key)
    if api_transport is ApiTransport.DASHSCOPE_CN_BEIJING_RESPONSES:
        api_key = _api_credential("DASHSCOPE_API_KEY", DASHSCOPE_KEYCHAIN_SERVICE)
        return openai.OpenAI(
            api_key=api_key,
            base_url=_dashscope_base_url(),
            http_client=openai.DefaultHttpxClient(trust_env=False),
        )
    raise AgentError(f"unsupported Responses transport: {api_transport.value}")


def _dashscope_base_url() -> str:
    base_url = _environment_or_keychain(
        DASHSCOPE_BASE_URL_ENV,
        DASHSCOPE_BASE_URL_KEYCHAIN_SERVICE,
    ).rstrip("/")
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or not parsed.hostname.endswith(".cn-beijing.maas.aliyuncs.com")
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/compatible-mode/v1"
        or parsed.query
        or parsed.fragment
    ):
        raise AgentError(
            f"{DASHSCOPE_BASE_URL_ENV} must be a China (Beijing) workspace Responses "
            "base URL ending in .cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
        )
    return base_url


def _responses_function_tool(tool: dict[str, object]) -> dict[str, object]:
    return {
        "type": "function",
        "name": tool["name"],
        "description": tool["description"],
        "parameters": tool["input_schema"],
        "strict": False,
    }


def _responses_request(
    api_transport: ApiTransport,
    visibility: Visibility,
    *,
    model: str,
    system_prompt: str,
    input_items: list[dict[str, object]],
    tools: list[dict[str, object]],
    max_output_tokens: int,
    reasoning_effort: str,
    prompt_cache: bool,
) -> dict[str, object]:
    request: dict[str, object] = {
        "model": model,
        "instructions": system_prompt,
        "input": deepcopy(input_items),
        "tools": tools,
        "max_output_tokens": max_output_tokens,
        "reasoning": {"effort": reasoning_effort},
    }
    if api_transport is ApiTransport.OPENAI_RESPONSES:
        request.update(
            {
                "parallel_tool_calls": True,
                "include": ["reasoning.encrypted_content"],
                "store": False,
            }
        )
        if prompt_cache:
            request["prompt_cache_key"] = _openai_prompt_cache_key(
                visibility,
                system_prompt,
                tools,
            )
            request["prompt_cache_options"] = {"mode": "implicit", "ttl": "30m"}
    elif api_transport is ApiTransport.DASHSCOPE_CN_BEIJING_RESPONSES:
        request.update({"parallel_tool_calls": False, "store": False})
        if prompt_cache:
            request["extra_headers"] = {"x-dashscope-session-cache": "enable"}
    else:
        raise AgentError(f"unsupported Responses transport: {api_transport.value}")
    return request


def _responses_continuation_items(
    output_items: list[object],
) -> list[dict[str, object]]:
    continuation_items: list[dict[str, object]] = []
    for output_item in output_items:
        if not isinstance(output_item, dict):
            raise AgentError("Responses API output item is not an object")
        item = deepcopy(output_item)
        if item.get("type") in {"reasoning", "function_call"}:
            item.pop("status", None)
            item = {key: value for key, value in item.items() if value is not None}
        continuation_items.append(item)
    return continuation_items


def _openai_prompt_cache_key(
    visibility: Visibility,
    system_prompt: str,
    tools: list[dict[str, object]],
) -> str:
    surface = json.dumps(
        {"system_prompt": system_prompt, "tools": tools},
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(surface.encode()).hexdigest()[:20]
    return f"semantic-rca-{visibility.value}-{digest}"


def _responses_function_call(item: object) -> tuple[str, str, dict[str, object]]:
    call_id = getattr(item, "call_id", None)
    name = getattr(item, "name", None)
    arguments = getattr(item, "arguments", None)
    if not isinstance(call_id, str) or not call_id:
        raise AgentError("Responses API function call has no call_id")
    if not isinstance(name, str) or not name:
        raise AgentError("Responses API function call has no name")
    if not isinstance(arguments, str):
        raise AgentError("Responses API function call arguments are not JSON text")
    try:
        decoded = json.loads(arguments)
    except json.JSONDecodeError as error:
        raise AgentError(
            f"Responses API function call arguments are invalid JSON: {error}"
        ) from error
    if not isinstance(decoded, dict):
        raise AgentError("Responses API function call arguments must decode to an object")
    return call_id, name, decoded


def _responses_tool_output(invocation: ToolInvocation) -> str:
    if not invocation.is_error:
        return invocation.content
    return json.dumps(
        {
            "is_error": True,
            "error": invocation.content,
            "remaining_tool_calls": invocation.remaining,
        },
        separators=(",", ":"),
    )


def _validate_responses_status(response: dict[str, object]) -> None:
    status = response.get("status")
    if status == "completed":
        return
    details = response.get("incomplete_details")
    reason = details.get("reason") if isinstance(details, dict) else None
    error = response.get("error")
    message = error.get("message") if isinstance(error, dict) else None
    detail = reason or message or "no provider detail"
    raise AgentError(f"Responses API status is {status!r}: {detail}")


def _reasoning_tokens(
    response: dict[str, object],
    *,
    infer_reasoning_from_total: bool = False,
) -> int:
    raw_usage = response.get("usage")
    if not isinstance(raw_usage, dict):
        raise AgentError("provider response has no usage object")
    output_tokens = int(raw_usage.get("output_tokens", raw_usage.get("completion_tokens", 0)) or 0)
    total_output_tokens = output_tokens
    details = raw_usage.get("output_tokens_details", raw_usage.get("completion_tokens_details"))
    reasoning_tokens = 0
    if infer_reasoning_from_total:
        total_output_tokens = _output_tokens_including_unreported_reasoning(raw_usage)
    if isinstance(details, dict) and any(
        details.get(key) is not None for key in ("reasoning_tokens", "thinking_tokens")
    ):
        raw_reasoning = (
            details["reasoning_tokens"]
            if details.get("reasoning_tokens") is not None
            else details.get("thinking_tokens", 0)
        )
        reasoning_tokens = int(raw_reasoning or 0)
    elif infer_reasoning_from_total:
        reasoning_tokens = total_output_tokens - output_tokens
    if reasoning_tokens < 0 or reasoning_tokens > total_output_tokens:
        raise AgentError("provider reasoning token breakdown exceeds output_tokens")
    return reasoning_tokens


def _provider_total_input_tokens(response: dict[str, object]) -> int:
    raw_usage = response.get("usage")
    if not isinstance(raw_usage, dict):
        raise AgentError("provider response has no usage object")
    if "prompt_cache_hit_tokens" in raw_usage and "prompt_cache_miss_tokens" in raw_usage:
        return int(raw_usage["prompt_cache_hit_tokens"] or 0) + int(
            raw_usage["prompt_cache_miss_tokens"] or 0
        )
    if "cache_read_input_tokens" in raw_usage and "cache_creation_input_tokens" in raw_usage:
        return sum(
            int(raw_usage.get(field, 0) or 0)
            for field in (
                "input_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
            )
        )
    input_tokens = int(raw_usage.get("input_tokens", raw_usage.get("prompt_tokens", 0)) or 0)
    details = raw_usage.get("input_tokens_details", raw_usage.get("prompt_tokens_details"))
    if isinstance(details, dict):
        cached = int(details.get("cached_tokens", 0) or 0)
        cache_write = int(
            details.get("cache_write_tokens", 0)
            or details.get("cache_creation_input_tokens", 0)
            or raw_usage.get("cache_creation_input_tokens", 0)
            or 0
        )
        if cached < 0 or cache_write < 0 or cached + cache_write > input_tokens:
            raise AgentError("provider cache token breakdown exceeds input_tokens")
    return input_tokens


def _provider_output_tokens(
    response: dict[str, object],
    *,
    infer_reasoning_from_total: bool = False,
) -> int:
    raw_usage = response.get("usage")
    if not isinstance(raw_usage, dict):
        raise AgentError("provider response has no usage object")
    if infer_reasoning_from_total:
        return _output_tokens_including_unreported_reasoning(raw_usage)
    return int(raw_usage.get("output_tokens", raw_usage.get("completion_tokens", 0)) or 0)


def _output_tokens_including_unreported_reasoning(raw_usage: dict[str, object]) -> int:
    input_tokens = int(raw_usage.get("input_tokens", raw_usage.get("prompt_tokens", 0)) or 0)
    visible_output = int(raw_usage.get("output_tokens", raw_usage.get("completion_tokens", 0)) or 0)
    total_tokens = int(raw_usage.get("total_tokens", 0) or 0)
    output_tokens = total_tokens - input_tokens
    if total_tokens <= 0 or output_tokens < visible_output:
        raise AgentError("provider total_tokens cannot account for output and reasoning tokens")
    return output_tokens


def _api_credential(environment_variable: str, keychain_service: str) -> str:
    api_key = _environment_or_keychain(environment_variable, keychain_service)
    if not api_key:
        raise AgentError(
            f"credential not found in {environment_variable} or macOS Keychain "
            f"service {keychain_service}"
        )
    return api_key


def _environment_or_keychain(environment_variable: str, keychain_service: str) -> str:
    value = os.environ.get(environment_variable, "")
    if not value and sys.platform == "darwin":
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", keychain_service, "-w"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            value = result.stdout.strip()
    return value


def _agent_tools(
    visibility: Visibility,
    fault_taxonomy: list[str],
    semantic_coverage: dict[str, object] | None,
    *,
    promql: bool = False,
) -> list[dict[str, object]]:
    tools = [
        _execute_sql_tool(visibility, semantic_coverage),
        _describe_table_tool(visibility),
    ]
    if promql:
        # GreptimeDB answers PromQL natively, so both GreptimeDB arms of the
        # end-to-end study expose it: leaving it out would present the product
        # below its actual surface. The schema-discovery and graph micro
        # benchmarks keep their frozen two-tool surface.
        tools.append(
            metrics_query_tool(
                native_stack=False,
                operations=(
                    METRIC_OPERATIONS_WITHOUT_SEMANTICS
                    if visibility is Visibility.RAW
                    else METRIC_OPERATIONS
                ),
            )
        )
    if visibility is Visibility.SEMANTIC_GRAPH:
        tools.append(_search_table_semantics_tool())
    if visibility is Visibility.SEMANTIC_GRAPH and _graph_status(semantic_coverage) != "empty":
        tools.append(_semantic_graph_tool(semantic_coverage))
    tools.append(_submit_tool(fault_taxonomy))
    return tools


def _investigation_tools(
    visibility: Visibility,
    fault_taxonomy: list[str],
    semantic_coverage: dict[str, object] | None,
    *,
    promql: bool = False,
) -> list[dict[str, object]]:
    return [
        tool
        for tool in _agent_tools(visibility, fault_taxonomy, semantic_coverage, promql=promql)
        if tool["name"] != "submit_diagnosis"
    ]


def _execute_sql_tool(
    visibility: Visibility,
    semantic_coverage: dict[str, object] | None = None,
) -> dict[str, object]:
    description = (
        "Execute one read-only GreptimeDB SQL statement in the incident database. "
        "Use MySQL dialect and INFORMATION_SCHEMA for ordinary schema discovery. "
        f"Results default to at most {DEFAULT_QUERY_MAX_ROWS} rows; max_rows may explicitly "
        f"raise this to {MAX_QUERY_MAX_ROWS}. Prefer aggregation or narrower filters. A truncated "
        "result is incomplete and cannot be cited as final evidence."
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
                },
                "max_rows": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_QUERY_MAX_ROWS,
                    "default": DEFAULT_QUERY_MAX_ROWS,
                    "description": (
                        "Maximum rows returned for this query. Raise it only when a complete "
                        "result cannot be obtained with aggregation or narrower filters."
                    ),
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    }


def _describe_table_tool(visibility: Visibility) -> dict[str, object]:
    description = "Get a table profile containing column schema and optional sample rows."
    if visibility is Visibility.SEMANTIC_GRAPH:
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
        "Omit start_time and end_time to use the complete audited Graph window, or supply both "
        "to query a narrower half-open observed_at range; both are RFC3339 UTC timestamps and "
        "observed_at is the Graph observation bucket timestamp. Set bucket=window to return one "
        "row per observation window, which is how a relationship or an entity that appears, "
        "changes, or disappears during the incident becomes visible; without it the requested "
        "range is reduced to one row per entity or edge. The tool supplies the greptime_private "
        "schema. Relationship rows are deduplicated per observation window before RED fields are "
        "summed. Entity identity "
        "fields are entity_type, entity_id, entity_id_attrs, and scope; descriptive holds "
        "non-identifying attributes and source_tables names the telemetry tables that witnessed "
        "the entity, which is where to query its signals next. Relationship direction "
        "is src_type/src_id to dst_type/dst_id; rel_type and provenance describe the edge; "
        "confidence is derivation certainty; request_count, error_count, duration_sum, and "
        "duration_count are windowed observations. unmatched_count reports unpaired client spans "
        "and is not generally additive to request_count; duration_max is the longest request in "
        "the duration population. Missing rows do not prove no relationship. "
        "This tool returns one hop; reach further entities with execute_sql by self-joining "
        "semantic_relationships on a.dst_id = b.src_id. "
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
                "start_time": {
                    "type": "string",
                    "description": (
                        "Inclusive observed_at lower bound as an RFC3339 UTC timestamp, for "
                        "example 2026-05-02T00:55:19+00:00."
                    ),
                },
                "end_time": {
                    "type": "string",
                    "description": (
                        "Exclusive observed_at upper bound as an RFC3339 UTC timestamp, for "
                        "example 2026-05-02T01:05:19+00:00."
                    ),
                },
                "bucket": {
                    "type": "string",
                    "enum": ["window"],
                    "description": (
                        "Return each deduplicated observation window instead of one aggregate "
                        "row per entity or edge over the requested range."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_QUERY_MAX_ROWS,
                    "default": 100,
                },
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


def _semantic_graph_query(
    case_input: CaseInput,
    arguments: dict[str, object],
    *,
    window: tuple[int, int] | None = None,
) -> str:
    view = str(arguments.get("view") or "")
    if view not in {"entities", "relationships"}:
        raise AgentError("semantic graph view must be entities or relationships")
    bucket = arguments.get("bucket")
    if bucket not in (None, "window"):
        raise AgentError("semantic graph bucket must be window when supplied")
    allowed_window = window or (case_input.time_start, case_input.time_end)
    window_start, window_end = _semantic_graph_query_window(arguments, allowed_window)
    start = datetime.fromtimestamp(window_start, UTC).strftime("%Y-%m-%d %H:%M:%S")
    end = datetime.fromtimestamp(window_end, UTC).strftime("%Y-%m-%d %H:%M:%S")
    # One row beyond the reported limit, so the gateway can distinguish a complete
    # result from one the LIMIT clipped. Citation eligibility depends on that flag.
    limit = _semantic_graph_limit(arguments) + 1
    predicates = [f"observed_at >= '{start}'", f"observed_at < '{end}'"]

    if view == "entities":
        _append_graph_filters(predicates, arguments, ("entity_type", "entity_id"))
        where = " AND ".join(predicates)
        # descriptive and source_tables are grouped rather than aggregated: MAX over a
        # JSON column would silently return one window's value for an entity whose
        # attributes or witnessing tables changed mid-incident.
        identity = "entity_type, entity_id, entity_id_attrs, scope, descriptive, source_tables"
        if bucket == "window":
            return f"""
                SELECT window_start, window_end, {identity},
                       MAX(fresh_until) AS fresh_until
                FROM greptime_private.semantic_entities
                WHERE {where}
                GROUP BY window_start, window_end, {identity}
                ORDER BY window_start, window_end, entity_type, entity_id
                LIMIT {limit}
            """
        return f"""
            SELECT {identity},
                   MIN(observed_at) AS first_observed_at,
                   MAX(observed_at) AS latest_observed_at,
                   MAX(fresh_until) AS fresh_until
            FROM greptime_private.semantic_entities
            WHERE {where}
            GROUP BY {identity}
            ORDER BY entity_type, entity_id
            LIMIT {limit}
        """

    _append_graph_filters(
        predicates,
        arguments,
        ("rel_type", "src_type", "src_id", "dst_type", "dst_id", "provenance"),
    )
    where = " AND ".join(predicates)
    if bucket == "window":
        return f"""
            {_relationship_window_query(where, include_attributes=True)}
            ORDER BY window_start, window_end, src_type, src_id, dst_type, dst_id,
                     rel_type, provenance
            LIMIT {limit}
        """
    return f"""
        SELECT src_type, src_id, dst_type, dst_id, rel_type, provenance,
               MAX(confidence) AS confidence,
               SUM(request_count) AS request_count,
               SUM(unmatched_count) AS unmatched_count,
               SUM(error_count) AS error_count,
               SUM(duration_sum) AS duration_sum,
               SUM(duration_count) AS duration_count,
               MAX(duration_max) AS duration_max
        FROM (
            {_relationship_window_query(where, include_attributes=False)}
        ) distinct_windows
        GROUP BY src_type, src_id, dst_type, dst_id, rel_type, provenance
        ORDER BY src_type, src_id, dst_type, dst_id, rel_type, provenance
        LIMIT {limit}
    """


def _relationship_window_query(where: str, *, include_attributes: bool) -> str:
    # attributes stays out of the summed shape: an edge whose attributes changed
    # mid-incident would split into several groups and break the RED totals.
    attributes = ", attributes" if include_attributes else ""
    return f"""
        SELECT window_start, window_end, src_type, src_id, dst_type, dst_id,
               rel_type, provenance{attributes},
               MAX(confidence) AS confidence,
               MAX(request_count) AS request_count,
               MAX(unmatched_count) AS unmatched_count,
               MAX(error_count) AS error_count,
               MAX(duration_sum) AS duration_sum,
               MAX(duration_count) AS duration_count,
               MAX(duration_max) AS duration_max
        FROM greptime_private.semantic_relationships
        WHERE {where}
        GROUP BY window_start, window_end, src_type, src_id, dst_type, dst_id,
                 rel_type, provenance{attributes}
    """


def _semantic_graph_limit(arguments: dict[str, object]) -> int:
    requested = arguments.get("limit", 100)
    if isinstance(requested, bool) or not isinstance(requested, int):
        raise AgentError("semantic graph limit must be an integer")
    return max(1, min(requested, MAX_QUERY_MAX_ROWS))


def _semantic_graph_query_window(
    arguments: dict[str, object],
    allowed_window: tuple[int, int],
) -> tuple[int, int]:
    requested_start = arguments.get("start_time")
    requested_end = arguments.get("end_time")
    if (requested_start is None) != (requested_end is None):
        raise AgentError("semantic graph start_time and end_time must be supplied together")
    if requested_start is None:
        return allowed_window
    start = _semantic_graph_bound(requested_start, "start_time")
    end = _semantic_graph_bound(requested_end, "end_time")
    if start >= end:
        raise AgentError("semantic graph time range must be non-empty")
    if start < allowed_window[0] or end > allowed_window[1]:
        raise AgentError("semantic graph time range must stay within the audited incident window")
    return start, end


def _semantic_graph_bound(value: object, field: str) -> int:
    if not isinstance(value, str):
        raise AgentError(f"semantic graph {field} must be an RFC3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise AgentError(f"semantic graph {field} must be an RFC3339 UTC timestamp") from error
    if parsed.tzinfo is None:
        raise AgentError(f"semantic graph {field} must declare a UTC offset")
    return int(parsed.timestamp())


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


def _validate_diagnosis_output(value: object) -> dict[str, object]:
    diagnosis = Diagnosis.model_validate(value)
    if any(not item.claim_types for item in diagnosis.evidence):
        raise ValueError("every evidence item must declare at least one claim type")
    return diagnosis.model_dump(mode="json")


_INVESTIGATION_METHOD = """You are the on-call SRE investigating a production incident from
telemetry. Determine the single most likely root-cause locus and causal fault type. Do not report a
surface observation as the cause unless the evidence discriminates that causal mechanism from its
alternatives. Work from query evidence, not naming alone. Compare baseline and anomalous periods
when the telemetry window
contains a known baseline, and correlate metrics, logs, and traces when present. Infer the change
point from telemetry rather than the alert time. Discover what the store holds before relying on
names, and use capabilities explicitly exposed by the tools when available.

Before broad health checks, establish the change point and the failing request or operation. Treat
observed signals as evidence, not automatically as causes. The same observation may arise from
different mechanisms, and an observed condition may be causal or propagated. Form two or three
hypotheses that differ in causal scope or mechanism, then use the next query to distinguish them.
An empty discovery result does not by itself prove that no relevant signal exists; try another
concrete concept while the hypothesis remains plausible. Interpret an empty data query only after
checking its time range, filters, and coverage.
If the available evidence does not discriminate, report the best-supported hypothesis, state the
ambiguity in explanation, and lower confidence. When traces are available, compare the same
operation before and after onset, including its parent-child path, service identity, span role, and
relevant attributes. A recorded successful request or span does not by itself prove that the
intended operation ran or returned semantically correct data. Run broad resource health checks only
when an active hypothesis makes resource pressure plausible."""

_GREPTIMEDB_NOTES = """Telemetry is stored in GreptimeDB, where every signal is queryable from one
surface. execute_sql can query the metric, log, and trace tables in the named incident database.
Stay inside that database.

GreptimeDB SQL notes:
- Compare Timestamp columns with timestamp string literals, not integer Unix epochs.
- Use date_bin for time buckets only when the bucket boundaries preserve the incident's required
  baseline/anomalous split; otherwise keep the exact timestamp boundary.
- Quote identifiers containing dots with double quotes.
- In UNION queries, put ORDER BY only after the combined query, or run separate queries.
- Confirm tables and columns through discovery tools before querying them. table_schema is an
  INFORMATION_SCHEMA column, not a telemetry-table column.
- query_metrics runs PromQL against the same metric tables, addressing each table by its name as
  the metric name and its tag columns as labels.
- Truncated query results are incomplete and cannot be cited. Use aggregation, narrower filters, or
  an explicit execute_sql max_rows up to 1000 to obtain a complete result before citing it."""

_SPLIT_PILLARS_NOTES = """Telemetry is split across three stores, each with its own query language:
Prometheus for metrics through query_metrics, Loki for logs through query_logs, and Tempo for
traces through query_traces. No store can read another's data.

Native query notes:
- Time parameters take Unix seconds or RFC 3339; range queries also need an explicit step.
- Select series by label matchers inside braces. Label names hold no dots; a source attribute such
  as k8s.pod.name is addressed as k8s_pod_name.
- Loki range queries include the start and exclude the end.
- Confirm what exists through the discovery operations (labels, label_values, series, tags,
  tag_values) before relying on names.
- Truncated query results are incomplete and cannot be cited. Use a narrower filter, an
  aggregating query, or an explicit max_items up to 1000 to obtain a complete result before citing
  it."""

_DIAGNOSIS_CONTRACT = """Final diagnosis contract:
- causal_scope and its locus fields identify where the mechanism exists, independently of where
  symptoms propagate. For component scope, set exactly one causal_component and leave both edge
  fields null. For infrastructure_node scope, name the node in causal_component and leave both edge
  fields null; use it when the mechanism is local to the node rather than to any workload it
  carries. For dependency_edge scope, set exactly one directed edge_source and edge_destination
  and leave causal_component null. Do not submit a list or alternation in any locus field.
- impacted_component optionally names one component that exhibits propagated impact. It is not a
  substitute for the component or edge where the causal mechanism exists. Put unresolved candidates
  in alternative_candidates and lower confidence.
- causal_operation names one operation at the causal locus when known.
- mechanism_code is the case-independent structured causal mechanism. fault_type is a concise
  free-text description of the same mechanism; put qualifications and alternatives in explanation.
- fault_category is the canonical category for that same causal mechanism. Classify the positive
  diagnosis, not ruled-out alternatives mentioned in fault_type or explanation.
- onset_time is the earliest time at which telemetry supports a deviation from baseline. It is not
  the supplied incident detection time. Return null when the evidence cannot establish onset.
- confidence is epistemic confidence in the joint component-and-fault conclusion: around 0.5 means
  merely more likely than alternatives, around 0.7 means supported by multiple consistent facts,
  and above 0.9 requires direct, cross-signal evidence with plausible alternatives ruled out.
  Missing telemetry and ambiguous identity or relationships must reduce confidence.
- each evidence claim must state only what the cited query result directly supports and declare
  which structured claims it supports. Use causal_locus for evidence that identifies the component
  or directed edge where the mechanism occurs; use propagated_impact only for downstream symptoms.
  Collectively, the cited evidence must support the primary causal locus and mechanism. When
  baseline telemetry is available, the evidence set must compare
  the relevant operation or signal across baseline and anomalous periods. Evidence that establishes
  only an observation does not by itself establish its cause.

Every final evidence item must copy an exact query_id returned by an investigation tool. Do not ask
the user questions. The incident prompt states a fixed investigation budget and each tool response
states the remainder. Reserve enough budget to synthesize the result; exhausting calls is not an
objective. Call submit_diagnosis once the available evidence supports the best answer."""


def _system_prompt(visibility: Visibility) -> str:
    """The same investigation method for every arm, with per-stack operating notes.

    The methodology and the diagnosis contract stay byte-identical across arms: a
    difference there would measure the prompt rather than the stack. Only the
    notes describing how to drive a particular store differ, and they state
    interface facts rather than investigation strategy.

    The two GreptimeDB arms therefore share one prompt. Their treatment
    difference is the tools they are given, not the guidance they receive;
    coaching the semantic arm on how to recover from an unhelpful search would
    move the primary comparison by prompt rather than by interface.
    """
    parts = [_INVESTIGATION_METHOD]
    parts.append(
        _SPLIT_PILLARS_NOTES if visibility is Visibility.SPLIT_PILLARS else _GREPTIMEDB_NOTES
    )
    parts.append(_DIAGNOSIS_CONTRACT)
    return "\n\n".join(parts)


def _incident_prompt(
    case_input: CaseInput,
    max_tool_calls: int,
    visibility: Visibility,
) -> str:
    start = datetime.fromtimestamp(case_input.time_start, UTC).isoformat()
    alert_time = datetime.fromtimestamp(case_input.alert_time, UTC).isoformat()
    end = datetime.fromtimestamp(case_input.time_end, UTC).isoformat()
    alert_text = case_input.alert_text or "Generic telemetry anomaly"
    if visibility is Visibility.SPLIT_PILLARS:
        header = ""
        scope = "The three telemetry stores are the only source of incident evidence."
    else:
        header = f"Database: {case_input.database}\n"
        scope = (
            f"The database is the only source of incident evidence. INFORMATION_SCHEMA row "
            f"queries must include table_schema = '{case_input.database}' so results stay "
            f"scoped to this incident."
        )
    return f"""Investigate this production incident.

{header}Telemetry window: {start} through {end}
Alert: {alert_text}
Alert fired at: {alert_time} (Unix {case_input.alert_time})
Investigation budget: at most {max_tool_calls} tool calls. Submit the best-supported diagnosis
before the budget reaches zero.

Find the causal component or directed causal edge, propagated impact when present, fault type, and
onset time. {scope} Treat the alert as the observed symptom; its named entity is not necessarily
the root cause."""
