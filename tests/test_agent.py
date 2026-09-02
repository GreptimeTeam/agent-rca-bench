import json
from types import SimpleNamespace

import pytest

import semantic_rca_bench.agent as agent_module
from semantic_rca_bench.agent import (
    SUBMIT_TOOL,
    AgentError,
    _agent_tools,
    _anthropic_client,
    _catalog_search_output,
    _chat_completions_client,
    _chat_completions_request,
    _citation_output,
    _describe_table_tool,
    _execute_sql_tool,
    _incident_prompt,
    _responses_client,
    _responses_request,
    _search_table_semantics_tool,
    _semantic_graph_output,
    _semantic_graph_query,
    _semantic_graph_tool,
    _submit_tool,
    _system_prompt,
    run_agent,
)
from semantic_rca_bench.contracts import (
    ApiTransport,
    CaseInput,
    DatabaseLoad,
    QueryResult,
    Visibility,
)
from semantic_rca_bench.greptimedb.visibility import MAX_QUERY_MAX_ROWS
from semantic_rca_bench.inspect import summarize_semantic_surfaces


def test_deepseek_model_uses_compatible_anthropic_endpoint(monkeypatch) -> None:
    calls = []
    direct_client = object()

    def fake_client(**kwargs):
        calls.append(kwargs)
        return kwargs

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    monkeypatch.setattr(
        agent_module.anthropic,
        "DefaultHttpxClient",
        lambda **kwargs: direct_client if kwargs == {"trust_env": False} else None,
    )
    monkeypatch.setattr(agent_module.anthropic, "Anthropic", fake_client)

    client = _anthropic_client(ApiTransport.ANTHROPIC_COMPATIBLE_MESSAGES)

    assert client == {
        "api_key": "test-deepseek-key",
        "base_url": "https://api.deepseek.com/anthropic",
        "http_client": direct_client,
    }
    assert calls == [client]


def test_non_deepseek_model_uses_anthropic_endpoint(monkeypatch) -> None:
    calls = []

    def fake_client(**kwargs):
        calls.append(kwargs)
        return kwargs

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setattr(agent_module.anthropic, "Anthropic", fake_client)

    client = _anthropic_client(ApiTransport.ANTHROPIC_MESSAGES)

    assert client == {"api_key": "test-anthropic-key"}
    assert calls == [client]


@pytest.mark.parametrize(
    ("transport", "environment_variable", "expected"),
    [
        (ApiTransport.OPENAI_RESPONSES, "OPENAI_API_KEY", {"api_key": "test-key"}),
        (
            ApiTransport.DASHSCOPE_CN_BEIJING_RESPONSES,
            "DASHSCOPE_API_KEY",
            {
                "api_key": "test-key",
                "base_url": ("https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"),
            },
        ),
    ],
)
def test_responses_clients_use_transport_bound_credentials_and_endpoints(
    monkeypatch, transport, environment_variable, expected
) -> None:
    calls = []
    direct_client = object()

    def fake_client(**kwargs):
        calls.append(kwargs)
        return kwargs

    monkeypatch.setenv(environment_variable, "test-key")
    monkeypatch.setenv(
        "DASHSCOPE_BASE_URL",
        "https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
    )
    monkeypatch.setattr(agent_module.openai, "OpenAI", fake_client)
    monkeypatch.setattr(
        agent_module.openai,
        "DefaultHttpxClient",
        lambda **kwargs: direct_client if kwargs == {"trust_env": False} else None,
    )

    if transport is ApiTransport.DASHSCOPE_CN_BEIJING_RESPONSES:
        expected["http_client"] = direct_client

    client = _responses_client(transport)

    assert client == expected
    assert calls == [client]


def test_bigmodel_client_uses_china_endpoint_and_dedicated_credential(monkeypatch) -> None:
    calls = []
    direct_client = object()

    def fake_client(**kwargs):
        calls.append(kwargs)
        return kwargs

    monkeypatch.setenv("BIGMODEL_API_KEY", "test-key")
    monkeypatch.setattr(
        agent_module.openai,
        "DefaultHttpxClient",
        lambda **kwargs: direct_client if kwargs == {"trust_env": False} else None,
    )
    monkeypatch.setattr(agent_module.openai, "OpenAI", fake_client)

    client = _chat_completions_client(ApiTransport.BIGMODEL_CHAT_COMPLETIONS)

    assert client == {
        "api_key": "test-key",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "http_client": direct_client,
    }
    assert calls == [client]


@pytest.mark.parametrize(
    "base_url",
    [
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "https://workspace.cn-beijing.maas.aliyuncs.com/api/v1",
        "http://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
    ],
)
def test_dashscope_responses_rejects_wrong_region_or_protocol(monkeypatch, base_url) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", base_url)

    with pytest.raises(AgentError, match="China \\(Beijing\\) workspace Responses"):
        _responses_client(ApiTransport.DASHSCOPE_CN_BEIJING_RESPONSES)


def test_dashscope_base_url_falls_back_to_keychain(monkeypatch) -> None:
    base_url = "https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    monkeypatch.delenv("DASHSCOPE_BASE_URL", raising=False)
    monkeypatch.setattr(agent_module.sys, "platform", "darwin")
    monkeypatch.setattr(
        agent_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=f"{base_url}\n"),
    )

    assert agent_module._dashscope_base_url() == base_url


def test_responses_request_keeps_provider_specific_options_separate() -> None:
    common = {
        "model": "model",
        "system_prompt": "system",
        "input_items": [{"role": "user", "content": "incident"}],
        "tools": [],
        "max_output_tokens": 16_384,
        "prompt_cache": True,
    }

    openai_request = _responses_request(
        ApiTransport.OPENAI_RESPONSES,
        Visibility.RAW,
        reasoning_effort="medium",
        **common,
    )
    dashscope_request = _responses_request(
        ApiTransport.DASHSCOPE_CN_BEIJING_RESPONSES,
        Visibility.RAW,
        reasoning_effort="xhigh",
        **common,
    )

    assert openai_request["include"] == ["reasoning.encrypted_content"]
    assert openai_request["prompt_cache_options"] == {"mode": "implicit", "ttl": "30m"}
    assert openai_request["reasoning"] == {"effort": "medium"}
    assert dashscope_request["reasoning"] == {"effort": "xhigh"}
    assert dashscope_request["store"] is False
    assert dashscope_request["parallel_tool_calls"] is False
    assert dashscope_request["extra_headers"] == {"x-dashscope-session-cache": "enable"}
    assert "include" not in dashscope_request
    assert "prompt_cache_key" not in dashscope_request
    assert "prompt_cache_options" not in dashscope_request


