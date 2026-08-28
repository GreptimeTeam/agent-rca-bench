import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import semantic_rca_bench.subscription as subscription
from semantic_rca_bench.agent import AgentError, ToolInvocation
from semantic_rca_bench.contracts import (
    AgentRunner,
    AgentUsage,
    CaseInput,
    ToolTrace,
    Visibility,
)

DIAGNOSIS = {
    "affected_component": "checkout",
    "fault_category": "cpu",
    "fault_type": "cpu",
    "confidence": 0.8,
    "evidence": [],
    "alternative_candidates": [],
    "explanation": "CPU saturation is the best-supported cause.",
}


def test_subscription_environment_removes_usage_billed_credentials(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("CODEX_API_KEY", "codex-secret")
    monkeypatch.setenv("CODEX_HOME", "/tmp/untrusted-codex-home")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/tmp/untrusted-claude-config")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:7890")

    environment = subscription._subscription_environment()

    assert environment["PATH"] == "/usr/bin"
    assert environment["https_proxy"] == "http://127.0.0.1:7890"
    assert "ANTHROPIC_API_KEY" not in environment
    assert "ANTHROPIC_BASE_URL" not in environment
    assert "DEEPSEEK_API_KEY" not in environment
    assert "OPENAI_API_KEY" not in environment
    assert "OPENAI_BASE_URL" not in environment
    assert "CODEX_API_KEY" not in environment
    assert "CODEX_HOME" not in environment
    assert "CLAUDE_CONFIG_DIR" not in environment


def test_subscription_prompt_names_the_only_allowed_mcp_surface() -> None:
    prompt = subscription._subscription_prompt(
        CaseInput(case_token="case", time_start=100, time_end=200, alert_time=200),
        12,
        {"execute_sql", "describe_table"},
    )

    assert "only MCP server is named semantic_rca" in prompt
    assert "describe_table, execute_sql" in prompt
    assert "Do not call semantic_sql" in prompt


def test_subscription_auth_rejects_api_key_methods(monkeypatch) -> None:
    def fake_run(command, **kwargs):
        if command[0] == "codex":
            return subprocess.CompletedProcess(command, 0, "", "Logged in using an API key\n")
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                {
                    "loggedIn": True,
                    "authMethod": "api_key",
                    "apiProvider": "firstParty",
                }
            ),
            "",
        )

    monkeypatch.setattr(subscription.subprocess, "run", fake_run)

    with pytest.raises(AgentError, match="ChatGPT"):
        subscription._require_codex_subscription({})
    with pytest.raises(AgentError, match="non-subscription"):
        subscription._require_claude_subscription({})


def test_tool_broker_requires_token_and_serializes_invocations() -> None:
    calls = []

    class Session:
        def invoke(self, name, arguments):
            calls.append((name, arguments))
            return ToolInvocation(content='{"rows":[[1]]}', is_error=False, remaining=3)

    with subscription._ToolBroker(Session()) as broker:  # type: ignore[arg-type]
        unauthorized = urllib.request.Request(broker.url, data=b"{}", method="POST")
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(unauthorized)
        assert error.value.code == 403

        request = urllib.request.Request(
            broker.url,
            data=json.dumps({"tool": "execute_sql", "arguments": {"query": "SELECT 1"}}).encode(),
            headers={
                "Authorization": f"Bearer {broker.token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            payload = json.loads(response.read())

    assert calls == [("execute_sql", {"query": "SELECT 1"})]
    assert payload == {
        "content": '{"rows":[[1]]}',
        "is_error": False,
        "remaining": 3,
    }


def test_subscription_mcp_exposes_only_configured_tools(tmp_path) -> None:
    calls = []

    class Session:
        def invoke(self, name, arguments):
            calls.append((name, arguments))
            return ToolInvocation(
                content='{"columns":["value"],"rows":[[1]]}',
                is_error=False,
                remaining=2,
            )

    async def exercise(config: Path) -> None:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "semantic_rca_bench.subscription_mcp",
                "--config",
                str(config),
            ],
        )
        async with (
            stdio_client(parameters) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as client,
        ):
            await client.initialize()
            tools = await client.list_tools()
            result = await client.call_tool(
                "execute_sql",
                {"query": "SELECT 1"},
            )
        assert [tool.name for tool in tools.tools] == ["execute_sql"]
        assert result.isError is False
        assert "Investigation budget: 2 tool calls remain" in str(result.content)

    with subscription._ToolBroker(Session()) as broker:  # type: ignore[arg-type]
        config = tmp_path / "mcp-config.json"
        config.write_text(
            json.dumps(
                {
                    "broker_url": broker.url,
                    "broker_token": broker.token,
                    "tools": [
                        {
                            "name": "execute_sql",
                            "description": "Execute one read-only SQL statement.",
                            "input_schema": {
                                "type": "object",
                                "properties": {"query": {"type": "string"}},
                                "required": ["query"],
                            },
                        }
                    ],
                }
            )
        )
        anyio.run(exercise, config)

    assert calls == [("execute_sql", {"query": "SELECT 1"})]


