"""Resumable PTY/pipe terminal session tools (start, poll, write, cancel)."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from agent_forge.tools.base import Tool, ToolResult
from agent_forge.tools.run_shell import check_shell_blocklist

if TYPE_CHECKING:
    from agent_forge.sandbox.base import Sandbox
    from agent_forge.sandbox.sessions import SessionSnapshot

_MAX_SESSION_DURATION = 600
_DEFAULT_PAGE_BYTES = 32 * 1024
_MIN_PAGE_BYTES = 256
_MAX_PAGE_BYTES = 64 * 1024


def _clamp_int(raw: object, default: int, minimum: int, maximum: int) -> int:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return default
    return max(minimum, min(int(raw), maximum))


def _clamp_float(raw: object, default: float, minimum: float, maximum: float) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return default
    return max(minimum, min(float(raw), maximum))


def _format_snapshot(
    snapshot: SessionSnapshot,
    *,
    elapsed_ms: int,
    terminated: bool = False,
) -> ToolResult:
    """Format a :class:`SessionSnapshot` into a bounded, metadata-annotated ToolResult."""
    metadata: dict[str, Any] = {
        "session_id": snapshot.session_id,
        "command": snapshot.command,
        "running": snapshot.running,
        "exit_code": snapshot.exit_code,
        "pty": snapshot.pty,
        "timed_out": snapshot.timed_out,
        "elapsed_seconds": snapshot.elapsed_seconds,
        **snapshot.page.to_metadata(),
    }
    if terminated:
        metadata["terminated"] = True

    state_label = "running" if snapshot.running else f"exited(code={snapshot.exit_code})"
    status_line = (
        f"[session={snapshot.session_id} state={state_label} "
        f"pty={snapshot.pty} elapsed={snapshot.elapsed_seconds:.2f}s]"
    )
    parts = [part for part in (snapshot.page.text.rstrip("\n"), status_line) if part]
    notice = snapshot.page.format_notice(snapshot.session_id)
    if notice:
        parts.append(notice)

    error: str | None = None
    if snapshot.timed_out:
        error = (
            f"Terminal session '{snapshot.session_id}' timed out "
            f"after {snapshot.elapsed_seconds:.2f}s"
        )

    if terminated:
        tool_exit_code = 0
    elif snapshot.timed_out:
        tool_exit_code = 124
    elif snapshot.running:
        tool_exit_code = 0
    elif snapshot.exit_code is not None:
        tool_exit_code = snapshot.exit_code
    else:
        tool_exit_code = 1

    return ToolResult(
        output="\n".join(parts),
        error=error,
        exit_code=tool_exit_code,
        execution_time_ms=elapsed_ms,
        metadata=metadata,
    )


class ShellStartTool(Tool):
    """Start a resumable PTY or background terminal session inside the sandbox."""

    @property
    def name(self) -> str:
        return "shell_start"

    @property
    def description(self) -> str:
        return (
            "Start a persistent interactive PTY or background shell session in the sandbox "
            "(for dev servers, test watchers, REPLs, or streaming builds); returns session_id "
            "and initial paginated output"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to launch in the session",
                },
                "pty": {
                    "type": "boolean",
                    "description": "Allocate a pseudo-terminal (default: true)",
                    "default": True,
                },
                "wait_seconds": {
                    "type": "number",
                    "description": "Initial seconds to wait for startup output (default: 0.5)",
                    "default": 0.5,
                },
                "max_duration_seconds": {
                    "type": "integer",
                    "description": "Max session lifetime in seconds (capped by sandbox policy)",
                },
                "max_bytes": {
                    "type": "integer",
                    "description": (
                        "Max output bytes to return in this observation (default: 32768)"
                    ),
                    "default": _DEFAULT_PAGE_BYTES,
                },
            },
            "required": ["command"],
        }

    async def execute(self, arguments: dict[str, Any], sandbox: Sandbox) -> ToolResult:
        """Start a terminal session and return its initial output page."""
        command = arguments.get("command", "")
        if not isinstance(command, str) or not command:
            return ToolResult(output="", error="Missing required argument: command", exit_code=1)

        blocked = check_shell_blocklist(command)
        if blocked is not None:
            return ToolResult(output="", error=blocked, exit_code=1)

        pty_enabled = bool(arguments.get("pty", True))
        wait_seconds = _clamp_float(arguments.get("wait_seconds"), 0.5, 0.0, 10.0)
        max_bytes = _clamp_int(
            arguments.get("max_bytes"),
            _DEFAULT_PAGE_BYTES,
            _MIN_PAGE_BYTES,
            _MAX_PAGE_BYTES,
        )
        timeout_cap = getattr(sandbox, "timeout_cap_seconds", _MAX_SESSION_DURATION)
        if not isinstance(timeout_cap, int) or timeout_cap <= 0:
            timeout_cap = _MAX_SESSION_DURATION
        raw_duration = arguments.get("max_duration_seconds")
        duration = (
            _clamp_int(raw_duration, timeout_cap, 1, min(timeout_cap, _MAX_SESSION_DURATION))
            if raw_duration is not None
            else min(timeout_cap, _MAX_SESSION_DURATION)
        )

        started = time.monotonic()
        try:
            snapshot = await sandbox.start_session(
                command,
                pty=pty_enabled,
                max_duration_seconds=duration,
                wait_seconds=wait_seconds,
                max_bytes=max_bytes,
            )
        except (NotImplementedError, RuntimeError, ValueError, OSError) as exc:
            return ToolResult(
                output="",
                error=str(exc),
                exit_code=1,
                execution_time_ms=int((time.monotonic() - started) * 1000),
            )
        return _format_snapshot(
            snapshot,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )


class ShellPollTool(Tool):
    """Poll a running or exited terminal session for paginated output."""

    @property
    def name(self) -> str:
        return "shell_poll"

    @property
    def side_effecting(self) -> bool:
        return False

    @property
    def description(self) -> str:
        return (
            "Poll a terminal session by session_id for unread output or a specific byte "
            "offset window; includes explicit truncation and remaining-byte metadata"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session identifier returned by shell_start",
                },
                "wait_seconds": {
                    "type": "number",
                    "description": "Seconds to wait for new output before returning (default: 0.5)",
                    "default": 0.5,
                },
                "offset": {
                    "type": "integer",
                    "description": "Byte offset to read from (defaults to unread cursor)",
                },
                "max_bytes": {
                    "type": "integer",
                    "description": "Max output bytes to return in this page (default: 32768)",
                    "default": _DEFAULT_PAGE_BYTES,
                },
            },
            "required": ["session_id"],
        }

    async def execute(self, arguments: dict[str, Any], sandbox: Sandbox) -> ToolResult:
        """Poll ``session_id`` and return bounded paginated output."""
        session_id = arguments.get("session_id", "")
        if not isinstance(session_id, str) or not session_id:
            return ToolResult(output="", error="Missing required argument: session_id", exit_code=1)

        wait_seconds = _clamp_float(arguments.get("wait_seconds"), 0.5, 0.0, 30.0)
        max_bytes = _clamp_int(
            arguments.get("max_bytes"),
            _DEFAULT_PAGE_BYTES,
            _MIN_PAGE_BYTES,
            _MAX_PAGE_BYTES,
        )
        raw_offset = arguments.get("offset")
        offset: int | None = None
        if raw_offset is not None:
            if isinstance(raw_offset, bool) or not isinstance(raw_offset, int) or raw_offset < 0:
                return ToolResult(
                    output="", error="offset must be a non-negative integer", exit_code=1
                )
            offset = raw_offset

        started = time.monotonic()
        try:
            snapshot = await sandbox.poll_session(
                session_id,
                wait_seconds=wait_seconds,
                offset=offset,
                max_bytes=max_bytes,
            )
        except (KeyError, NotImplementedError, RuntimeError, ValueError, OSError) as exc:
            return ToolResult(
                output="",
                error=str(exc),
                exit_code=1,
                execution_time_ms=int((time.monotonic() - started) * 1000),
            )
        return _format_snapshot(
            snapshot,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )


class ShellWriteTool(Tool):
    """Send stdin / keystrokes to an active interactive terminal session."""

    @property
    def name(self) -> str:
        return "shell_write"

    @property
    def description(self) -> str:
        return (
            "Write stdin input (including newline '\\n' if needed) to an active terminal "
            "session and return the resulting output"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session identifier returned by shell_start",
                },
                "input": {
                    "type": "string",
                    "description": "Text or control characters to send to session stdin",
                },
                "wait_seconds": {
                    "type": "number",
                    "description": "Seconds to wait for resulting output (default: 0.5)",
                    "default": 0.5,
                },
                "max_bytes": {
                    "type": "integer",
                    "description": "Max output bytes to return in this page (default: 32768)",
                    "default": _DEFAULT_PAGE_BYTES,
                },
            },
            "required": ["session_id", "input"],
        }

    async def execute(self, arguments: dict[str, Any], sandbox: Sandbox) -> ToolResult:
        """Write ``input`` to ``session_id`` and return bounded resulting output."""
        session_id = arguments.get("session_id", "")
        if not isinstance(session_id, str) or not session_id:
            return ToolResult(output="", error="Missing required argument: session_id", exit_code=1)
        data = arguments.get("input")
        if not isinstance(data, str):
            return ToolResult(output="", error="Missing required argument: input", exit_code=1)

        wait_seconds = _clamp_float(arguments.get("wait_seconds"), 0.5, 0.0, 10.0)
        max_bytes = _clamp_int(
            arguments.get("max_bytes"),
            _DEFAULT_PAGE_BYTES,
            _MIN_PAGE_BYTES,
            _MAX_PAGE_BYTES,
        )
        started = time.monotonic()
        try:
            snapshot = await sandbox.write_session(
                session_id,
                data,
                wait_seconds=wait_seconds,
                max_bytes=max_bytes,
            )
        except (KeyError, NotImplementedError, RuntimeError, ValueError, OSError) as exc:
            return ToolResult(
                output="",
                error=str(exc),
                exit_code=1,
                execution_time_ms=int((time.monotonic() - started) * 1000),
            )
        return _format_snapshot(
            snapshot,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )


class ShellCancelTool(Tool):
    """Terminate an active terminal session and clean up its process group."""

    @property
    def name(self) -> str:
        return "shell_cancel"

    @property
    def description(self) -> str:
        return (
            "Terminate a running terminal session (and all child processes) and return "
            "remaining output"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session identifier returned by shell_start",
                },
            },
            "required": ["session_id"],
        }

    async def execute(self, arguments: dict[str, Any], sandbox: Sandbox) -> ToolResult:
        """Cancel ``session_id`` and return remaining output."""
        session_id = arguments.get("session_id", "")
        if not isinstance(session_id, str) or not session_id:
            return ToolResult(output="", error="Missing required argument: session_id", exit_code=1)

        started = time.monotonic()
        try:
            snapshot = await sandbox.cancel_session(session_id)
        except (KeyError, NotImplementedError, RuntimeError, ValueError, OSError) as exc:
            return ToolResult(
                output="",
                error=str(exc),
                exit_code=1,
                execution_time_ms=int((time.monotonic() - started) * 1000),
            )
        return _format_snapshot(
            snapshot,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            terminated=True,
        )