def test_bigmodel_request_freezes_reasoning_and_output_budget() -> None:
    request = _chat_completions_request(
        ApiTransport.BIGMODEL_CHAT_COMPLETIONS,
        model="glm-5.3",
        messages=[{"role": "user", "content": "incident"}],
        tools=[],
        max_output_tokens=16_384,
        reasoning_effort="max",
    )

    assert request["max_tokens"] == 16_384
    assert request["extra_body"] == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "max",
    }


def test_tools_expose_only_allowed_semantic_capabilities() -> None:
    raw_profile = str(_describe_table_tool(Visibility.RAW)["description"])
    graph_profile = str(_describe_table_tool(Visibility.SEMANTIC_GRAPH)["description"])
    graph_sql = str(_execute_sql_tool(Visibility.SEMANTIC_GRAPH)["description"])
    raw_sql = str(_execute_sql_tool(Visibility.RAW)["description"])

    assert "semantic metadata" not in raw_profile
    assert "semantic metadata" in graph_profile
    assert "semantic_entities" not in raw_sql
    assert "semantic_entities" in graph_sql
    assert "semantic_relationships" in graph_sql
    assert "entity_id_attrs" in graph_sql
    assert "confidence is derivation certainty" in graph_sql
    assert "Missing edges" in graph_sql
    assert "metadata_quality" in graph_profile
    assert "entity_declarations" in graph_profile
    assert "scope lists namespace or environment columns" in graph_profile
    assert "unmatched_count" in graph_sql
    assert "duration_max" in graph_sql
    max_rows = _execute_sql_tool(Visibility.RAW)["input_schema"]["properties"]["max_rows"]
    assert max_rows == {
        "type": "integer",
        "minimum": 1,
        "maximum": 1000,
        "default": 200,
        "description": (
            "Maximum rows returned for this query. Raise it only when a complete result cannot "
            "be obtained with aggregation or narrower filters."
        ),
    }


def test_graph_tool_reports_active_coverage() -> None:
    tool = _execute_sql_tool(
        Visibility.SEMANTIC_GRAPH,
        {
            "graph": {
                "status": "entity-only",
                "distinct_entity_count": 7,
                "distinct_relationship_count": 0,
            }
        },
    )

    description = str(tool["description"])
    assert "status=entity-only" in description
    assert "distinct_entities=7" in description
    assert "distinct_relationships=0" in description
    assert "do not infer that entities are unrelated" in description


def test_incident_prompt_exposes_alert_time_without_injection_time() -> None:
    prompt = _incident_prompt(
        CaseInput(
            case_token="case",
            database="benchmark_db",
            time_start=100,
            time_end=300,
            alert_time=300,
        ),
        48,
    )

    assert "Generic telemetry anomaly" in prompt
    assert "Alert fired at" in prompt
    assert "Unix 300" in prompt
    assert "at most 48 tool calls" in prompt
    assert "Incident detected at" not in prompt


def test_system_prompt_only_requires_baseline_comparison_when_known() -> None:
    prompt = " ".join(_system_prompt().split())

    assert "when the telemetry window contains a known baseline" in prompt


def test_system_prompt_uses_generic_hypothesis_triage_without_case_clues() -> None:
    prompt = " ".join(_system_prompt().split())
    lowered = prompt.lower()

    assert "two or three hypotheses that differ in causal scope or mechanism" in prompt
    assert "observed signals as evidence, not automatically as causes" in prompt
    assert "compare the same operation before and after onset" in prompt
    assert "does not by itself prove" in prompt
    assert "broad resource health checks only" in prompt
    assert "do not exhaust the tool budget on traces or logs" not in lowered
    assert "ts-security-service" not in lowered
    assert "ts-order-other-service" not in lowered
    assert "method replacement" not in lowered
    assert "packet loss" not in lowered
    assert "socket exhaustion" not in lowered


def test_diagnosis_requires_canonical_fault_category() -> None:
    schema = SUBMIT_TOOL["input_schema"]

    assert "fault_category" in schema["required"]
    assert schema["properties"]["fault_category"]["enum"] == [
        "cpu",
        "delay",
        "disk",
        "loss",
        "memory",
        "socket",
        "other",
    ]
    assert {
        "causal_scope",
        "causal_component",
        "edge_source",
        "edge_destination",
        "impacted_component",
        "causal_operation",
        "mechanism_code",
    } <= set(schema["required"])
    assert schema["properties"]["causal_scope"]["enum"] == [
        "component",
        "dependency_edge",
    ]
    assert "call_path_delay" in schema["properties"]["mechanism_code"]["enum"]
    evidence = schema["properties"]["evidence"]["items"]
    assert "claim_types" in evidence["required"]
    claim_types = evidence["properties"]["claim_types"]["items"]["enum"]
    assert "causal_locus" in claim_types
    assert "propagated_impact" in claim_types
    assert "causal_scope" not in claim_types
    claim_description = evidence["properties"]["claim_types"]["description"]
    assert "where the mechanism occurs" in claim_description
    assert "downstream symptom" in claim_description


def test_current_diagnosis_contract_is_case_independent() -> None:
    prompt = _system_prompt().lower()
    schema = json.dumps(SUBMIT_TOOL, sort_keys=True).lower()

    for source_label in (
        "httprequestdelay",
        "ts-route-plan-service",
        "ts-travel2-service",
        "trips/left",
    ):
        assert source_label not in prompt
        assert source_label not in schema


def test_dataset_fault_taxonomy_guides_without_rejecting_out_of_taxonomy_output() -> None:
    tool = _submit_tool(["httpError5xx", "rateLimiting"])
    fault_type = tool["input_schema"]["properties"]["fault_type"]

    assert "enum" not in fault_type
    assert "httpError5xx, rateLimiting" in fault_type["description"]
    assert "scored as incorrect" in fault_type["description"]
    assert "enum" not in SUBMIT_TOOL["input_schema"]["properties"]["fault_type"]


def test_table_samples_are_opt_in() -> None:
    tool = _describe_table_tool(Visibility.RAW)

    assert tool["input_schema"]["properties"]["include_samples"]["default"] is False
    assert tool["input_schema"]["properties"]["sample_limit"]["default"] == 1


def test_graph_query_tool_is_only_available_in_graph_treatment() -> None:
    raw_names = {tool["name"] for tool in _agent_tools(Visibility.RAW, [], None)}
    graph_names = {tool["name"] for tool in _agent_tools(Visibility.SEMANTIC_GRAPH, [], None)}

    assert "query_semantic_graph" not in raw_names
    assert "query_semantic_graph" in graph_names


def test_semantic_catalog_search_is_hidden_from_raw_treatment() -> None:
    raw_names = {tool["name"] for tool in _agent_tools(Visibility.RAW, [], None)}
    graph_names = {tool["name"] for tool in _agent_tools(Visibility.SEMANTIC_GRAPH, [], None)}

    assert "search_table_semantics" not in raw_names
    assert "search_table_semantics" in graph_names