def test_codex_runner_uses_isolated_config_and_parses_usage(monkeypatch, tmp_path) -> None:
    captured = {}

    def fake_process(command, prompt, cwd, environment):
        captured.update(
            command=command,
            prompt=prompt,
            cwd=cwd,
            environment=environment,
        )
        output = Path(command[command.index("--output-last-message") + 1])
        output.write_text(json.dumps(DIAGNOSIS))
        events = [
            {
                "type": "item.completed",
                "item": {
                    "type": "mcp_tool_call",
                    "server": "semantic_rca",
                    "tool": "execute_sql",
                },
            },
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": json.dumps(DIAGNOSIS)},
            },
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 120, "output_tokens": 30},
            },
        ]
        return subprocess.CompletedProcess(
            command,
            0,
            "\n".join(json.dumps(event) for event in events),
            "",
        )

    monkeypatch.setattr(subscription, "_run_process", fake_process)
    config = tmp_path / "mcp.json"
    config.write_text("{}")

    diagnosis, responses, usage, rejected = subscription._run_codex(
        tmp_path,
        config,
        {"type": "object"},
        "incident prompt",
        "gpt-5.6-luna",
        {"PATH": "/usr/bin"},
        {"execute_sql"},
    )

    assert diagnosis == DIAGNOSIS
    assert rejected == []
    assert usage == AgentUsage(input_tokens=120, output_tokens=30)
    assert responses[0]["content"][0]["type"] == "mcp_tool_call"
    assert "--ignore-user-config" in captured["command"]
    assert "--approve-for-me" in captured["command"]
    assert "--strict-config" in captured["command"]
    assert 'approval_policy="on-request"' in captured["command"]
    assert "--sandbox" not in captured["command"]
    assert captured["environment"] == {"PATH": "/usr/bin"}


def test_codex_runner_records_unconfigured_mcp_attempt(monkeypatch, tmp_path) -> None:
    def fake_process(command, prompt, cwd, environment):
        output = Path(command[command.index("--output-last-message") + 1])
        output.write_text(json.dumps(DIAGNOSIS))
        events = [
            {
                "type": "item.completed",
                "item": {
                    "type": "mcp_tool_call",
                    "server": "semantic_rca",
                    "tool": "list_mcp_resources",
                    "arguments": {},
                },
            },
            {"type": "turn.completed", "usage": {}},
        ]
        return subprocess.CompletedProcess(
            command,
            0,
            "\n".join(json.dumps(event) for event in events),
            "",
        )

    monkeypatch.setattr(subscription, "_run_process", fake_process)
    config = tmp_path / "mcp.json"
    config.write_text("{}")

    _, _, _, rejected = subscription._run_codex(
        tmp_path,
        config,
        {"type": "object"},
        "incident prompt",
        "gpt-5.6-terra",
        {},
        {"execute_sql"},
    )

    assert len(rejected) == 1
    assert rejected[0].tool_name == "mcp__semantic_rca__list_mcp_resources"


@pytest.mark.parametrize("event_count", [0, 2])
def test_codex_usage_requires_one_cumulative_turn_event(event_count) -> None:
    events = [
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 120, "output_tokens": 30},
        }
        for _ in range(event_count)
    ]

    with pytest.raises(AgentError, match="exactly one cumulative"):
        subscription._codex_usage(events)


