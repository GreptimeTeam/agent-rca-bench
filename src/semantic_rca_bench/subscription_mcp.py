from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import anyio
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="semantic-rca-subscription-mcp")
    parser.add_argument("--config", type=Path, required=True)
    return parser


def _read_config(path: Path) -> dict[str, Any]:
    config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(config.get("broker_url"), str):
        raise ValueError("subscription MCP config is missing broker_url")
    if not isinstance(config.get("broker_token"), str):
        raise ValueError("subscription MCP config is missing broker_token")
    if not isinstance(config.get("tools"), list):
        raise ValueError("subscription MCP config is missing tools")
    return config


def _call_broker(config: dict[str, Any], name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        str(config["broker_url"]),
        data=json.dumps({"tool": name, "arguments": arguments}).encode(),
        headers={
            "Authorization": f"Bearer {config['broker_token']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=130) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:2000]
        raise RuntimeError(f"tool broker rejected the request: {detail}") from error
    if not isinstance(payload, dict):
        raise RuntimeError("tool broker returned an invalid response")
    return payload


async def _run(config: dict[str, Any]) -> None:
    server = Server(
        "semantic_rca",
        instructions=(
            "Use these tools as the only source of incident evidence. Tool results include "
            "run-local query IDs and the remaining investigation budget."
        ),
    )
    tool_definitions = [
        types.Tool(
            name=str(tool["name"]),
            description=str(tool.get("description", "")),
            inputSchema=dict(tool["input_schema"]),
        )
        for tool in config["tools"]
        if isinstance(tool, dict)
    ]

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return tool_definitions

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        payload = await anyio.to_thread.run_sync(_call_broker, config, name, arguments)
        content = [types.TextContent(type="text", text=str(payload.get("content", "")))]
        remaining = payload.get("remaining")
        if remaining is not None:
            content.append(
                types.TextContent(
                    type="text",
                    text=(
                        f"Investigation budget: {remaining} tool calls remain. "
                        "Submit the required final output before the budget reaches zero."
                    ),
                )
            )
        return types.CallToolResult(
            content=content,
            isError=bool(payload.get("is_error", False)),
        )

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> None:
    args = _parser().parse_args()
    anyio.run(_run, _read_config(args.config))


if __name__ == "__main__":
    main()