def test_semantic_catalog_search_explains_metadata_boundary() -> None:
    tool = _search_table_semantics_tool()
    description = str(tool["description"])

    assert "wide" in description
    assert "does not search telemetry row values" in description
    assert tool["input_schema"]["properties"]["limit"]["default"] == 50


def test_empty_catalog_search_guides_fault_mechanism_retry() -> None:
    output = _catalog_search_output(
        {"matched_table_count": 0, "matches": []},
        ["high CPU usage", "high memory usage"],
    )

    assert "application-domain words" in str(output["guidance"])
    assert "high memory usage" in str(output["guidance"])


def test_citation_output_replaces_provider_facing_query_id() -> None:
    output, query_id = _citation_output({"query_id": "opaque-long-id", "rows": []}, 3)

    assert query_id == "q03"
    assert output["query_id"] == "q03"


def test_graph_query_is_suppressed_for_inspected_empty_coverage() -> None:
    coverage = summarize_semantic_surfaces({})
    names = {tool["name"] for tool in _agent_tools(Visibility.SEMANTIC_GRAPH, [], coverage)}
    sql_description = str(_execute_sql_tool(Visibility.SEMANTIC_GRAPH, coverage)["description"])

    assert "query_semantic_graph" not in names
    assert coverage["graph"]["status"] == "empty"
    assert "no Semantic Graph entities or relationships" in sql_description
    assert "Do not query graph tables" in sql_description


def test_graph_tool_explains_identity_and_relational_entry_point() -> None:
    tool = _semantic_graph_tool({"graph": {"status": "relational"}})
    description = str(tool["description"])

    assert "not Semantic Graph entity IDs" in description
    assert "Start with view=relationships" in description
    assert "no src_id or dst_id filter" in description
    assert "rel_type=calls" in description


def test_empty_filtered_graph_result_explains_how_to_discover_ids() -> None:
    output = _semantic_graph_output(
        QueryResult(
            query_id="query",
            columns=["entity_id"],
            rows=[],
            elapsed_seconds=0.1,
        ),
        {"view": "entities", "entity_id": "external-alert-id"},
    )

    assert "guidance" in output
    assert "Retry view=entities without entity_id" in str(output["guidance"])


def test_agent_records_requested_calls_rejected_by_the_tool_budget(monkeypatch) -> None:
    class Response:
        def __init__(self, blocks: list[SimpleNamespace]) -> None:
            self.content = blocks
            self.usage = SimpleNamespace(input_tokens=10, output_tokens=5)

        def model_dump(self, *, mode: str) -> dict[str, object]:
            assert mode == "json"
            return {
                "content": [
                    {"type": block.type, "name": block.name, "input": block.input}
                    for block in self.content
                ],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }

    diagnosis = {
        "causal_scope": "component",
        "causal_component": "checkout",
        "edge_source": None,
        "edge_destination": None,
        "impacted_component": None,
        "causal_operation": None,
        "fault_category": "cpu",
        "mechanism_code": "cpu_saturation",
        "fault_type": "cpu",
        "confidence": 0.7,
        "evidence": [],
        "alternative_candidates": [],
        "explanation": "CPU saturation is the most likely cause.",
    }
    responses = iter(
        [
            Response(
                [
                    SimpleNamespace(
                        type="tool_use",
                        name="execute_sql",
                        input={"query": "SELECT 1"},
                        id="tool-1",
                    )
                ]
            ),
            Response(
                [
                    SimpleNamespace(
                        type="tool_use",
                        name="submit_diagnosis",
                        input=diagnosis,
                        id="tool-2",
                    )
                ]
            ),
        ]
    )
    requests = []

    def create(**kwargs):
        requests.append(kwargs)
        return next(responses)

    provider = SimpleNamespace(messages=SimpleNamespace(create=create))
    monkeypatch.setattr(agent_module, "_anthropic_client", lambda _: provider)
    gateway = SimpleNamespace(client=SimpleNamespace())

    result = run_agent(
        gateway,  # type: ignore[arg-type]
        CaseInput(case_token="case", time_start=100, time_end=200, alert_time=200),
        Visibility.RAW,
        model="test-model",
        api_transport=ApiTransport.ANTHROPIC_MESSAGES,
        reasoning_effort="high",
        max_output_tokens=16_384,
        max_tool_calls=0,
    )

    assert result.tool_calls == []
    assert result.tool_calls_requested == 1
    assert result.tool_budget_exhausted is True
    assert "at most 0 tool calls" in requests[0]["messages"][0]["content"]
    budget_text = [
        block["text"]
        for message in requests[1]["messages"]
        if isinstance(message["content"], list)
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    assert any(text.startswith("Investigation budget: 0 tool calls remain") for text in budget_text)
    assert all(request["cache_control"] == {"type": "ephemeral"} for request in requests)
    assert all(request["output_config"] == {"effort": "high"} for request in requests)
    assert all(request["max_tokens"] == 16_384 for request in requests)


def test_deepseek_uses_automatic_cache_and_counts_native_usage(monkeypatch) -> None:
    diagnosis = {
        "causal_scope": "component",
        "causal_component": "checkout",
        "edge_source": None,
        "edge_destination": None,
        "impacted_component": None,
        "causal_operation": None,
        "fault_category": "cpu",
        "mechanism_code": "cpu_saturation",
        "fault_type": "cpu",
        "confidence": 0.7,
        "evidence": [],
        "alternative_candidates": [],
        "explanation": "CPU saturation is the most likely cause.",
    }

    class Response:
        content = [
            SimpleNamespace(
                type="tool_use",
                name="submit_diagnosis",
                input=diagnosis,
                id="tool-1",
            )
        ]
        usage = SimpleNamespace(input_tokens=120, output_tokens=5)

        def model_dump(self, *, mode: str) -> dict[str, object]:
            assert mode == "json"
            return {
                "content": [],
                "usage": {
                    "input_tokens": 120,
                    "output_tokens": 5,
                    "prompt_cache_hit_tokens": 100,
                    "prompt_cache_miss_tokens": 20,
                },
            }

    requests = []

    def create(**kwargs):
        requests.append(kwargs)
        return Response()

    provider = SimpleNamespace(messages=SimpleNamespace(create=create))
    monkeypatch.setattr(agent_module, "_anthropic_client", lambda _: provider)

    result = run_agent(
        SimpleNamespace(client=SimpleNamespace()),  # type: ignore[arg-type]
        CaseInput(case_token="case", time_start=100, time_end=200, alert_time=200),
        Visibility.RAW,
        model="deepseek-v4-flash",
        api_transport=ApiTransport.ANTHROPIC_COMPATIBLE_MESSAGES,
        reasoning_effort="high",
        max_output_tokens=16_384,
        max_tool_calls=2,
    )

    assert result.usage.input_tokens == 120
    assert result.usage.output_tokens == 5
    assert "cache_control" not in requests[0]
    assert requests[0]["output_config"] == {"effort": "high"}
    assert requests[0]["max_tokens"] == 16_384


def test_api_input_usage_remains_total_when_provider_omits_cache_details() -> None:
    assert (
        agent_module._provider_total_input_tokens(
            {"usage": {"prompt_tokens": 120, "completion_tokens": 5}}
        )
        == 120
    )
    assert (
        agent_module._provider_total_input_tokens(
            {
                "usage": {
                    "input_tokens": 20,
                    "cache_creation_input_tokens": 40,
                    "cache_read_input_tokens": 160,
                }
            }
        )
        == 220
    )


def test_reasoning_usage_normalizes_provider_breakdown_names() -> None:
    assert (
        agent_module._reasoning_tokens(
            {
                "usage": {
                    "output_tokens": 70,
                    "output_tokens_details": {"thinking_tokens": 50},
                }
            }
        )
        == 50
    )
    assert (
        agent_module._reasoning_tokens(
            {
                "usage": {
                    "completion_tokens": 80,
                    "completion_tokens_details": {"reasoning_tokens": 60},
                }
            }
        )
        == 60
    )
    assert (
        agent_module._reasoning_tokens(
            {
                "usage": {
                    "output_tokens": 10,
                    "output_tokens_details": {
                        "reasoning_tokens": 0,
                        "thinking_tokens": 7,
                    },
                }
            }
        )
        == 0
    )


def test_anthropic_refusal_fails_without_retrying(monkeypatch) -> None:
    class Response:
        content: list[object] = []
        usage = SimpleNamespace(input_tokens=10, output_tokens=0)

        def model_dump(self, *, mode: str) -> dict[str, object]:
            assert mode == "json"
            return {
                "content": [],
                "stop_reason": "refusal",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 0,
                    "output_tokens_details": {"thinking_tokens": 0},
                },
            }

    requests: list[dict[str, object]] = []

    def create(**kwargs):
        requests.append(kwargs)
        return Response()

    monkeypatch.setattr(
        agent_module,
        "_anthropic_client",
        lambda _: SimpleNamespace(messages=SimpleNamespace(create=create)),
    )

    result = run_agent(
        SimpleNamespace(client=SimpleNamespace()),  # type: ignore[arg-type]
        CaseInput(case_token="case", time_start=100, time_end=200, alert_time=200),
        Visibility.RAW,
        model="claude-fable-5",
        api_transport=ApiTransport.ANTHROPIC_MESSAGES,
        reasoning_effort="high",
        max_output_tokens=16_384,
        max_tool_calls=2,
    )

    assert result.error == "agent provider refused the request"
    assert len(requests) == 1