def test_claude_runner_disables_builtin_tools_and_parses_structured_output(
    monkeypatch,
    tmp_path,
) -> None:
    captured = {}

    def fake_process(command, prompt, cwd, environment):
        captured["command"] = command
        captured["environment"] = environment
        events = [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "mcp__semantic_rca__execute_sql",
                            "input": {"query": "SELECT 1"},
                        },
                        {
                            "type": "tool_use",
                            "name": "StructuredOutput",
                            "input": DIAGNOSIS,
                        },
                    ]
                },
            },
            {
                "type": "result",
                "is_error": False,
                "structured_output": DIAGNOSIS,
                "usage": {
                    "input_tokens": 80,
                    "cache_creation_input_tokens": 40,
                    "cache_read_input_tokens": 160,
                    "output_tokens": 20,
                },
            },
        ]
        return subprocess.CompletedProcess(
            command,
            0,
            "\n".join(json.dumps(event) for event in events),
            "",
        )

    monkeypatch.setattr(subscription, "_run_process", fake_process)
    config = tmp_path / "mcp.json"
    config.write_text("{}")

    diagnosis, responses, usage, rejected = subscription._run_claude(
        tmp_path,
        config,
        {"type": "object"},
        "incident prompt",
        "system prompt",
        "sonnet",
        {},
        {"execute_sql"},
    )

    command = captured["command"]
    assert diagnosis == DIAGNOSIS
    assert rejected == []
    assert usage == AgentUsage(input_tokens=280, output_tokens=20)
    assert responses[0]["content"][0]["name"] == "mcp__semantic_rca__execute_sql"
    assert command[command.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in command
    assert "--no-session-persistence" in command
    assert "--max-turns" not in command
    assert command[command.index("--system-prompt") + 1] == "system prompt"
    assert captured["environment"]["CLAUDE_CODE_SKIP_PLUGIN_MCP_SERVERS"] == "1"


def test_claude_runner_records_unconfigured_tool_attempt(monkeypatch, tmp_path) -> None:
    def fake_process(command, prompt, cwd, environment):
        events = [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "mcp__semantic_sql__execute_sql",
                            "input": {"query": "SELECT 1"},
                        }
                    ]
                },
            },
            {
                "type": "result",
                "is_error": False,
                "structured_output": DIAGNOSIS,
                "usage": {},
            },
        ]
        return subprocess.CompletedProcess(
            command,
            0,
            "\n".join(json.dumps(event) for event in events),
            "",
        )

    monkeypatch.setattr(subscription, "_run_process", fake_process)
    config = tmp_path / "mcp.json"
    config.write_text("{}")

    _, _, _, rejected = subscription._run_claude(
        tmp_path,
        config,
        {"type": "object"},
        "incident prompt",
        "system prompt",
        "sonnet",
        {},
        {"execute_sql"},
    )

    assert len(rejected) == 1
    assert rejected[0].tool_name == "mcp__semantic_sql__execute_sql"
    assert rejected[0].input == {"query": "SELECT 1"}


def test_subscription_agent_records_runner_without_api_client(monkeypatch) -> None:
    session = SimpleNamespace(
        allowed_tools={"execute_sql"},
        tool_calls=[],
        tool_calls_requested=1,
        tool_budget_exhausted=False,
    )
    monkeypatch.setattr(subscription, "_require_codex_subscription", lambda _: None)
    monkeypatch.setattr(subscription, "InvestigationSession", lambda *args, **kwargs: session)
    captured = {}

    def fake_run_codex(*args, **kwargs):
        captured["prompt"] = args[3]
        return (
            DIAGNOSIS,
            [],
            AgentUsage(input_tokens=10, output_tokens=5),
            [],
        )

    monkeypatch.setattr(subscription, "_run_codex", fake_run_codex)
    gateway = SimpleNamespace(client=SimpleNamespace())

    run = subscription.run_subscription_agent(
        gateway,  # type: ignore[arg-type]
        CaseInput(
            case_token="case",
            time_start=100,
            time_end=200,
            alert_time=200,
            fault_taxonomy=["cpu"],
        ),
        Visibility.RAW,
        runner=AgentRunner.CODEX_SUBSCRIPTION,
        model="gpt-5.6-luna",
    )

    assert run.runner is AgentRunner.CODEX_SUBSCRIPTION
    assert run.model == "gpt-5.6-luna"
    assert run.usage.input_tokens == 10
    assert "You are the on-call SRE" in captured["prompt"]
    assert "The only MCP server is named semantic_rca" in captured["prompt"]


