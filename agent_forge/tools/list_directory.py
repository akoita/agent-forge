"""list_directory tool — lists files and directories in the sandbox."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from agent_forge.tools.base import Tool, ToolResult, validate_path

if TYPE_CHECKING:
    from agent_forge.sandbox.base import Sandbox


class ListDirectoryTool(Tool):
    """List files and directories at the given path."""

    @property
    def name(self) -> str:
        return "list_directory"

    @property
    def description(self) -> str:
        return "List files and directories at the given path"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to list (default: /workspace)",
                    "default": "/workspace",
                },
                "recursive": {
                    "type": "boolean",
                    "description": "List recursively (default: false)",
                    "default": False,
                },
                "max_depth": {
                    "type": "integer",
                    "description": "Maximum depth for recursive listing (default: 3)",
                    "default": 3,
                },
            },
        }

    async def execute(self, arguments: dict[str, Any], sandbox: Sandbox) -> ToolResult:
        """List directory contents using find."""
        path = arguments.get("path", "/workspace")
        recursive = arguments.get("recursive", False)
        max_depth = arguments.get("max_depth", 3)

        try:
            resolved = validate_path(path)
        except ValueError as exc:
            return ToolResult(output="", error=str(exc), exit_code=1)

        start = time.monotonic()

        if recursive:
            cmd = f"find {resolved} -maxdepth {max_depth} -type f -o -type d | sort"
        else:
            cmd = f"find {resolved} -maxdepth 1 -type f -o -type d | sort"

        result = await sandbox.exec(cmd)
        elapsed_ms = int((time.monotonic() - start) * 1000)

        if result.exit_code != 0:
            return ToolResult(
                output="",
                error=result.stderr or f"Failed to list directory: {resolved}",
                exit_code=result.exit_code,
                execution_time_ms=elapsed_ms,
            )

        return ToolResult(
            output=result.stdout,
            exit_code=0,
            execution_time_ms=elapsed_ms,
        )