def test_openai_responses_runner_projects_continuation_items_and_counts_cache_usage(
    monkeypatch,
) -> None:
    diagnosis = {
        "causal_scope": "component",
        "causal_component": "checkout",
        "edge_source": None,
        "edge_destination": None,
        "impacted_component": None,
        "causal_operation": None,
        "fault_category": "cpu",
        "mechanism_code": "cpu_saturation",
        "fault_type": "cpu saturation",
        "confidence": 0.7,
        "evidence": [],
        "alternative_candidates": [],
        "explanation": "CPU saturation is the most likely cause.",
    }

    class Response:
        def __init__(self, output, raw_output, usage):
            self.output = output
            self._raw_output = raw_output
            self._usage = usage
            self.usage = SimpleNamespace(output_tokens=usage["output_tokens"])

        def model_dump(self, *, mode: str) -> dict[str, object]:
            assert mode == "json"
            return {
                "status": "completed",
                "incomplete_details": None,
                "output": self._raw_output,
                "usage": self._usage,
            }

    first_raw_output = [
        {
            "type": "reasoning",
            "id": "reasoning-1",
            "status": "completed",
            "summary": [],
            "content": [],
            "encrypted_content": "opaque-reasoning",
        },
        {
            "type": "function_call",
            "id": "item-1",
            "call_id": "call-1",
            "name": "execute_sql",
            "arguments": '{"query":"SELECT 1"}',
            "status": "completed",
            "caller": None,
            "namespace": None,
        },
    ]
    responses = iter(
        [
            Response(
                [
                    SimpleNamespace(type="reasoning"),
                    SimpleNamespace(
                        type="function_call",
                        call_id="call-1",
                        name="execute_sql",
                        arguments='{"query":"SELECT 1"}',
                    ),
                ],
                first_raw_output,
                {
                    "input_tokens": 120,
                    "input_tokens_details": {
                        "cached_tokens": 100,
                        "cache_write_tokens": 10,
                    },
                    "output_tokens": 5,
                    "output_tokens_details": {"reasoning_tokens": 3},
                },
            ),
            Response(
                [
                    SimpleNamespace(
                        type="function_call",
                        call_id="call-2",
                        name="submit_diagnosis",
                        arguments=json.dumps(diagnosis),
                    )
                ],
                [
                    {
                        "type": "function_call",
                        "id": "item-2",
                        "call_id": "call-2",
                        "name": "submit_diagnosis",
                        "arguments": json.dumps(diagnosis),
                    }
                ],
                {
                    "input_tokens": 50,
                    "input_tokens_details": {
                        "cached_tokens": 20,
                        "cache_write_tokens": 0,
                    },
                    "output_tokens": 6,
                    "output_tokens_details": {"reasoning_tokens": 1},
                },
            ),
        ]
    )
    requests = []

    def create(**kwargs):
        requests.append(kwargs)
        return next(responses)

    provider = SimpleNamespace(responses=SimpleNamespace(create=create))
    monkeypatch.setattr(agent_module, "_responses_client", lambda _: provider)
    gateway = SimpleNamespace(
        client=SimpleNamespace(),
        execute=lambda _: QueryResult(
            query_id="q1",
            columns=["value"],
            rows=[[1]],
            elapsed_seconds=0.01,
        ),
    )

    result = run_agent(
        gateway,  # type: ignore[arg-type]
        CaseInput(case_token="case", time_start=100, time_end=200, alert_time=200),
        Visibility.RAW,
        model="gpt-5.6-sol",
        api_transport=ApiTransport.OPENAI_RESPONSES,
        reasoning_effort="medium",
        max_output_tokens=16384,
        max_tool_calls=2,
    )

    assert result.diagnosis is not None
    assert result.usage.input_tokens == 170
    assert result.usage.output_tokens == 11
    assert result.usage.reasoning_tokens == 4
    assert result.api_transport is ApiTransport.OPENAI_RESPONSES
    assert result.reasoning_effort == "medium"
    assert result.max_output_tokens == 16384
    assert len(result.tool_calls) == 1
    assert requests[0]["store"] is False
    assert requests[0]["include"] == ["reasoning.encrypted_content"]
    assert requests[0]["reasoning"] == {"effort": "medium"}
    assert requests[0]["max_output_tokens"] == 16384
    assert requests[0]["prompt_cache_key"].startswith("semantic-rca-raw-")
    assert requests[0]["prompt_cache_options"] == {"mode": "implicit", "ttl": "30m"}
    assert all(
        tool["type"] == "function" and tool["strict"] is False for tool in requests[0]["tools"]
    )
    second_input = requests[1]["input"]
    reasoning = next(item for item in second_input if item.get("type") == "reasoning")
    assert reasoning == {
        "type": "reasoning",
        "id": "reasoning-1",
        "summary": [],
        "content": [],
        "encrypted_content": "opaque-reasoning",
    }
    function_call = next(item for item in second_input if item.get("type") == "function_call")
    assert function_call == {
        "type": "function_call",
        "id": "item-1",
        "call_id": "call-1",
        "name": "execute_sql",
        "arguments": '{"query":"SELECT 1"}',
    }
    function_output = next(
        item for item in second_input if item.get("type") == "function_call_output"
    )
    assert function_output["call_id"] == "call-1"
    assert json.loads(function_output["output"])["query_id"] == "q01"
    assert result.responses[0]["output"] == first_raw_output


