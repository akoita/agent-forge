"""Integration tests for resumable PTY sessions across Docker and Bubblewrap (#137)."""

from __future__ import annotations

import platform
import shutil
import time
from typing import TYPE_CHECKING

import docker
import pytest
from docker.errors import DockerException

from agent_forge.sandbox.base import SandboxConfig
from agent_forge.sandbox.bwrap import BwrapSandbox
from agent_forge.sandbox.docker import DockerSandbox

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_docker_sandbox_pty_pagination_and_orphan_cleanup(tmp_path: Path) -> None:
    """Verify Docker PTY REPL, 10 MB bounded pagination, and PGID orphan cleanup."""
    try:
        client = docker.from_env()
        client.ping()
        client.images.get("agent-forge-sandbox:latest")
    except DockerException:
        pytest.skip("Docker daemon or agent-forge-sandbox:latest image unavailable")

    sandbox = DockerSandbox()
    cfg = SandboxConfig(backend="docker", image="agent-forge-sandbox:latest", timeout_seconds=60)
    await sandbox.start(str(tmp_path), cfg)

    try:
        # 1. Interactive PTY Python REPL
        repl = await sandbox.start_session("python3 -u -i", pty=True, wait_seconds=1.0)
        assert repl.pty is True
        assert repl.running is True

        out = await sandbox.write_session(
            repl.session_id,
            "import sys; print('DKR_TTY=', sys.stdin.isatty(), 6 * 7)\n",
            wait_seconds=1.0,
        )
        combined = out.page.text
        if "DKR_TTY= True 42" not in combined:
            more = await sandbox.poll_session(repl.session_id, wait_seconds=1.0)
            combined += more.page.text
        assert "DKR_TTY= True 42" in combined
        await sandbox.cancel_session(repl.session_id)

        # 2. Long-running parent + child subprocess cleanup leaves zero orphans
        bg = await sandbox.start_session(
            (
                "python3 -u -c 'import subprocess, time; "
                'subprocess.Popen(["sleep", "60"]); '
                'print("DKR_BG_READY", flush=True); '
                "time.sleep(60)'"
            ),
            pty=False,
            wait_seconds=1.5,
        )
        bg_text = bg.page.text
        deadline = time.monotonic() + 3.0
        while "DKR_BG_READY" not in bg_text and time.monotonic() < deadline:
            polled = await sandbox.poll_session(bg.session_id, wait_seconds=0.3)
            bg_text += polled.page.text
        assert "DKR_BG_READY" in bg_text

        cancelled = await sandbox.cancel_session(bg.session_id)
        assert cancelled.running is False

        probe = await sandbox.exec("grep -H . /proc/[0-9]*/comm 2>/dev/null || true")
        comms = [line.split(":", 1)[-1].strip() for line in probe.stdout.splitlines()]
        # Only container PID 1 (`sleep infinity`), our `bash`, and `grep` should exist
        assert "python3" not in comms, f"Leaked python3 processes in container: {probe.stdout}"
        sleep_count = sum(1 for c in comms if c == "sleep")
        assert sleep_count <= 1, f"Leaked sleep child processes in container: {probe.stdout}"

        # 3. 10 MB output burst stays bounded with exact truncation metadata
        total = 10 * 1024 * 1024
        burst = await sandbox.start_session(
            f"python3 -c 'import sys; sys.stdout.write(\"B\" * {total})'",
            pty=False,
            wait_seconds=5.0,
            max_bytes=16_384,
        )
        deadline = time.monotonic() + 10.0
        while burst.running and time.monotonic() < deadline:
            burst = await sandbox.poll_session(burst.session_id, wait_seconds=0.2)

        page1 = await sandbox.poll_session(
            burst.session_id,
            offset=0,
            max_bytes=16_384,
            wait_seconds=0.0,
        )
        assert page1.page.truncated is True
        assert page1.page.total_bytes == total
        assert page1.page.returned_bytes == 16_384
        assert page1.page.bytes_elided_before == total - (2 * 1024 * 1024)
        assert page1.page.bytes_remaining_after > 0
    finally:
        await sandbox.stop()


@pytest.mark.asyncio
async def test_bwrap_sandbox_pty_and_orphan_cleanup(tmp_path: Path) -> None:
    """Verify Bubblewrap interactive PTY execution and clean session cancellation."""
    if platform.system() != "Linux" or shutil.which("bwrap") is None:
        pytest.skip("bubblewrap is not available")

    sandbox = BwrapSandbox()
    cfg = SandboxConfig(backend="bwrap", timeout_seconds=60)
    await sandbox.start(str(tmp_path), cfg)

    try:
        repl = await sandbox.start_session("python3 -u -i", pty=True, wait_seconds=1.0)
        assert repl.pty is True
        assert repl.running is True

        out = await sandbox.write_session(
            repl.session_id,
            "import sys; print('BWRAP_TTY=', sys.stdin.isatty(), 6 * 7)\n",
            wait_seconds=1.0,
        )
        combined = out.page.text
        if "BWRAP_TTY= True 42" not in combined:
            more = await sandbox.poll_session(repl.session_id, wait_seconds=1.0)
            combined += more.page.text
        assert "BWRAP_TTY= True 42" in combined

        cancelled = await sandbox.cancel_session(repl.session_id)
        assert cancelled.running is False

        bg = await sandbox.start_session(
            "python3 -u -c 'import time; print(\"BW_BG\", flush=True); time.sleep(60)'",
            pty=False,
            wait_seconds=1.0,
        )
        assert bg.running is True
        await sandbox.cancel_session(bg.session_id)
        sessions = await sandbox.list_sessions()
        assert all(not item.running for item in sessions)
    finally:
        await sandbox.stop()
