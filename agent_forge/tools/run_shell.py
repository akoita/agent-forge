"""run_shell tool — executes shell commands in the sandbox."""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Any

from agent_forge.tools.base import Tool, ToolResult

if TYPE_CHECKING:
    from agent_forge.sandbox.base import Sandbox

_MAX_OUTPUT_BYTES = 50 * 1024  # 50 KB
_MAX_TIMEOUT = 600  # hard cap

# Dangerous command patterns
_BLOCKLIST: list[re.Pattern[str]] = [
    re.compile(r"rm\s+-rf\s+/(?:\s|$)"),
    re.compile(r":\(\)\{\s*:\|\:&\s*\};:"),  # fork bomb
    re.compile(r"\bmkfs\b"),
    re.compile(r"\bdd\s+if=/dev/"),
    re.compile(r"\bshutdown\b"),
]


def check_shell_blocklist(command: str) -> str | None:
    """Return a refusal message if ``command`` matches a dangerous pattern."""
    for pattern in _BLOCKLIST:
        if pattern.search(command):
            return f"Command blocked: matches dangerous pattern '{pattern.pattern}'"
    return None


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
                    "description": (
                        "Timeout in seconds (default: 30, max: sandbox policy, hard cap: 600)"
                    ),
                    "default": 30,
                },
            },
            "required": ["command"],
        }

    async def execute(self, arguments: dict[str, Any], sandbox: Sandbox) -> ToolResult:
        """Run a shell command with timeout and output truncation."""
        command = arguments.get("command", "")
        timeout_cap = getattr(sandbox, "timeout_cap_seconds", _MAX_TIMEOUT)
        if not isinstance(timeout_cap, int) or timeout_cap <= 0:
            timeout_cap = _MAX_TIMEOUT
        timeout = min(arguments.get("timeout_seconds", 30), timeout_cap, _MAX_TIMEOUT)

        if not command:
            return ToolResult(output="", error="Missing required argument: command", exit_code=1)

        blocked = check_shell_blocklist(command)
        if blocked is not None:
            return ToolResult(output="", error=blocked, exit_code=1)

        start = time.monotonic()
        result = await sandbox.exec(command, timeout_seconds=timeout)
        elapsed_ms = int((time.monotonic() - start) * 1000)

        output = result.stdout
        stderr = result.stderr or None
        raw_bytes = output.encode("utf-8", errors="replace")
        total_bytes = len(raw_bytes)
        metadata: dict[str, Any] = {
            "truncated": False,
            "total_bytes": total_bytes,
            "returned_bytes": total_bytes,
            "bytes_elided": 0,
        }

        if total_bytes > _MAX_OUTPUT_BYTES:
            head = raw_bytes[:_MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
            if "\n" in head:
                head = head.rsplit("\n", 1)[0]
            returned_bytes = len(head.encode("utf-8", errors="replace"))
            bytes_elided = total_bytes - returned_bytes
            hint = (
                "Output exceeded 50KB cap. Re-run with shell_start and paginate "
                "using shell_poll(session_id=..., offset=...) for full output."
            )
            metadata = {
                "truncated": True,
                "total_bytes": total_bytes,
                "returned_bytes": returned_bytes,
                "bytes_elided": bytes_elided,
                "hint": hint,
            }
            output = (
                f"{head}\n\n"
                f"[WARNING: Output truncated ({returned_bytes} of {total_bytes} bytes returned; "
                f"{bytes_elided} bytes elided). {hint}]"
            )

        return ToolResult(
            output=output,
            error=stderr,
            exit_code=result.exit_code,
            execution_time_ms=elapsed_ms,
            metadata=metadata,
        )