def test_bigmodel_chat_completions_runner_replays_tools_and_counts_cached_input(
    monkeypatch,
) -> None:
    diagnosis = {
        "causal_scope": "component",
        "causal_component": "checkout",
        "edge_source": None,
        "edge_destination": None,
        "impacted_component": None,
        "causal_operation": None,
        "fault_category": "cpu",
        "mechanism_code": "cpu_saturation",
        "fault_type": "cpu saturation",
        "confidence": 0.7,
        "evidence": [],
        "alternative_candidates": [],
        "explanation": "CPU saturation is the most likely cause.",
    }

    class Message:
        def __init__(self, tool_calls, raw):
            self.tool_calls = tool_calls
            self.raw = raw

        def model_dump(self, *, mode: str, exclude_none: bool) -> dict[str, object]:
            assert mode == "json"
            assert exclude_none is True
            return self.raw

    class Response:
        def __init__(self, message, finish_reason, usage):
            self.choices = [SimpleNamespace(message=message, finish_reason=finish_reason)]
            self.usage = SimpleNamespace(completion_tokens=usage["completion_tokens"])
            self.raw = {
                "choices": [{"message": message.raw, "finish_reason": finish_reason, "index": 0}],
                "usage": usage,
            }

        def model_dump(self, *, mode: str) -> dict[str, object]:
            assert mode == "json"
            return self.raw

    execute_call = SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(name="execute_sql", arguments='{"query":"SELECT 1"}'),
    )
    submit_call = SimpleNamespace(
        id="call-2",
        function=SimpleNamespace(name="submit_diagnosis", arguments=json.dumps(diagnosis)),
    )
    responses = iter(
        [
            Response(
                Message(
                    [execute_call],
                    {
                        "role": "assistant",
                        "content": None,
                        "reasoning_content": "inspect the data",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "execute_sql",
                                    "arguments": '{"query":"SELECT 1"}',
                                },
                            }
                        ],
                    },
                ),
                "tool_calls",
                {
                    "prompt_tokens": 120,
                    "prompt_tokens_details": {"cached_tokens": 100},
                    "completion_tokens": 5,
                    "total_tokens": 125,
                },
            ),
            Response(
                Message(
                    [submit_call],
                    {
                        "role": "assistant",
                        "content": None,
                        "reasoning_content": "submit",
                        "tool_calls": [
                            {
                                "id": "call-2",
                                "type": "function",
                                "function": {
                                    "name": "submit_diagnosis",
                                    "arguments": json.dumps(diagnosis),
                                },
                            }
                        ],
                    },
                ),
                "tool_calls",
                {
                    "prompt_tokens": 50,
                    "prompt_tokens_details": {"cached_tokens": 30},
                    "completion_tokens": 6,
                    "total_tokens": 56,
                },
            ),
        ]
    )
    requests = []

    def create(**kwargs):
        requests.append(kwargs)
        return next(responses)

    provider = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(agent_module, "_chat_completions_client", lambda _: provider)
    gateway = SimpleNamespace(
        client=SimpleNamespace(),
        execute=lambda _: QueryResult(
            query_id="q1",
            columns=["value"],
            rows=[[1]],
            elapsed_seconds=0.01,
        ),
    )

    result = run_agent(
        gateway,  # type: ignore[arg-type]
        CaseInput(case_token="case", time_start=100, time_end=200, alert_time=200),
        Visibility.RAW,
        model="glm-5.3",
        api_transport=ApiTransport.BIGMODEL_CHAT_COMPLETIONS,
        reasoning_effort="max",
        max_output_tokens=16_384,
        max_tool_calls=2,
    )

    assert result.diagnosis is not None
    assert result.usage.input_tokens == 170
    assert result.usage.output_tokens == 11
    assert result.usage.reasoning_tokens == 0
    assert result.api_transport is ApiTransport.BIGMODEL_CHAT_COMPLETIONS
    assert result.reasoning_effort == "max"
    assert len(result.tool_calls) == 1
    assert requests[0]["extra_body"] == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "max",
    }
    assert requests[0]["max_tokens"] == 16_384
    second_messages = requests[1]["messages"]
    assistant = next(message for message in second_messages if message["role"] == "assistant")
    assert assistant["reasoning_content"] == "inspect the data"
    tool_result = next(message for message in second_messages if message["role"] == "tool")
    assert tool_result["tool_call_id"] == "call-1"
    assert json.loads(tool_result["content"])["query_id"] == "q01"


