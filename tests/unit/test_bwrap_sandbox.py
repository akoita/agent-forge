"""Unit tests for the bubblewrap sandbox backend."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_forge.llm.errors import SandboxStartupError
from agent_forge.sandbox.base import SandboxConfig, SandboxState
from agent_forge.sandbox.bwrap import BwrapSandbox

if TYPE_CHECKING:
    from pathlib import Path


class TestBwrapSandboxLifecycle:
    @pytest.mark.asyncio
    async def test_start_requires_linux(self) -> None:
        with (
            patch("agent_forge.sandbox.bwrap.platform.system", return_value="Darwin"),
            patch("agent_forge.sandbox.bwrap.shutil.which", return_value="/usr/bin/bwrap"),
        ):
            sandbox = BwrapSandbox()
            with pytest.raises(SandboxStartupError, match="only available on Linux"):
                await sandbox.start("/tmp/repo")

    @pytest.mark.asyncio
    async def test_start_requires_bwrap_binary(self) -> None:
        with (
            patch("agent_forge.sandbox.bwrap.platform.system", return_value="Linux"),
            patch("agent_forge.sandbox.bwrap.shutil.which", return_value=None),
        ):
            sandbox = BwrapSandbox()
            with pytest.raises(SandboxStartupError, match="requires the 'bwrap' executable"):
                await sandbox.start("/tmp/repo")

    @pytest.mark.asyncio
    async def test_start_sets_running_state(self) -> None:
        with (
            patch("agent_forge.sandbox.bwrap.platform.system", return_value="Linux"),
            patch("agent_forge.sandbox.bwrap.shutil.which", return_value="/usr/bin/bwrap"),
        ):
            sandbox = BwrapSandbox()
            await sandbox.start("/tmp/repo")
            assert sandbox.state == SandboxState.RUNNING
            assert await sandbox.is_alive() is True

    @pytest.mark.asyncio
    async def test_stop_sets_stopped_state(self) -> None:
        with (
            patch("agent_forge.sandbox.bwrap.platform.system", return_value="Linux"),
            patch("agent_forge.sandbox.bwrap.shutil.which", return_value="/usr/bin/bwrap"),
        ):
            sandbox = BwrapSandbox()
            await sandbox.start("/tmp/repo")
            await sandbox.stop()
            assert sandbox.state == SandboxState.STOPPED


class TestBwrapSandboxExec:
    @pytest.mark.asyncio
    async def test_exec_builds_isolated_command(self, tmp_path: Path) -> None:
        process = AsyncMock()
        process.communicate.return_value = (b"hello\n", b"")
        process.kill = MagicMock()
        process.returncode = 0

        with (
            patch("agent_forge.sandbox.bwrap.platform.system", return_value="Linux"),
            patch("agent_forge.sandbox.bwrap.shutil.which", return_value="/usr/bin/bwrap"),
            patch(
                "agent_forge.sandbox.bwrap.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=process,
            ) as mock_exec,
        ):
            sandbox = BwrapSandbox()
            await sandbox.start(
                str(tmp_path),
                SandboxConfig(
                    backend="bwrap",
                    network_enabled=True,
                    writable_cache_mounts=True,
                ),
            )

            result = await sandbox.exec("echo hello", timeout_seconds=45)

        assert result.exit_code == 0
        assert result.stdout == "hello\n"
        argv = mock_exec.await_args.args
        assert "--unshare-pid" in argv
        assert "--die-with-parent" in argv
        assert "--new-session" in argv
        assert "--bind" in argv
        assert "/cache" in argv
        assert "echo hello" in argv

    @pytest.mark.asyncio
    async def test_exec_unshares_network_when_disabled(self, tmp_path: Path) -> None:
        process = AsyncMock()
        process.communicate.return_value = (b"ok\n", b"")
        process.kill = MagicMock()
        process.returncode = 0

        with (
            patch("agent_forge.sandbox.bwrap.platform.system", return_value="Linux"),
            patch("agent_forge.sandbox.bwrap.shutil.which", return_value="/usr/bin/bwrap"),
            patch(
                "agent_forge.sandbox.bwrap.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=process,
            ) as mock_exec,
        ):
            sandbox = BwrapSandbox()
            await sandbox.start(
                str(tmp_path),
                SandboxConfig(backend="bwrap", network_enabled=False),
            )
            await sandbox.exec("echo ok")

        argv = mock_exec.await_args.args
        assert "--unshare-net" in argv

    @pytest.mark.asyncio
    async def test_exec_timeout_kills_process(self, tmp_path: Path) -> None:
        process = AsyncMock()
        process.kill = MagicMock()
        process.communicate = AsyncMock(side_effect=[TimeoutError, (b"", b"")])

        with (
            patch("agent_forge.sandbox.bwrap.platform.system", return_value="Linux"),
            patch("agent_forge.sandbox.bwrap.shutil.which", return_value="/usr/bin/bwrap"),
            patch(
                "agent_forge.sandbox.bwrap.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=process,
            ),
        ):
            sandbox = BwrapSandbox()
            await sandbox.start(str(tmp_path))
            result = await sandbox.exec("sleep 10", timeout_seconds=1)

        assert result.exit_code == 124
        process.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_read_file_not_found(self, tmp_path: Path) -> None:
        with (
            patch("agent_forge.sandbox.bwrap.platform.system", return_value="Linux"),
            patch("agent_forge.sandbox.bwrap.shutil.which", return_value="/usr/bin/bwrap"),
        ):
            sandbox = BwrapSandbox()
            await sandbox.start(str(tmp_path))
            sandbox.exec = AsyncMock(
                return_value=MagicMock(exit_code=1, stdout="", stderr="missing")
            )
            with pytest.raises(FileNotFoundError):
                await sandbox.read_file("/workspace/missing.txt")
