"""read_file tool — reads file contents from the sandbox."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from agent_forge.tools.base import Tool, ToolResult, validate_path

if TYPE_CHECKING:
    from agent_forge.sandbox.base import Sandbox

_MAX_SIZE_BYTES = 100 * 1024  # 100 KB


class ReadFileTool(Tool):
    """Read the contents of a file at the given path."""

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return "Read the contents of a file at the given path"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path within the workspace",
                },
            },
            "required": ["path"],
        }

    async def execute(self, arguments: dict[str, Any], sandbox: Sandbox) -> ToolResult:
        """Read a file, truncating at 100KB."""
        path = arguments.get("path", "")
        if not path:
            return ToolResult(output="", error="Missing required argument: path", exit_code=1)

        try:
            resolved = validate_path(path)
        except ValueError as exc:
            return ToolResult(output="", error=str(exc), exit_code=1)

        start = time.monotonic()

        result = await sandbox.exec(f"cat {resolved}")
        elapsed_ms = int((time.monotonic() - start) * 1000)

        if result.exit_code != 0:
            return ToolResult(
                output="",
                error=result.stderr or f"Failed to read file: {resolved}",
                exit_code=result.exit_code,
                execution_time_ms=elapsed_ms,
            )

        content = result.stdout
        if len(content.encode("utf-8", errors="replace")) > _MAX_SIZE_BYTES:
            truncated = content[:_MAX_SIZE_BYTES].rsplit("\n", 1)[0]
            content = (
                f"{truncated}\n\n[WARNING: File truncated at 100KB. Total size exceeds limit.]"
            )

        return ToolResult(
            output=content,
            exit_code=0,
            execution_time_ms=elapsed_ms,
        )