def test_openai_incomplete_response_records_provider_reason(monkeypatch) -> None:
    raw = {
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "output": [],
        "usage": {
            "input_tokens": 10,
            "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
            "output_tokens": 16_384,
            "output_tokens_details": {"reasoning_tokens": 16_384},
        },
    }

    class Response:
        output: list[object] = []
        usage = SimpleNamespace(output_tokens=16_384)

        def model_dump(self, *, mode: str) -> dict[str, object]:
            assert mode == "json"
            return raw

    provider = SimpleNamespace(responses=SimpleNamespace(create=lambda **_: Response()))
    monkeypatch.setattr(agent_module, "_responses_client", lambda _: provider)

    result = run_agent(
        SimpleNamespace(client=SimpleNamespace()),  # type: ignore[arg-type]
        CaseInput(case_token="case", time_start=100, time_end=200, alert_time=200),
        Visibility.RAW,
        model="gpt-5.6-sol",
        api_transport=ApiTransport.OPENAI_RESPONSES,
        reasoning_effort="medium",
        max_output_tokens=16_384,
        max_tool_calls=2,
    )

    assert result.error == (
        "invalid provider response: Responses API status is 'incomplete': max_output_tokens"
    )
    assert result.responses == [raw]
    assert result.usage.output_tokens == 16_384
    assert result.usage.reasoning_tokens == 16_384


def test_openai_tool_error_uses_explicit_json_envelope() -> None:
    output = agent_module._responses_tool_output(
        agent_module.ToolInvocation(
            content="SQL rejected",
            is_error=True,
            remaining=7,
        )
    )

    assert json.loads(output) == {
        "is_error": True,
        "error": "SQL rejected",
        "remaining_tool_calls": 7,
    }


def test_invalid_provider_usage_is_persisted_with_raw_response(monkeypatch) -> None:
    class Response:
        content: list[object] = []
        usage = SimpleNamespace(output_tokens=5)

        def model_dump(self, *, mode: str) -> dict[str, object]:
            assert mode == "json"
            return {"content": []}

    provider = SimpleNamespace(messages=SimpleNamespace(create=lambda **_: Response()))
    monkeypatch.setattr(agent_module, "_anthropic_client", lambda _: provider)

    result = run_agent(
        SimpleNamespace(client=SimpleNamespace()),  # type: ignore[arg-type]
        CaseInput(case_token="case", time_start=100, time_end=200, alert_time=200),
        Visibility.RAW,
        model="test-model",
        api_transport=ApiTransport.ANTHROPIC_MESSAGES,
        max_tool_calls=2,
    )

    assert result.error == "invalid provider response: provider response has no usage object"
    assert result.responses == [{"content": []}]
    assert result.tool_calls == []


def test_valid_final_output_records_same_response_investigation_calls_as_rejected(
    monkeypatch,
) -> None:
    diagnosis = {
        "causal_scope": "component",
        "causal_component": "checkout",
        "edge_source": None,
        "edge_destination": None,
        "impacted_component": None,
        "causal_operation": None,
        "fault_category": "cpu",
        "mechanism_code": "cpu_saturation",
        "fault_type": "cpu",
        "confidence": 0.7,
        "evidence": [],
        "alternative_candidates": [],
        "explanation": "CPU saturation is the most likely cause.",
    }

    class Response:
        content = [
            SimpleNamespace(
                type="tool_use",
                name="execute_sql",
                input={"query": "SELECT 1"},
                id="tool-1",
            ),
            SimpleNamespace(
                type="tool_use",
                name="submit_diagnosis",
                input=diagnosis,
                id="tool-2",
            ),
        ]
        usage = SimpleNamespace(input_tokens=10, output_tokens=5)

        def model_dump(self, *, mode: str) -> dict[str, object]:
            assert mode == "json"
            return {
                "content": [
                    {"type": block.type, "name": block.name, "input": block.input}
                    for block in self.content
                ],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }

    provider = SimpleNamespace(messages=SimpleNamespace(create=lambda **_: Response()))
    monkeypatch.setattr(agent_module, "_anthropic_client", lambda _: provider)

    result = run_agent(
        SimpleNamespace(client=SimpleNamespace()),  # type: ignore[arg-type]
        CaseInput(case_token="case", time_start=100, time_end=200, alert_time=200),
        Visibility.RAW,
        model="test-model",
        api_transport=ApiTransport.ANTHROPIC_MESSAGES,
        max_tool_calls=2,
    )

    assert result.diagnosis is not None
    assert result.tool_calls == []
    assert result.tool_calls_requested == 1
    assert len(result.rejected_tool_calls) == 1
    assert result.rejected_tool_calls[0].tool_name == "execute_sql"
    assert result.rejected_tool_calls[0].reason_code == "superseded_by_final_output"


def test_agent_turn_limit_tracks_tool_budget_and_records_failure(monkeypatch) -> None:
    class Response:
        content: list[object] = []
        usage = SimpleNamespace(input_tokens=1, output_tokens=1)

        def model_dump(self, *, mode: str) -> dict[str, object]:
            assert mode == "json"
            return {"content": [], "usage": {"input_tokens": 1, "output_tokens": 1}}

    calls = 0

    def create(**kwargs):
        nonlocal calls
        calls += 1
        return Response()

    provider = SimpleNamespace(messages=SimpleNamespace(create=create))
    monkeypatch.setattr(agent_module, "_anthropic_client", lambda _: provider)

    result = run_agent(
        SimpleNamespace(client=SimpleNamespace()),  # type: ignore[arg-type]
        CaseInput(case_token="case", time_start=100, time_end=200, alert_time=200),
        Visibility.RAW,
        model="test-model",
        api_transport=ApiTransport.ANTHROPIC_MESSAGES,
        max_tool_calls=2,
    )

    assert calls == 12
    assert result.diagnosis is None
    assert result.error == "agent did not submit a diagnosis within 12 turns"


def test_api_provider_initialization_failure_is_recorded(monkeypatch) -> None:
    monkeypatch.setattr(
        agent_module,
        "_anthropic_client",
        lambda _: (_ for _ in ()).throw(AgentError("credentials unavailable")),
    )

    result = run_agent(
        SimpleNamespace(client=SimpleNamespace()),  # type: ignore[arg-type]
        CaseInput(case_token="case", time_start=100, time_end=200, alert_time=200),
        Visibility.RAW,
        model="test-model",
        api_transport=ApiTransport.ANTHROPIC_MESSAGES,
        max_tool_calls=2,
    )

    assert result.diagnosis is None
    assert result.error == "agent provider failed: credentials unavailable"
    assert result.tool_calls == []


def test_api_session_records_unknown_tools_as_rejected_calls() -> None:
    session = agent_module.InvestigationSession(
        SimpleNamespace(client=SimpleNamespace()),  # type: ignore[arg-type]
        CaseInput(case_token="case", time_start=100, time_end=200, alert_time=200),
        Visibility.RAW,
        max_tool_calls=2,
        semantic_coverage=None,
    )

    result = session.invoke("invented_tool", {"value": 1})

    assert result.is_error
    assert session.tool_calls == []
    assert session.tool_calls_requested == 1
    assert len(session.rejected_tool_calls) == 1
    assert session.rejected_tool_calls[0].tool_name == "invented_tool"
    assert session.remaining == 2


