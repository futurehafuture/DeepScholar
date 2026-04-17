"""
MCP client for DeepScholar.

Uses the `mcp` Python library for proper stdio transport (Content-Length framing).
Discovers tools from all configured servers at startup, then filters per phase
using PHASE_TOOL_PERMISSIONS.

The LLM receives tools in OpenAI function-call format:
  {"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}

Tool names discovered from MCP servers plus the internal `transition_to_phase` tool
(defined in state_machine.py) together form the full tool set.
"""

import asyncio
import json
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent.state_machine import COMPLETE_TOOL, TRANSITION_TOOL


# Per-phase tool allowlists. Tools not in this list are hidden from the LLM.
# transition_to_phase is always injected regardless of phase.
PHASE_TOOL_PERMISSIONS: dict[str, list[str]] = {
    # arxiv server tools: search_papers, download_paper, read_paper, get_abstract, list_papers
    # filesystem server tools: read_file, write_file, list_directory, create_directory
    "survey":     ["search_papers", "web_search", "read_file", "write_file", "create_directory"],
    "literature": ["search_papers", "download_paper", "read_paper", "get_abstract", "read_file", "write_file"],
    "arguments":  ["read_file", "write_file"],
    "innovation": ["read_file", "write_file"],
    "experiment": ["execute_python", "read_file", "write_file", "list_directory", "create_directory"],
    "analysis":   ["execute_python", "read_file", "write_file"],
    "writing":    ["read_file", "write_file", "list_directory"],
}


class MCPClient:
    """
    Manages MCP server connections and tool dispatch.

    Call `await client.start()` before use, `await client.stop()` when done.
    For convenience, use as an async context manager.
    """

    def __init__(self, config_path: str = "mcp_servers/servers.yaml"):
        self.config_path = config_path
        # tool_name → {"schema": <OpenAI tool dict>, "server": <server_name>}
        self._tools: dict[str, dict] = {}
        self._sessions: dict[str, ClientSession] = {}
        self._exit_stack = AsyncExitStack()

    async def start(self) -> None:
        """Connect to all configured MCP servers and discover their tools."""
        config = yaml.safe_load(Path(self.config_path).read_text(encoding="utf-8"))
        servers = config.get("servers", {})

        for server_name, server_config in servers.items():
            command: list[str] = server_config["command"]
            env: dict[str, str] | None = server_config.get("env")

            params = StdioServerParameters(
                command=command[0],
                args=command[1:],
                env=env,
            )
            try:
                read, write = await self._exit_stack.enter_async_context(
                    stdio_client(params)
                )
                session = await self._exit_stack.enter_async_context(
                    ClientSession(read, write)
                )
                await session.initialize()
                self._sessions[server_name] = session

                tools_response = await session.list_tools()
                for tool in tools_response.tools:
                    self._tools[tool.name] = {
                        "schema": self._to_openai_schema(tool),
                        "server": server_name,
                    }
                print(
                    f"[MCP] Connected to '{server_name}': "
                    f"{[t.name for t in tools_response.tools]}"
                )
            except Exception as exc:
                print(f"[MCP] Warning: could not connect to '{server_name}': {exc}")

        print(f"[MCP] Total tools discovered: {list(self._tools.keys())}")

    async def stop(self) -> None:
        await self._exit_stack.aclose()

    async def __aenter__(self) -> "MCPClient":
        await self.start()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.stop()

    def get_tools_for_phase(self, phase: str) -> list[dict]:
        """Return the OpenAI-format tool list for the given phase."""
        allowed = PHASE_TOOL_PERMISSIONS.get(phase, [])
        tools = [TRANSITION_TOOL]  # always available
        if phase == "writing":
            tools.append(COMPLETE_TOOL)  # terminal tool, writing phase only
        for name in allowed:
            if name in self._tools:
                tools.append(self._tools[name]["schema"])
        return tools

    async def call_tool(self, tool_name: str, tool_input: dict) -> str:
        """Dispatch a tool call to the appropriate MCP server."""
        if tool_name not in self._tools:
            return f"错误：工具 '{tool_name}' 不存在或当前阶段不可用"

        server_name = self._tools[tool_name]["server"]
        session = self._sessions.get(server_name)
        if session is None:
            return f"错误：服务器 '{server_name}' 未连接"

        result = await session.call_tool(tool_name, tool_input)

        # Extract text content from result
        texts = []
        for item in result.content:
            if hasattr(item, "text"):
                texts.append(item.text)
            else:
                texts.append(str(item))
        return "\n".join(texts) if texts else "(no output)"

    @staticmethod
    def _to_openai_schema(tool: Any) -> dict:
        """Convert an MCP tool definition to OpenAI function-call format."""
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": tool.inputSchema if tool.inputSchema else {"type": "object", "properties": {}},
            },
        }
