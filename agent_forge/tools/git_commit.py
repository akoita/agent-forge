"""git_commit tool — create a commit from staged workspace changes."""

from __future__ import annotations

import shlex
import time
from typing import TYPE_CHECKING, Any

from agent_forge.tools.base import Tool, ToolResult

if TYPE_CHECKING:
    from agent_forge.sandbox.base import Sandbox


class GitCommitTool(Tool):
    """Commit already-staged changes with a validated commit message."""

    @property
    def name(self) -> str:
        return "git_commit"

    @property
    def description(self) -> str:
        return "Create a git commit from staged changes with the given message"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "Commit message for the staged changes",
                },
            },
            "required": ["message"],
        }

    async def execute(self, arguments: dict[str, Any], sandbox: Sandbox) -> ToolResult:
        """Commit staged changes after validating the message and staging state."""
        message = str(arguments.get("message", "")).strip()
        if not message:
            return ToolResult(output="", error="Missing required argument: message", exit_code=1)
        if "\x00" in message:
            return ToolResult(
                output="",
                error="Invalid commit message: contains NUL byte",
                exit_code=1,
            )

        start = time.monotonic()
        status = await sandbox.exec("git -C /workspace diff --cached --quiet", timeout_seconds=30)
        if status.exit_code == 0:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            return ToolResult(
                output="",
                error="No staged changes to commit.",
                exit_code=1,
                execution_time_ms=elapsed_ms,
            )
        if status.exit_code not in {0, 1}:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            return ToolResult(
                output="",
                error=status.stderr or "Unable to inspect staged changes",
                exit_code=status.exit_code,
                execution_time_ms=elapsed_ms,
            )

        result = await sandbox.exec(
            f"git -C /workspace commit -m {shlex.quote(message)}",
            timeout_seconds=30,
        )
        elapsed_ms = int((time.monotonic() - start) * 1000)
        if result.exit_code != 0:
            return ToolResult(
                output=result.stdout,
                error=result.stderr or "git commit failed",
                exit_code=result.exit_code,
                execution_time_ms=elapsed_ms,
            )
        return ToolResult(
            output=result.stdout or f"Created commit with message: {message}",
            exit_code=0,
            execution_time_ms=elapsed_ms,
        )
