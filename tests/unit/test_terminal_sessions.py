"""Unit tests for resumable PTY terminal sessions, pagination metadata, and limits (#137)."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest

from agent_forge.agent.approvals import ApprovalGate
from agent_forge.agent.core import react_loop
from agent_forge.agent.models import AgentConfig, AgentRun, RunState
from agent_forge.eval.deterministic import SubprocessEvalSandbox
from agent_forge.llm.base import (
    LLMConfig,
    LLMProvider,
    LLMResponse,
    Message,
    TokenUsage,
    ToolCall,
    ToolDefinition,
)
from agent_forge.sandbox.base import ExecResult, SandboxConfig
from agent_forge.sandbox.sessions import BoundedOutputSpool, LocalPtySessionManager, SessionLimits
from agent_forge.tools import (
    RunShellTool,
    ShellCancelTool,
    ShellPollTool,
    ShellStartTool,
    ShellWriteTool,
    create_default_registry,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


def test_bounded_output_spool_10mb_pagination_and_elided_metadata() -> None:
    """10 MB bursts stay bounded in RAM and expose exact byte offsets & elision metadata."""
    max_retained = 2 * 1024 * 1024
    total_written = 10 * 1024 * 1024
    spool = BoundedOutputSpool(max_retained_bytes=max_retained)
    chunk = b"X" * 65_536
    for _ in range(total_written // len(chunk)):
        spool.append(chunk)

    assert spool.retained_bytes == max_retained
    assert spool.total_bytes == total_written

    first_page = spool.read(offset=None, max_bytes=32_768)
    assert first_page.truncated is True
    assert first_page.total_bytes == total_written
    assert first_page.returned_bytes == 32_768
    assert first_page.bytes_elided_before == total_written - max_retained
    assert first_page.offset == total_written - max_retained
    assert first_page.next_offset == first_page.offset + 32_768
    assert first_page.bytes_remaining_after == max_retained - 32_768
    notice = first_page.format_notice("sess-abc")
    assert "elided" in notice
    assert f"offset={first_page.next_offset}" in notice

    # Paginate to end explicitly
    cursor = first_page.next_offset
    while True:
        page = spool.read(offset=cursor, max_bytes=65_536)
        assert page.offset == cursor
        cursor = page.next_offset
        if page.bytes_remaining_after == 0:
            assert cursor == total_written
            break


@pytest.mark.asyncio
async def test_run_shell_10mb_returns_bounded_output_with_metadata() -> None:
    """One-shot run_shell caps stdout and emits structured truncation metadata."""
    total_bytes = 10 * 1024 * 1024
    payload = ("0123456789abcdef\n" * (total_bytes // 17 + 1))[:total_bytes]
    sandbox = AsyncMock()
    sandbox.timeout_cap_seconds = 120
    sandbox.exec.return_value = ExecResult(exit_code=0, stdout=payload, stderr="")

    result = await RunShellTool().execute({"command": "emit 10mb"}, sandbox)
    assert result.exit_code == 0
    assert result.metadata["truncated"] is True
    assert result.metadata["total_bytes"] == total_bytes
    assert result.metadata["returned_bytes"] <= 50 * 1024
    assert result.metadata["bytes_elided"] == total_bytes - result.metadata["returned_bytes"]
    assert "shell_poll" in result.metadata["hint"]
    assert "truncated" in result.output.lower()


@pytest.mark.asyncio
async def test_dev_server_start_poll_cancel_leaves_zero_orphans(tmp_path: Path) -> None:
    """Exit criterion 1: start a background server, poll it, cancel it, verify no orphan PID."""
    sandbox = SubprocessEvalSandbox()
    cfg = SandboxConfig(backend="subprocess-eval", timeout_seconds=30)
    await sandbox.start(str(tmp_path), cfg)

    start_tool = ShellStartTool()
    poll_tool = ShellPollTool()
    cancel_tool = ShellCancelTool()

    try:
        server_cmd = (
            "python3 -u -c 'import os, time; "
            'print(f"READY pid={os.getpid()}", flush=True); '
            "time.sleep(60)'"
        )
        started = await start_tool.execute(
            {"command": server_cmd, "pty": False, "wait_seconds": 1.0},
            sandbox,
        )
        assert started.exit_code == 0
        assert started.metadata["running"] is True
        assert "READY pid=" in started.output
        session_id = started.metadata["session_id"]

        pid_line = next(line for line in started.output.splitlines() if "READY pid=" in line)
        pid = int(pid_line.split("pid=")[1].strip())
        # Process must still be running after start returns
        os.kill(pid, 0)

        polled = await poll_tool.execute(
            {"session_id": session_id, "wait_seconds": 0.1},
            sandbox,
        )
        assert polled.exit_code == 0
        assert polled.metadata["running"] is True

        cancelled = await cancel_tool.execute({"session_id": session_id}, sandbox)
        assert cancelled.exit_code == 0
        assert cancelled.metadata["terminated"] is True
        assert cancelled.metadata["running"] is False

        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        await sandbox.stop()


@pytest.mark.asyncio
async def test_interactive_pty_repl_session(tmp_path: Path) -> None:
    """Interactive PTY programs (python -i REPL) report isatty=True and answer writes."""
    sandbox = SubprocessEvalSandbox()
    await sandbox.start(str(tmp_path), SandboxConfig(backend="subprocess-eval", timeout_seconds=30))
    start_tool = ShellStartTool()
    write_tool = ShellWriteTool()
    poll_tool = ShellPollTool()
    cancel_tool = ShellCancelTool()

    try:
        started = await start_tool.execute(
            {"command": "python3 -u -i", "pty": True, "wait_seconds": 1.0},
            sandbox,
        )
        session_id = started.metadata["session_id"]
        assert started.metadata["pty"] is True
        assert started.metadata["running"] is True

        written = await write_tool.execute(
            {
                "session_id": session_id,
                "input": "import sys; print('TTY=', sys.stdin.isatty(), 6 * 7)\n",
                "wait_seconds": 1.0,
            },
            sandbox,
        )
        collected = written.output
        if "TTY= True 42" not in collected:
            more = await poll_tool.execute(
                {"session_id": session_id, "wait_seconds": 1.0},
                sandbox,
            )
            collected += more.output
        assert "TTY= True 42" in collected

        ended = await cancel_tool.execute({"session_id": session_id}, sandbox)
        assert ended.metadata["running"] is False
    finally:
        await sandbox.stop()


@pytest.mark.asyncio
async def test_10mb_session_pagination_and_elided_metadata(tmp_path: Path) -> None:
    """Exit criterion 2: 10 MB command yields bounded pages with explicit truncation metadata."""
    sandbox = SubprocessEvalSandbox()
    await sandbox.start(str(tmp_path), SandboxConfig(backend="subprocess-eval", timeout_seconds=30))
    start_tool = ShellStartTool()
    poll_tool = ShellPollTool()

    try:
        total = 10 * 1024 * 1024
        started = await start_tool.execute(
            {
                "command": f"python3 -c 'import sys; sys.stdout.write(\"A\" * {total})'",
                "pty": False,
                "wait_seconds": 5.0,
                "max_bytes": 16_384,
            },
            sandbox,
        )
        deadline = time.monotonic() + 5.0
        latest = started
        while latest.metadata["running"] and time.monotonic() < deadline:
            latest = await poll_tool.execute(
                {"session_id": started.metadata["session_id"], "wait_seconds": 0.2},
                sandbox,
            )

        page1 = await poll_tool.execute(
            {
                "session_id": started.metadata["session_id"],
                "offset": 0,
                "max_bytes": 16_384,
                "wait_seconds": 0.0,
            },
            sandbox,
        )
        assert page1.metadata["truncated"] is True
        assert page1.metadata["total_bytes"] == total
        assert page1.metadata["returned_bytes"] == 16_384
        assert page1.metadata["bytes_elided_before"] == total - (2 * 1024 * 1024)
        assert page1.metadata["bytes_remaining_after"] > 0
        assert len(page1.output.encode("utf-8")) <= 20 * 1024
        assert "Output truncated:" in page1.output
        assert "shell_poll(" in page1.output
    finally:
        await sandbox.stop()


@pytest.mark.asyncio
async def test_session_limits_duration_blocklist_and_approval_gate(tmp_path: Path) -> None:
    """Enforce max_concurrent_sessions, per-session timeout, blocklist, and approvals."""
    mgr = LocalPtySessionManager(
        limits=SessionLimits(max_concurrent_sessions=1, default_max_duration_seconds=30)
    )
    try:
        s1 = await mgr.start(
            ["python3", "-u", "-c", "import time; print('HI', flush=True); time.sleep(30)"],
            command_label="sleep 30",
            pty_enabled=False,
            initial_wait_seconds=0.3,
        )
        assert s1.running is True
        with pytest.raises(RuntimeError, match="Maximum concurrent terminal sessions"):
            await mgr.start(["echo", "two"], command_label="echo two", pty_enabled=False)

        # Auto-timeout cancels runaway process
        timed_mgr = LocalPtySessionManager(
            limits=SessionLimits(max_concurrent_sessions=2, default_max_duration_seconds=1)
        )
        try:
            s_timeout = await timed_mgr.start(
                ["python3", "-c", "import time; time.sleep(30)"],
                command_label="runaway",
                pty_enabled=False,
                max_duration_seconds=1,
                initial_wait_seconds=0.1,
            )
            await asyncio.sleep(1.3)
            after = await timed_mgr.poll(s_timeout.session_id, wait_seconds=0.0)
            assert after.running is False
            assert after.timed_out is True
            assert after.exit_code == -signal.SIGKILL or (after.exit_code or 0) != 0
        finally:
            await timed_mgr.close_all()
    finally:
        await mgr.close_all()

    sandbox = SubprocessEvalSandbox()
    await sandbox.start(str(tmp_path), SandboxConfig(backend="subprocess-eval"))
    try:
        blocked = await ShellStartTool().execute({"command": "rm -rf /"}, sandbox)
        assert blocked.exit_code == 1
        assert "blocked" in (blocked.error or "").lower()
    finally:
        await sandbox.stop()

    gate = ApprovalGate(mode="deny")
    req, decision = await gate.evaluate("shell_start", {"command": "rm -rf tmp"})
    assert req is not None and decision is not None and not decision.approved


class _ScriptedSessionLoopLLM(LLMProvider):
    def __init__(self, pid_file: Path) -> None:
        self._pid_file = pid_file
        self._step = 0

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: LLMConfig | None = None,
    ) -> LLMResponse:
        del tools, config
        self._step += 1
        if self._step == 1:
            cmd = (
                'python3 -u -c "import os, pathlib, time; '
                f"pathlib.Path('{self._pid_file}').write_text(str(os.getpid())); "
                "print('LOOP_SERVER_UP', flush=True); time.sleep(60)\""
            )
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="shell_start",
                        arguments={"command": cmd, "pty": False, "wait_seconds": 1.0},
                    )
                ],
                usage=TokenUsage(10, 5, 15),
                finish_reason="tool_calls",
            )
        last_meta_line = messages[-1].content
        if self._step == 2:
            session_id = _extract_session_id(last_meta_line)
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="c2",
                        name="shell_poll",
                        arguments={"session_id": session_id, "wait_seconds": 0.1},
                    )
                ],
                usage=TokenUsage(10, 5, 15),
                finish_reason="tool_calls",
            )
        if self._step == 3:
            session_id = _extract_session_id(last_meta_line)
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="c3",
                        name="shell_cancel",
                        arguments={"session_id": session_id},
                    )
                ],
                usage=TokenUsage(10, 5, 15),
                finish_reason="tool_calls",
            )
        return LLMResponse(
            content=json.dumps({"status": "stopped"}),
            tool_calls=[],
            usage=TokenUsage(10, 5, 15),
            finish_reason="stop",
        )

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: LLMConfig | None = None,
    ) -> AsyncIterator[LLMResponse]:
        yield await self.complete(messages, tools=tools, config=config)


def _extract_session_id(tool_output: str) -> str:
    for token in tool_output.replace("[", " ").replace("]", " ").split():
        if token.startswith("session="):
            return token.split("=", 1)[1]
    msg = f"session id not found in {tool_output!r}"
    raise AssertionError(msg)


@pytest.mark.asyncio
async def test_react_loop_persists_session_across_iterations_and_cleans_on_stop(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Sessions persist across react_loop iterations and sandbox.stop() cleans leftovers."""
    from agent_forge.agent import persistence

    monkeypatch.setattr(persistence, "_default_runs_dir", lambda: tmp_path / "runs")
    pid_file = tmp_path / "server.pid"
    sandbox = SubprocessEvalSandbox()
    cfg = SandboxConfig(backend="subprocess-eval", timeout_seconds=30)
    await sandbox.start(str(tmp_path), cfg)
    tools = create_default_registry(load_plugins=False)
    run = AgentRun(
        task="Start server, check output, stop server",
        repo_path=str(tmp_path),
        config=AgentConfig(max_iterations=6),
    )

    try:
        completed = await react_loop(run, _ScriptedSessionLoopLLM(pid_file), tools, sandbox)
    finally:
        await sandbox.stop()

    assert completed.state == RunState.COMPLETED
    assert len(completed.tool_invocations) == 3
    assert [inv.tool_name for inv in completed.tool_invocations] == [
        "shell_start",
        "shell_poll",
        "shell_cancel",
    ]
    assert "LOOP_SERVER_UP" in completed.tool_invocations[0].result.output
    assert completed.tool_invocations[1].result.metadata["running"] is True
    assert completed.tool_invocations[2].result.metadata["terminated"] is True
    pid = int(pid_file.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
