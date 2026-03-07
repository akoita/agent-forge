"""write_file tool — writes content to a file in the sandbox."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from agent_forge.tools.base import Tool, ToolResult, validate_path

if TYPE_CHECKING:
    from agent_forge.sandbox.base import Sandbox


class WriteFileTool(Tool):
    """Create or overwrite a file at the given path with the provided content."""

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return "Create or overwrite a file at the given path with the provided content"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path within the workspace",
                },
                "content": {
                    "type": "string",
                    "description": "File content to write",
                },
            },
            "required": ["path", "content"],
        }

    async def execute(self, arguments: dict[str, Any], sandbox: Sandbox) -> ToolResult:
        """Write content to a file, creating parent directories."""
        path = arguments.get("path", "")
        content = arguments.get("content", "")

        if not path:
            return ToolResult(output="", error="Missing required argument: path", exit_code=1)

        try:
            resolved = validate_path(path)
        except ValueError as exc:
            return ToolResult(output="", error=str(exc), exit_code=1)

        start = time.monotonic()

        # Create parent directories
        parent = "/".join(resolved.rsplit("/", 1)[:-1])
        if parent:
            mkdir_result = await sandbox.exec(f"mkdir -p {parent}")
            if mkdir_result.exit_code != 0:
                elapsed_ms = int((time.monotonic() - start) * 1000)
                return ToolResult(
                    output="",
                    error=mkdir_result.stderr or f"Failed to create directory: {parent}",
                    exit_code=mkdir_result.exit_code,
                    execution_time_ms=elapsed_ms,
                )

        # Write the file
        await sandbox.write_file(resolved, content)
        elapsed_ms = int((time.monotonic() - start) * 1000)

        return ToolResult(
            output=f"Successfully wrote {len(content)} bytes to {resolved}",
            exit_code=0,
            execution_time_ms=elapsed_ms,
        )