def test_api_runner_rejects_nonpositive_output_budget_before_provider_access() -> None:
    with pytest.raises(ValueError, match="max_output_tokens must be positive"):
        run_agent(
            SimpleNamespace(client=SimpleNamespace()),  # type: ignore[arg-type]
            CaseInput(case_token="case", time_start=100, time_end=200, alert_time=200),
            Visibility.RAW,
            model="test-model",
            api_transport=ApiTransport.ANTHROPIC_MESSAGES,
            max_output_tokens=0,
        )


def test_investigation_trace_records_database_load_delta() -> None:
    class Client:
        database = "benchmark_db"

        def __init__(self) -> None:
            self.load = DatabaseLoad(max_concurrency=7)

        def query_load_snapshot(self) -> DatabaseLoad:
            return self.load.model_copy(deep=True)

    client = Client()

    class Gateway:
        def __init__(self) -> None:
            self.client = client

        def execute(self, query: str) -> QueryResult:
            assert query == "SELECT 1"
            client.load.query_count = 1
            client.load.rows_returned = 3
            client.load.query_elapsed_seconds = 0.2
            return QueryResult(
                query_id="provider-id",
                columns=["value"],
                rows=[[1], [2], [3]],
                elapsed_seconds=0.2,
            )

    session = agent_module.InvestigationSession(
        Gateway(),  # type: ignore[arg-type]
        CaseInput(case_token="case", time_start=100, time_end=200, alert_time=200),
        Visibility.RAW,
        max_tool_calls=2,
        semantic_coverage=None,
    )

    session.invoke("execute_sql", {"query": "SELECT 1"})

    assert session.tool_calls[0].database_load == DatabaseLoad(
        query_count=1,
        rows_returned=3,
        query_elapsed_seconds=0.2,
        max_concurrency=1,
    )


def test_execute_sql_passes_explicit_row_limit_to_gateway() -> None:
    calls = []

    class Gateway:
        client = SimpleNamespace()

        def execute(self, query: str, *, max_rows: int | None = None) -> QueryResult:
            calls.append((query, max_rows))
            return QueryResult(query_id="provider", columns=[], rows=[], elapsed_seconds=0)

    session = agent_module.InvestigationSession(
        Gateway(),  # type: ignore[arg-type]
        CaseInput(case_token="case", time_start=100, time_end=200, alert_time=200),
        Visibility.RAW,
        max_tool_calls=1,
        semantic_coverage=None,
    )

    session.invoke("execute_sql", {"query": "SELECT * FROM traces", "max_rows": 1000})

    assert calls == [("SELECT * FROM traces", 1000)]
    assert session.tool_calls[0].input["max_rows"] == 1000


def test_truncated_final_citation_is_returned_for_repair(monkeypatch) -> None:
    def diagnosis(query_id: str) -> dict[str, object]:
        return {
            "causal_scope": "component",
            "causal_component": "checkout",
            "edge_source": None,
            "edge_destination": None,
            "impacted_component": None,
            "causal_operation": None,
            "fault_category": "cpu",
            "mechanism_code": "cpu_saturation",
            "fault_type": "cpu saturation",
            "confidence": 0.7,
            "evidence": [
                {
                    "query_id": query_id,
                    "claim": "CPU is saturated.",
                    "claim_types": ["fault_mechanism"],
                }
            ],
            "alternative_candidates": [],
            "explanation": "The complete aggregate supports CPU saturation.",
        }

    class Response:
        def __init__(self, name: str, value: dict[str, object], identifier: str) -> None:
            self.content = [SimpleNamespace(type="tool_use", name=name, input=value, id=identifier)]
            self.usage = SimpleNamespace(input_tokens=1, output_tokens=1)

        def model_dump(self, *, mode: str) -> dict[str, object]:
            assert mode == "json"
            return {
                "content": [
                    {"type": block.type, "name": block.name, "input": block.input}
                    for block in self.content
                ],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }

    responses = iter(
        [
            Response("execute_sql", {"query": "SELECT * FROM cpu"}, "tool-1"),
            Response("submit_diagnosis", diagnosis("q01"), "tool-2"),
            Response(
                "execute_sql",
                {"query": "SELECT host, MAX(usage) FROM cpu GROUP BY host"},
                "tool-3",
            ),
            Response("submit_diagnosis", diagnosis("q02"), "tool-4"),
        ]
    )
    provider = SimpleNamespace(messages=SimpleNamespace(create=lambda **_: next(responses)))
    monkeypatch.setattr(agent_module, "_anthropic_client", lambda _: provider)

    class Gateway:
        client = SimpleNamespace()

        def __init__(self) -> None:
            self.calls = 0

        def execute(self, _: str) -> QueryResult:
            self.calls += 1
            return QueryResult(
                query_id="provider",
                columns=["value"],
                rows=[[1]],
                elapsed_seconds=0,
                truncated=self.calls == 1,
            )

    result = run_agent(
        Gateway(),  # type: ignore[arg-type]
        CaseInput(case_token="case", time_start=100, time_end=200, alert_time=200),
        Visibility.RAW,
        model="test-model",
        api_transport=ApiTransport.ANTHROPIC_MESSAGES,
        max_tool_calls=4,
    )

    assert result.diagnosis is not None
    assert [trace.query_id for trace in result.tool_calls] == ["q01", "q02"]
    assert len(result.responses) == 4
    assert result.diagnosis.evidence[0].query_id == "q02"


def test_unfiltered_empty_graph_result_does_not_add_identity_guidance() -> None:
    output = _semantic_graph_output(
        QueryResult(query_id="query", columns=[], rows=[], elapsed_seconds=0.1),
        {"view": "relationships", "rel_type": "calls"},
    )

    assert "guidance" not in output


def test_graph_relationship_query_supplies_window_scope_and_deduplication() -> None:
    query = _semantic_graph_query(
        CaseInput(
            case_token="case",
            time_start=1_777_094_292,
            time_end=1_777_094_892,
            alert_time=1_777_094_426,
        ),
        {
            "view": "relationships",
            "rel_type": "calls",
            "src_id": "checkout' OR true",
            "limit": 5_000,
        },
    )

    assert "greptime_private.semantic_relationships" in query
    assert "SELECT window_start, window_end" in query
    assert "MAX(request_count) AS request_count" in query
    assert "MAX(unmatched_count) AS unmatched_count" in query
    assert "SUM(unmatched_count) AS unmatched_count" in query
    assert query.count("MAX(duration_max) AS duration_max") == 2
    assert "GROUP BY window_start, window_end, src_type, src_id, dst_type, dst_id" in query
    assert "observed_at >= '2026-04-25 05:18:12'" in query
    assert "observed_at < '2026-04-25 05:28:12'" in query
    assert "src_id = 'checkout'' OR true'" in query
    assert "attributes" not in query
    assert "LIMIT 1001" in query


