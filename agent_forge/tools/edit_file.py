"""edit_file tool — targeted text replacement in sandbox files."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from agent_forge.tools.base import Tool, ToolResult, validate_path

if TYPE_CHECKING:
    from agent_forge.sandbox.base import Sandbox


class EditFileTool(Tool):
    """Apply a targeted edit to a file by replacing a specific text block."""

    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        return "Apply a targeted edit to a file by replacing a specific text block"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path within the workspace",
                },
                "old_text": {
                    "type": "string",
                    "description": "Exact text to find and replace",
                },
                "new_text": {
                    "type": "string",
                    "description": "Replacement text",
                },
            },
            "required": ["path", "old_text", "new_text"],
        }

    async def execute(self, arguments: dict[str, Any], sandbox: Sandbox) -> ToolResult:
        """Read file, replace first occurrence of old_text with new_text, write back."""
        path = arguments.get("path", "")
        old_text = arguments.get("old_text", "")
        new_text = arguments.get("new_text", "")

        if not path:
            return ToolResult(output="", error="Missing required argument: path", exit_code=1)
        if not old_text:
            return ToolResult(output="", error="Missing required argument: old_text", exit_code=1)

        try:
            resolved = validate_path(path)
        except ValueError as exc:
            return ToolResult(output="", error=str(exc), exit_code=1)

        start = time.monotonic()

        # Read the file
        try:
            content = await sandbox.read_file(resolved)
        except FileNotFoundError:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            return ToolResult(
                output="",
                error=f"File not found: {resolved}",
                exit_code=1,
                execution_time_ms=elapsed_ms,
            )

        # Check if old_text exists
        if old_text not in content:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            # Provide preview of file content for debugging
            preview = content[:500]
            return ToolResult(
                output="",
                error=(f"Text not found in {resolved}.\n\nFile preview:\n{preview}"),
                exit_code=1,
                execution_time_ms=elapsed_ms,
            )

        # Replace first occurrence
        new_content = content.replace(old_text, new_text, 1)
        await sandbox.write_file(resolved, new_content)

        elapsed_ms = int((time.monotonic() - start) * 1000)
        return ToolResult(
            output=f"Successfully edited {resolved}",
            exit_code=0,
            execution_time_ms=elapsed_ms,
        )
