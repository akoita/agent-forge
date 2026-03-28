from __future__ import annotations

from typing import Any

from agent_forge.tools.base import Tool, ToolResult


class EchoTool(Tool):
    @property
    def name(self) -> str:
        return "echo_tool"

    @property
    def description(self) -> str:
        return "Echo a short string to demonstrate external plugin loading"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "Message to echo back",
                }
            },
            "required": ["message"],
        }

    async def execute(self, arguments: dict[str, Any], sandbox: Any) -> ToolResult:
        return ToolResult(output=str(arguments.get("message", "")))