def test_subscription_taxonomy_violation_is_a_recorded_answer(monkeypatch) -> None:
    session = SimpleNamespace(
        allowed_tools={"execute_sql"},
        tool_calls=[],
        tool_calls_requested=1,
        tool_budget_exhausted=False,
    )
    diagnosis = {**DIAGNOSIS, "fault_type": "outside-taxonomy"}
    monkeypatch.setattr(subscription, "_require_codex_subscription", lambda _: None)
    monkeypatch.setattr(subscription, "InvestigationSession", lambda *args, **kwargs: session)
    monkeypatch.setattr(
        subscription,
        "_run_codex",
        lambda *args, **kwargs: (diagnosis, [], AgentUsage(), []),
    )

    run = subscription.run_subscription_agent(
        SimpleNamespace(client=SimpleNamespace()),  # type: ignore[arg-type]
        CaseInput(
            case_token="case",
            time_start=100,
            time_end=200,
            alert_time=200,
            fault_taxonomy=["cpu"],
        ),
        Visibility.RAW,
        runner=AgentRunner.CODEX_SUBSCRIPTION,
        model="gpt-5.6-luna",
    )

    assert run.diagnosis is not None
    assert run.diagnosis.fault_type == "outside-taxonomy"


def test_rejected_tools_do_not_exhaust_investigation_budget(monkeypatch) -> None:
    session = SimpleNamespace(
        allowed_tools={"execute_sql"},
        tool_calls=[ToolTrace(tool_name="execute_sql", input={}) for _ in range(48)],
        tool_calls_requested=1,
        tool_budget_exhausted=False,
    )
    rejected = [
        {"tool_name": "mcp__other__tool", "input": {}, "error": "unavailable"} for _ in range(48)
    ]
    monkeypatch.setattr(subscription, "_require_codex_subscription", lambda _: None)
    monkeypatch.setattr(subscription, "InvestigationSession", lambda *args, **kwargs: session)
    monkeypatch.setattr(
        subscription,
        "_run_codex",
        lambda *args, **kwargs: (DIAGNOSIS, [], AgentUsage(), rejected),
    )

    run = subscription.run_subscription_agent(
        SimpleNamespace(client=SimpleNamespace()),  # type: ignore[arg-type]
        CaseInput(case_token="case", time_start=100, time_end=200, alert_time=200),
        Visibility.RAW,
        runner=AgentRunner.CODEX_SUBSCRIPTION,
        model="gpt-5.6-luna",
        max_tool_calls=48,
    )

    assert run.tool_budget_exhausted is False


def test_subscription_agent_records_failure_without_investigation(monkeypatch) -> None:
    session = SimpleNamespace(
        allowed_tools={"execute_sql"},
        tool_calls=[],
        tool_calls_requested=0,
        tool_budget_exhausted=False,
    )
    monkeypatch.setattr(subscription, "_require_codex_subscription", lambda _: None)
    monkeypatch.setattr(subscription, "InvestigationSession", lambda *args, **kwargs: session)
    monkeypatch.setattr(
        subscription,
        "_run_codex",
        lambda *args, **kwargs: (
            DIAGNOSIS,
            [],
            AgentUsage(input_tokens=10, output_tokens=5),
            [],
        ),
    )

    run = subscription.run_subscription_agent(
        SimpleNamespace(client=SimpleNamespace()),  # type: ignore[arg-type]
        CaseInput(
            case_token="case",
            time_start=100,
            time_end=200,
            alert_time=200,
            fault_taxonomy=["cpu"],
        ),
        Visibility.RAW,
        runner=AgentRunner.CODEX_SUBSCRIPTION,
        model="gpt-5.6-luna",
    )

    assert run.diagnosis is None
    assert run.error == "subscription agent completed without executing an investigation tool"
