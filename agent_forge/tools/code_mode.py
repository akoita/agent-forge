"""Code Mode tools: progressive tool schema discovery and sandboxed script execution."""

from __future__ import annotations

import json
import time
import uuid
from typing import TYPE_CHECKING, Any

from agent_forge.tools.base import Tool, ToolResult

if TYPE_CHECKING:
    from agent_forge.agent.code_mode import HostToolBridge
    from agent_forge.sandbox.base import Sandbox
    from agent_forge.tools.base import ToolRegistry


class SearchToolsTool(Tool):
    """Search the registered host/MCP tool catalog on demand to avoid schema bloat."""

    def __init__(self, underlying_tools: ToolRegistry) -> None:
        self._underlying_tools = underlying_tools

    @property
    def name(self) -> str:
        return "search_tools"

    @property
    def description(self) -> str:
        return (
            "Search available built-in and external MCP tools by keyword and optionally inspect "
            "their JSON parameter schemas before calling them inside execute_code."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keyword or tool capability query (empty lists catalog).",
                    "default": "",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum matching tools to return (1..50).",
                    "default": 10,
                    "minimum": 1,
                    "maximum": 50,
                },
                "include_schema": {
                    "type": "boolean",
                    "description": "Include parameter JSON schemas in results.",
                    "default": True,
                },
            },
            "required": [],
        }

    @property
    def side_effecting(self) -> bool:
        return False

    async def execute(self, arguments: dict[str, Any], sandbox: Sandbox) -> ToolResult:
        """Return matching tool descriptors as compact JSON."""
        del sandbox
        from agent_forge.agent.code_mode import search_tool_definitions

        started = time.monotonic()
        query = str(arguments.get("query", ""))
        raw_limit = arguments.get("limit", 10)
        limit = raw_limit if isinstance(raw_limit, int) and not isinstance(raw_limit, bool) else 10
        raw_include = arguments.get("include_schema", True)
        include_schema = bool(raw_include) if isinstance(raw_include, bool) else True

        matches = search_tool_definitions(
            self._underlying_tools,
            query,
            limit=limit,
            include_schema=include_schema,
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return ToolResult(
            output=json.dumps({"tools": matches, "count": len(matches)}, indent=2),
            error=None,
            exit_code=0,
            execution_time_ms=elapsed_ms,
            metadata={"count": len(matches)},
        )


class ExecuteCodeTool(Tool):
    """Run short Python orchestration scripts in the sandbox using host-bridged tools."""

    def __init__(self, bridge: HostToolBridge) -> None:
        self._bridge = bridge

    @property
    def name(self) -> str:
        return "execute_code"

    @property
    def description(self) -> str:
        return (
            "Execute a short Python script inside the isolated sandbox with "
            "`call_tool(name, arguments)` and `list_tools()` helpers bridged safely back to the "
            "host through approval gates and the run journal. Print only the condensed result."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python 3 script using call_tool(name, arguments) and print().",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "Execution timeout in seconds (clamped to harness max).",
                    "minimum": 1,
                },
            },
            "required": ["code"],
        }

    async def execute(self, arguments: dict[str, Any], sandbox: Sandbox) -> ToolResult:
        """Execute *code* via :class:`~agent_forge.agent.code_mode.HostToolBridge`."""
        del sandbox
        code = str(arguments.get("code", ""))
        raw_timeout = arguments.get("timeout_seconds")
        timeout = (
            raw_timeout
            if isinstance(raw_timeout, int) and not isinstance(raw_timeout, bool)
            else None
        )
        parent_call_id = str(arguments.get("_tool_call_id") or f"code-{uuid.uuid4().hex[:8]}")
        return await self._bridge.execute_script(
            code,
            parent_call_id=parent_call_id,
            timeout_seconds=timeout,
        )
