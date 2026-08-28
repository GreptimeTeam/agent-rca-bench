from __future__ import annotations

import hmac
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread

from semantic_rca_bench.agent import (
    AgentError,
    InvestigationSession,
    StructuredAgentResult,
    _incident_prompt,
    _investigation_tools,
    _submit_tool,
    _system_prompt,
)
from semantic_rca_bench.contracts import (
    AgentRun,
    AgentRunner,
    AgentUsage,
    CaseInput,
    Diagnosis,
    RejectedToolCall,
    Visibility,
)
from semantic_rca_bench.greptimedb.visibility import QueryGateway

SUBSCRIPTION_RUN_TIMEOUT_SECONDS = 30 * 60
_MCP_SERVER_NAME = "semantic_rca"
_PROVIDER_ENV_PREFIXES = (
    "ANTHROPIC_",
    "CLAUDE_",
    "CODEX_",
    "DEEPSEEK_",
    "OPENAI_",
)


class _ToolBroker:
    def __init__(self, session: InvestigationSession) -> None:
        self.session = session
        self.token = secrets.token_urlsafe(32)
        self.server: ThreadingHTTPServer | None = None
        self.thread: Thread | None = None
        self.invoke_lock = Lock()

    def __enter__(self) -> _ToolBroker:
        broker = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                authorization = self.headers.get("Authorization")
                if self.path != "/tool" or not broker._authorized(authorization):
                    self._write(403, {"error": "forbidden"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length < 1 or length > 1_000_000:
                        raise ValueError("invalid request size")
                    request = json.loads(self.rfile.read(length))
                    name = str(request.get("tool", ""))
                    arguments = request.get("arguments", {})
                    if not isinstance(arguments, dict):
                        raise ValueError("tool arguments must be an object")
                    with broker.invoke_lock:
                        result = broker.session.invoke(name, arguments)
                    self._write(
                        200,
                        {
                            "content": result.content,
                            "is_error": result.is_error,
                            "remaining": result.remaining,
                        },
                    )
                except Exception as error:
                    self._write(400, {"error": str(error)})

            def log_message(self, *_: object) -> None:
                return

            def _write(self, status: int, payload: dict[str, object]) -> None:
                body = json.dumps(payload, separators=(",", ":")).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)

    @property
    def url(self) -> str:
        if self.server is None:
            raise RuntimeError("tool broker has not started")
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}/tool"

    def _authorized(self, header: str | None) -> bool:
        return header is not None and hmac.compare_digest(header, f"Bearer {self.token}")


def run_subscription_agent(
    gateway: QueryGateway,
    case_input: CaseInput,
    visibility: Visibility,
    *,
    runner: AgentRunner,
    model: str,
    max_tool_calls: int = 24,
    semantic_coverage: dict[str, object] | None = None,
) -> AgentRun:
    tools = _investigation_tools(
        visibility,
        case_input.fault_taxonomy,
        semantic_coverage,
    )
    diagnosis_schema = dict(_submit_tool(case_input.fault_taxonomy)["input_schema"])
    required = list(diagnosis_schema["required"])
    if "onset_time" not in required:
        required.append("onset_time")
    diagnosis_schema["required"] = required
    result = run_structured_subscription_agent(
        gateway,
        case_input,
        visibility,
        runner=runner,
        model=model,
        system_prompt=_system_prompt(),
        user_prompt=_subscription_prompt(
            case_input,
            max_tool_calls,
            {str(tool["name"]) for tool in tools},
        ),
        investigation_tools=tools,
        output_schema=diagnosis_schema,
        validate_output=lambda value: Diagnosis.model_validate(value).model_dump(mode="json"),
        max_tool_calls=max_tool_calls,
        semantic_coverage=semantic_coverage,
    )
    diagnosis = Diagnosis.model_validate(result.output) if result.output is not None else None
    return AgentRun(
        run_id=uuid.uuid4().hex,
        visibility=visibility,
        model=model,
        runner=runner,
        diagnosis=diagnosis,
        error=result.error,
        tool_calls=result.tool_calls,
        rejected_tool_calls=result.rejected_tool_calls,
        tool_calls_requested=result.tool_calls_requested,
        tool_budget_exhausted=result.tool_budget_exhausted,
        usage=result.usage,
        elapsed_seconds=result.elapsed_seconds,
        responses=result.responses,
    )


