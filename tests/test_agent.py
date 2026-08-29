import json
from types import SimpleNamespace

import semantic_rca_bench.agent as agent_module
from semantic_rca_bench.agent import (
    SUBMIT_TOOL,
    AgentError,
    _agent_tools,
    _anthropic_client,
    _catalog_search_output,
    _citation_output,
    _describe_table_tool,
    _execute_sql_tool,
    _incident_prompt,
    _search_table_semantics_tool,
    _semantic_graph_output,
    _semantic_graph_query,
    _semantic_graph_tool,
    _submit_tool,
    _system_prompt,
    run_agent,
)
from semantic_rca_bench.contracts import CaseInput, DatabaseLoad, QueryResult, Visibility
from semantic_rca_bench.inspect import summarize_semantic_surfaces


def test_deepseek_model_uses_compatible_anthropic_endpoint(monkeypatch) -> None:
    calls = []

    def fake_client(**kwargs):
        calls.append(kwargs)
        return kwargs

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    monkeypatch.setattr(agent_module.anthropic, "Anthropic", fake_client)

    client = _anthropic_client("deepseek-v4-flash")

    assert client == {
        "api_key": "test-deepseek-key",
        "base_url": "https://api.deepseek.com/anthropic",
    }
    assert calls == [client]


def test_non_deepseek_model_uses_anthropic_endpoint(monkeypatch) -> None:
    calls = []

    def fake_client(**kwargs):
        calls.append(kwargs)
        return kwargs

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setattr(agent_module.anthropic, "Anthropic", fake_client)

    client = _anthropic_client("claude-sonnet-5")

    assert client == {"api_key": "test-anthropic-key"}
    assert calls == [client]


def test_tools_expose_only_allowed_semantic_capabilities() -> None:
    raw_profile = str(_describe_table_tool(Visibility.RAW)["description"])
    table_profile = str(_describe_table_tool(Visibility.TABLE_SEMANTICS)["description"])
    graph_sql = str(_execute_sql_tool(Visibility.SEMANTIC_GRAPH)["description"])
    raw_sql = str(_execute_sql_tool(Visibility.RAW)["description"])

    assert "semantic metadata" not in raw_profile
    assert "semantic metadata" in table_profile
    assert "semantic_entities" not in raw_sql
    assert "semantic_entities" in graph_sql
    assert "semantic_relationships" in graph_sql
    assert "entity_id_attrs" in graph_sql
    assert "confidence is derivation certainty" in graph_sql
    assert "Missing edges" in graph_sql
    assert "metadata_quality" in table_profile
    assert "entity_declarations" in table_profile


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
    assert {"causal_scope", "causal_operation", "mechanism_code"} <= set(schema["required"])
    assert schema["properties"]["causal_scope"]["enum"] == [
        "component",
        "dependency_edge",
    ]
    assert "call_path_delay" in schema["properties"]["mechanism_code"]["enum"]
    evidence = schema["properties"]["evidence"]["items"]
    assert "claim_types" in evidence["required"]


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
    table_names = {tool["name"] for tool in _agent_tools(Visibility.TABLE_SEMANTICS, [], None)}

    assert "search_table_semantics" not in raw_names
    assert "search_table_semantics" in table_names


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
        "affected_component": "checkout",
        "causal_dependency": None,
        "causal_scope": "component",
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


def test_deepseek_uses_automatic_cache_and_counts_native_usage(monkeypatch) -> None:
    diagnosis = {
        "affected_component": "checkout",
        "causal_dependency": None,
        "causal_scope": "component",
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
        max_tool_calls=2,
    )

    assert result.usage.input_tokens == 20
    assert result.usage.output_tokens == 5
    assert "cache_control" not in requests[0]


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
        max_tool_calls=2,
    )

    assert result.error == "invalid provider response: provider response has no usage object"
    assert result.responses == [{"content": []}]
    assert result.tool_calls == []


def test_valid_final_output_records_same_response_investigation_calls_as_rejected(
    monkeypatch,
) -> None:
    diagnosis = {
        "affected_component": "checkout",
        "causal_dependency": None,
        "causal_scope": "component",
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
            "limit": 500,
        },
    )

    assert "greptime_private.semantic_relationships" in query
    assert "SELECT window_start, window_end" in query
    assert "MAX(request_count) AS request_count" in query
    assert "GROUP BY window_start, window_end, src_type, src_id, dst_type, dst_id" in query
    assert "observed_at >= '2026-04-25 05:18:12'" in query
    assert "observed_at < '2026-04-25 05:28:12'" in query
    assert "src_id = 'checkout'' OR true'" in query
    assert "LIMIT 200" in query


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


def test_graph_tool_invocation_uses_gateway_window_without_changing_case_window() -> None:
    queries = []

    class Gateway:
        client = SimpleNamespace()
        semantic_graph_window = (1_752_918_720, 1_752_919_260)

        def execute(self, query: str) -> QueryResult:
            queries.append(query)
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
