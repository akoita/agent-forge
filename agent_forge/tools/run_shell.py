"""run_shell tool — executes shell commands in the sandbox."""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Any

from agent_forge.tools.base import Tool, ToolResult

if TYPE_CHECKING:
    from agent_forge.sandbox.base import Sandbox

_MAX_OUTPUT_BYTES = 50 * 1024  # 50 KB
_MAX_TIMEOUT = 120  # hard cap

# Dangerous command patterns
_BLOCKLIST: list[re.Pattern[str]] = [
    re.compile(r"rm\s+-rf\s+/(?:\s|$)"),
    re.compile(r":\(\)\{\s*:\|\:&\s*\};:"),  # fork bomb
    re.compile(r"\bmkfs\b"),
    re.compile(r"\bdd\s+if=/dev/"),
    re.compile(r"\bshutdown\b"),
]


class RunShellTool(Tool):
    """Execute a shell command inside the sandboxed workspace."""

    @property
    def name(self) -> str:
        return "run_shell"

    @property
    def description(self) -> str:
        return "Execute a shell command inside the sandboxed workspace"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "Timeout in seconds (default: 30, max: 120)",
                    "default": 30,
                },
            },
            "required": ["command"],
        }

    async def execute(self, arguments: dict[str, Any], sandbox: Sandbox) -> ToolResult:
        """Run a shell command with timeout and output truncation."""
        command = arguments.get("command", "")
        timeout = min(arguments.get("timeout_seconds", 30), _MAX_TIMEOUT)

        if not command:
            return ToolResult(output="", error="Missing required argument: command", exit_code=1)

        # Check blocklist
        for pattern in _BLOCKLIST:
            if pattern.search(command):
                return ToolResult(
                    output="",
                    error=f"Command blocked: matches dangerous pattern '{pattern.pattern}'",
                    exit_code=1,
                )

        start = time.monotonic()
        result = await sandbox.exec(command, timeout_seconds=timeout)
        elapsed_ms = int((time.monotonic() - start) * 1000)

        output = result.stdout
        stderr = result.stderr or None

        # Truncate if output is too large
        if len(output.encode("utf-8", errors="replace")) > _MAX_OUTPUT_BYTES:
            output = output[:_MAX_OUTPUT_BYTES].rsplit("\n", 1)[0]
            output += "\n\n[WARNING: Output truncated at 50KB.]"

        return ToolResult(
            output=output,
            error=stderr,
            exit_code=result.exit_code,
            execution_time_ms=elapsed_ms,
        )