def test_graph_relationship_query_can_use_an_audited_minute_envelope() -> None:
    query = _semantic_graph_query(
        CaseInput(
            case_token="aegis-transfer-001",
            time_start=1_752_918_758,
            time_end=1_752_919_238,
            alert_time=1_752_918_998,
        ),
        {"view": "relationships"},
        window=(1_752_918_720, 1_752_919_260),
    )

    assert "observed_at >= '2025-07-19 09:52:00'" in query
    assert "observed_at < '2025-07-19 10:01:00'" in query


MEASUREMENT_CASE = CaseInput(
    case_token="case",
    time_start=1_777_094_292,
    time_end=1_777_094_892,
    alert_time=1_777_094_426,
)


def test_graph_relationship_query_accepts_bounded_window_rows() -> None:
    query = _semantic_graph_query(
        MEASUREMENT_CASE,
        {
            "view": "relationships",
            "start_time": "2026-04-25T05:20:00+00:00",
            "end_time": "2026-04-25T05:25:00+00:00",
            "bucket": "window",
            "limit": 1_000,
        },
    )

    assert "observed_at >= '2026-04-25 05:20:00'" in query
    assert "observed_at < '2026-04-25 05:25:00'" in query
    assert "SELECT window_start, window_end" in query
    assert ") distinct_windows" not in query
    assert "rel_type, provenance, attributes" in query
    assert "ORDER BY window_start, window_end" in query
    assert "LIMIT 1001" in query


def test_graph_entity_query_returns_reachable_identity_and_presence() -> None:
    query = _semantic_graph_query(MEASUREMENT_CASE, {"view": "entities", "limit": 20})

    identity = "entity_type, entity_id, entity_id_attrs, scope, descriptive, source_tables"

    assert "greptime_private.semantic_entities" in query
    assert f"SELECT {identity}" in query
    assert "MIN(observed_at) AS first_observed_at" in query
    assert "MAX(observed_at) AS latest_observed_at" in query
    assert f"GROUP BY {identity}" in query
    assert "LIMIT 21" in query


def test_graph_entity_query_can_return_one_row_per_observation_window() -> None:
    query = _semantic_graph_query(
        MEASUREMENT_CASE,
        {"view": "entities", "bucket": "window", "entity_type": "k8s.pod"},
    )

    assert "SELECT window_start, window_end, entity_type" in query
    assert "GROUP BY window_start, window_end, entity_type" in query
    assert "ORDER BY window_start, window_end, entity_type, entity_id" in query
    assert "entity_type = 'k8s.pod'" in query
    assert "MIN(observed_at)" not in query


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            {"view": "relationships", "start_time": "2026-04-25T05:20:00+00:00"},
            "must be supplied together",
        ),
        (
            {
                "view": "relationships",
                "start_time": 1_777_094_400,
                "end_time": "2026-04-25T05:25:00+00:00",
            },
            "start_time must be an RFC3339 UTC timestamp",
        ),
        (
            {
                "view": "relationships",
                "start_time": "2026-04-25T05:20:00+00:00",
                "end_time": "not a timestamp",
            },
            "end_time must be an RFC3339 UTC timestamp",
        ),
        (
            {
                "view": "relationships",
                "start_time": "2026-04-25T05:20:00",
                "end_time": "2026-04-25T05:25:00+00:00",
            },
            "start_time must declare a UTC offset",
        ),
        (
            {
                "view": "relationships",
                "start_time": "2026-04-25T05:25:00+00:00",
                "end_time": "2026-04-25T05:25:00+00:00",
            },
            "must be non-empty",
        ),
        (
            {
                "view": "relationships",
                "start_time": "2026-04-25T05:10:00+00:00",
                "end_time": "2026-04-25T05:25:00+00:00",
            },
            "must stay within the audited incident window",
        ),
        (
            {"view": "entities", "bucket": "minute"},
            "bucket must be window",
        ),
    ],
)
def test_graph_query_rejects_invalid_time_and_bucket_scope(
    arguments: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(AgentError, match=message):
        _semantic_graph_query(MEASUREMENT_CASE, arguments)


def test_graph_tool_invocation_uses_gateway_window_without_changing_case_window() -> None:
    queries = []

    class Gateway:
        client = SimpleNamespace()
        semantic_graph_window = (1_752_918_720, 1_752_919_260)

        def execute(self, query: str, *, max_rows: int = 200) -> QueryResult:
            queries.append(query)
            assert max_rows == 100
            return QueryResult(query_id="provider", columns=[], rows=[], elapsed_seconds=0)

    case = CaseInput(
        case_token="aegis-transfer-001",
        time_start=1_752_918_758,
        time_end=1_752_919_238,
        alert_time=1_752_918_998,
    )
    session = agent_module.InvestigationSession(
        Gateway(),  # type: ignore[arg-type]
        case,
        Visibility.SEMANTIC_GRAPH,
        max_tool_calls=1,
        semantic_coverage={"graph": {"status": "relational"}},
    )

    session.invoke("query_semantic_graph", {"view": "relationships"})

    assert "observed_at >= '2025-07-19 09:52:00'" in queries[0]
    assert "observed_at < '2025-07-19 10:01:00'" in queries[0]
    assert case.time_start == 1_752_918_758
    assert case.time_end == 1_752_919_238


def test_graph_tool_asks_for_one_row_beyond_the_reported_row_cap() -> None:
    # The gateway only reports truncation when the result exceeds max_rows, so a
    # graph query whose SQL LIMIT equalled max_rows would present a clipped result
    # as complete and citable.
    observed: list[tuple[str, int]] = []

    class Gateway:
        client = SimpleNamespace()
        semantic_graph_window = None

        def execute(self, query: str, *, max_rows: int = 200) -> QueryResult:
            observed.append((query, max_rows))
            return QueryResult(query_id="provider", columns=[], rows=[], elapsed_seconds=0)

    session = agent_module.InvestigationSession(
        Gateway(),  # type: ignore[arg-type]
        MEASUREMENT_CASE,
        Visibility.SEMANTIC_GRAPH,
        max_tool_calls=2,
        semantic_coverage={"graph": {"status": "relational"}},
    )

    session.invoke("query_semantic_graph", {"view": "entities", "limit": 7})
    session.invoke("query_semantic_graph", {"view": "relationships", "limit": 5_000})

    assert [max_rows for _, max_rows in observed] == [7, MAX_QUERY_MAX_ROWS]
    assert "LIMIT 8" in observed[0][0]
    assert f"LIMIT {MAX_QUERY_MAX_ROWS + 1}" in observed[1][0]