def run_structured_subscription_agent(
    gateway: QueryGateway,
    case_input: CaseInput,
    visibility: Visibility,
    *,
    runner: AgentRunner,
    model: str,
    system_prompt: str,
    user_prompt: str,
    investigation_tools: list[dict[str, object]],
    output_schema: dict[str, object],
    validate_output: Callable[[object], dict[str, object]],
    max_tool_calls: int,
    semantic_coverage: dict[str, object] | None = None,
) -> StructuredAgentResult:
    if runner not in {AgentRunner.CODEX_SUBSCRIPTION, AgentRunner.CLAUDE_SUBSCRIPTION}:
        raise ValueError(f"unsupported subscription runner: {runner}")
    session = InvestigationSession(
        gateway,
        case_input,
        visibility,
        max_tool_calls=max_tool_calls,
        semantic_coverage=semantic_coverage,
        investigation_tools=investigation_tools,
    )
    started = time.monotonic()
    responses: list[dict[str, object]] = []
    usage = AgentUsage()
    rejected_tool_calls: list[RejectedToolCall] = []
    output: dict[str, object] | None = None
    error: str | None = None
    try:
        environment = _subscription_environment()
        if runner is AgentRunner.CODEX_SUBSCRIPTION:
            _require_codex_subscription(environment)
        else:
            _require_claude_subscription(environment)
        with (
            _ToolBroker(session) as broker,
            tempfile.TemporaryDirectory(prefix="semantic-rca-subscription-") as temp_directory,
        ):
            root = Path(temp_directory)
            mcp_config = root / "mcp-server.json"
            mcp_config.write_text(
                json.dumps(
                    {
                        "broker_url": broker.url,
                        "broker_token": broker.token,
                        "tools": investigation_tools,
                    },
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            mcp_config.chmod(0o600)
            if runner is AgentRunner.CODEX_SUBSCRIPTION:
                (
                    output_data,
                    responses,
                    usage,
                    rejected_tool_calls,
                    usage_error,
                ) = _run_codex(
                    root,
                    mcp_config,
                    output_schema,
                    f"{system_prompt}\n\n{user_prompt}",
                    model,
                    environment,
                    session.allowed_tools,
                )
                if usage_error is not None:
                    raise AgentError(usage_error)
            else:
                output_data, responses, usage, rejected_tool_calls = _run_claude(
                    root,
                    mcp_config,
                    output_schema,
                    user_prompt,
                    system_prompt,
                    model,
                    environment,
                    session.allowed_tools,
                )
        if session.tool_calls_requested == 0:
            error = "subscription agent completed without executing an investigation tool"
        else:
            output = validate_output(output_data)
    except Exception as caught:
        error = str(caught)
    all_rejected_tool_calls = [
        *getattr(session, "rejected_tool_calls", []),
        *rejected_tool_calls,
    ]
    return StructuredAgentResult(
        output=output,
        error=error,
        tool_calls=session.tool_calls,
        rejected_tool_calls=all_rejected_tool_calls,
        tool_calls_requested=session.tool_calls_requested + len(rejected_tool_calls),
        tool_budget_exhausted=session.tool_budget_exhausted,
        usage=usage,
        elapsed_seconds=time.monotonic() - started,
        responses=responses,
    )


def _subscription_prompt(
    case_input: CaseInput,
    max_tool_calls: int,
    allowed_tools: set[str],
) -> str:
    tool_names = ", ".join(sorted(allowed_tools))
    return (
        _incident_prompt(case_input, max_tool_calls)
        + "\n\nThe only MCP server is named semantic_rca. Its allowed tools for this run are: "
        + tool_names
        + ". Do not call semantic_sql or any other MCP server. Do not use shell, files, "
        "web search, browser, or any other tool. Return the final diagnosis as the JSON object "
        "required by the supplied output schema."
    )


def _subscription_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not any(key.startswith(prefix) for prefix in _PROVIDER_ENV_PREFIXES)
    }


def _require_codex_subscription(environment: dict[str, str]) -> None:
    result = subprocess.run(
        ["codex", "login", "status"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )
    status = result.stdout + result.stderr
    if result.returncode != 0 or "ChatGPT" not in status:
        hint = (
            "; CODEX_HOME is intentionally ignored for subscription isolation"
            if "CODEX_HOME" in os.environ and "CODEX_HOME" not in environment
            else ""
        )
        raise AgentError(
            "codex-subscription requires `codex login` with ChatGPT, not an API key" + hint
        )


def _require_claude_subscription(environment: dict[str, str]) -> None:
    result = subprocess.run(
        ["claude", "auth", "status"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )
    try:
        status = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise AgentError("unable to read Claude Code authentication status") from error
    method = str(status.get("authMethod", "")).lower()
    if result.returncode != 0 or not status.get("loggedIn"):
        hint = (
            "; CLAUDE_CONFIG_DIR is intentionally ignored for subscription isolation"
            if "CLAUDE_CONFIG_DIR" in os.environ and "CLAUDE_CONFIG_DIR" not in environment
            else ""
        )
        raise AgentError(
            "claude-subscription requires `claude auth login` with a Claude subscription" + hint
        )
    if method not in {"oauth", "oauth_token", "claude.ai", "subscription"}:
        raise AgentError(
            f"claude-subscription refuses non-subscription authentication: {method or 'unknown'}"
        )


def _run_codex(
    root: Path,
    mcp_config: Path,
    output_schema: dict[str, object],
    prompt: str,
    model: str,
    environment: dict[str, str],
    allowed_tools: set[str],
) -> tuple[
    dict[str, object],
    list[dict[str, object]],
    AgentUsage,
    list[RejectedToolCall],
    str | None,
]:
    schema_path = root / "output.schema.json"
    output_path = root / "output.json"
    schema_path.write_text(json.dumps(output_schema), encoding="utf-8")
    python = json.dumps(sys.executable)
    args = json.dumps(["-m", "semantic_rca_bench.subscription_mcp", "--config", str(mcp_config)])
    command = [
        "codex",
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--json",
        "--skip-git-repo-check",
        "--cd",
        str(root),
        "--model",
        model,
        "--approve-for-me",
        "--strict-config",
        "--output-schema",
        str(schema_path),
        "--output-last-message",
        str(output_path),
        "-c",
        'approval_policy="on-request"',
        "-c",
        f"mcp_servers.{_MCP_SERVER_NAME}.command={python}",
        "-c",
        f"mcp_servers.{_MCP_SERVER_NAME}.args={args}",
        "-c",
        f"mcp_servers.{_MCP_SERVER_NAME}.startup_timeout_sec=15",
        "-c",
        f"mcp_servers.{_MCP_SERVER_NAME}.tool_timeout_sec=150",
        "-",
    ]
    result = _run_process(command, prompt, root, environment)
    events = _json_lines(result.stdout, "Codex")
    rejected_tool_calls = _rejected_codex_tools(events, allowed_tools)
    if not output_path.is_file():
        raise AgentError("Codex did not write the structured output")
    output = _json_object(output_path.read_text(encoding="utf-8"), "Codex output")
    responses = _codex_responses(events)
    try:
        usage = _codex_usage(events)
    except AgentError as error:
        usage = AgentUsage()
        usage_error = str(error)
    else:
        usage_error = None
    return output, responses, usage, rejected_tool_calls, usage_error


def _run_claude(
    root: Path,
    mcp_config: Path,
    output_schema: dict[str, object],
    prompt: str,
    system_prompt: str,
    model: str,
    environment: dict[str, str],
    allowed_tools: set[str],
) -> tuple[
    dict[str, object],
    list[dict[str, object]],
    AgentUsage,
    list[RejectedToolCall],
]:
    claude_mcp_config = root / "claude-mcp.json"
    claude_mcp_config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    _MCP_SERVER_NAME: {
                        "type": "stdio",
                        "command": sys.executable,
                        "args": [
                            "-m",
                            "semantic_rca_bench.subscription_mcp",
                            "--config",
                            str(mcp_config),
                        ],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    claude_mcp_config.chmod(0o600)
    allowed = [f"mcp__{_MCP_SERVER_NAME}__{name}" for name in sorted(allowed_tools)]
    command = [
        "claude",
        "--print",
        "--output-format",
        "stream-json",
        "--verbose",
        "--no-session-persistence",
        "--disable-slash-commands",
        "--no-chrome",
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--mcp-config",
        str(claude_mcp_config),
        "--tools",
        "",
        "--allowedTools",
        *allowed,
        "--permission-mode",
        "dontAsk",
        "--model",
        model,
        "--system-prompt",
        system_prompt,
        "--json-schema",
        json.dumps(output_schema, separators=(",", ":")),
    ]
    isolated_environment = dict(environment)
    isolated_environment["CLAUDE_CODE_SKIP_PLUGIN_MCP_SERVERS"] = "1"
    result = _run_process(command, prompt, root, isolated_environment)
    events = _json_lines(result.stdout, "Claude Code")
    rejected_tool_calls = _rejected_claude_tools(events, allowed_tools)
    final = next((event for event in reversed(events) if event.get("type") == "result"), None)
    if final is None:
        raise AgentError("Claude Code did not return a final result event")
    if final.get("is_error"):
        raise AgentError(f"Claude Code failed: {str(final.get('result', 'unknown error'))[:2000]}")
    structured = final.get("structured_output")
    if isinstance(structured, dict):
        output = structured
    else:
        output = _json_object(str(final.get("result", "")), "Claude output")
    responses = _claude_responses(events)
    usage_data = final.get("usage") if isinstance(final.get("usage"), dict) else {}
    usage = AgentUsage(
        input_tokens=sum(
            int(usage_data.get(field, 0))
            for field in (
                "input_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
            )
        ),
        output_tokens=int(usage_data.get("output_tokens", 0)),
    )
    return output, responses, usage, rejected_tool_calls


def _run_process(
    command: list[str],
    prompt: str,
    cwd: Path,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            input=prompt,
            check=False,
            capture_output=True,
            text=True,
            cwd=cwd,
            env=environment,
            timeout=SUBSCRIPTION_RUN_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise AgentError(
            f"subscription agent exceeded {SUBSCRIPTION_RUN_TIMEOUT_SECONDS} seconds"
        ) from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout)[-4000:]
        raise AgentError(f"subscription agent exited with {result.returncode}: {detail}")
    return result


def _json_lines(output: str, provider: str) -> list[dict[str, object]]:
    events = []
    for line in output.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise AgentError(f"{provider} emitted invalid JSONL") from error
        if not isinstance(event, dict):
            raise AgentError(f"{provider} emitted a non-object JSONL event")
        events.append(event)
    return events


def _json_object(output: str, label: str) -> dict[str, object]:
    try:
        value = json.loads(output)
    except json.JSONDecodeError as error:
        raise AgentError(f"{label} is not valid JSON") from error
    if not isinstance(value, dict):
        raise AgentError(f"{label} must be a JSON object")
    return value


def _rejected_codex_tools(
    events: list[dict[str, object]],
    allowed_tools: set[str],
) -> list[RejectedToolCall]:
    violations = []
    rejected = []
    forbidden_types = {
        "command_execution",
        "file_change",
        "web_search",
        "image_generation",
        "computer_use",
    }
    for event in events:
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type", ""))
        if item_type in forbidden_types:
            violations.append(item_type)
        if item_type == "mcp_tool_call":
            server = str(item.get("server", item.get("server_name", "")))
            tool = str(item.get("tool", item.get("name", "")))
            if server != _MCP_SERVER_NAME or tool not in allowed_tools:
                arguments = item.get("arguments", item.get("input"))
                rejected.append(
                    RejectedToolCall(
                        tool_name=f"mcp__{server}__{tool}",
                        input=arguments if isinstance(arguments, dict) else {},
                        error=("tool is outside the strict MCP configuration and was not executed"),
                    )
                )
    if violations:
        raise AgentError(
            f"Codex protocol violation: disallowed tools used: {sorted(set(violations))}"
        )
    return rejected


def _rejected_claude_tools(
    events: list[dict[str, object]],
    allowed_tools: set[str],
) -> list[RejectedToolCall]:
    allowed = {f"mcp__{_MCP_SERVER_NAME}__{name}" for name in allowed_tools}
    rejected = []
    for event in events:
        if event.get("type") != "assistant":
            continue
        message = event.get("message")
        content = message.get("content", []) if isinstance(message, dict) else []
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = str(block.get("name", ""))
                if name != "StructuredOutput" and name not in allowed:
                    arguments = block.get("input")
                    rejected.append(
                        RejectedToolCall(
                            tool_name=name,
                            input=arguments if isinstance(arguments, dict) else {},
                            error=(
                                "tool is outside the strict MCP configuration and was not executed"
                            ),
                        )
                    )
    return rejected


def _codex_responses(events: list[dict[str, object]]) -> list[dict[str, object]]:
    responses = []
    for event in events:
        if event.get("type") != "item.completed" or not isinstance(event.get("item"), dict):
            continue
        responses.append({"content": [event["item"]]})
    return responses


def _codex_usage(events: list[dict[str, object]]) -> AgentUsage:
    completed = [
        event["usage"]
        for event in events
        if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict)
    ]
    if len(completed) != 1:
        raise AgentError(
            "Codex must return exactly one cumulative turn.completed usage event, "
            f"got {len(completed)}"
        )
    usage = completed[0]
    return AgentUsage(
        input_tokens=int(usage.get("input_tokens", 0)),
        output_tokens=int(usage.get("output_tokens", 0)),
    )


def _claude_responses(events: list[dict[str, object]]) -> list[dict[str, object]]:
    responses = []
    for event in events:
        if event.get("type") != "assistant" or not isinstance(event.get("message"), dict):
            continue
        content = event["message"].get("content", [])
        if isinstance(content, list):
            responses.append({"content": content})
    return responses
