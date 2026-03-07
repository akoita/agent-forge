"""Unit tests for the Docker sandbox.

Uses mocked Docker SDK to test container lifecycle, security constraints,
exec, read_file, write_file, and error handling.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agent_forge.sandbox.base import SandboxConfig, SandboxState
from agent_forge.sandbox.docker import DockerSandbox


@pytest.fixture
def mock_docker_client() -> MagicMock:
    """Create a mocked Docker client."""
    client = MagicMock()
    container = MagicMock()
    container.short_id = "abc123"
    container.status = "running"
    container.exec_run.return_value = (0, (b"hello", b""))
    container.reload.return_value = None
    client.containers.run.return_value = container
    return client


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestSandboxLifecycle:
    @pytest.mark.asyncio
    async def test_initial_state(self) -> None:
        with patch("agent_forge.sandbox.docker.docker") as mock_docker:
            mock_docker.from_env.return_value = MagicMock()
            sandbox = DockerSandbox()
            assert sandbox.state == SandboxState.IDLE

    @pytest.mark.asyncio
    async def test_start(self, mock_docker_client: MagicMock) -> None:
        with patch("agent_forge.sandbox.docker.docker") as mock_docker:
            mock_docker.from_env.return_value = mock_docker_client
            sandbox = DockerSandbox()
            await sandbox.start("/tmp/repo")

            assert sandbox.state == SandboxState.RUNNING
            mock_docker_client.containers.run.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_security_opts(self, mock_docker_client: MagicMock) -> None:
        with patch("agent_forge.sandbox.docker.docker") as mock_docker:
            mock_docker.from_env.return_value = mock_docker_client
            sandbox = DockerSandbox()
            await sandbox.start("/tmp/repo")

            call_kwargs = mock_docker_client.containers.run.call_args[1]
            assert "no-new-privileges" in call_kwargs["security_opt"]
            assert call_kwargs["read_only"] is True
            assert call_kwargs["pids_limit"] == 256
            assert "/tmp" in call_kwargs["tmpfs"]
            assert call_kwargs["network_mode"] == "none"

    @pytest.mark.asyncio
    async def test_start_with_custom_config(self, mock_docker_client: MagicMock) -> None:
        with patch("agent_forge.sandbox.docker.docker") as mock_docker:
            mock_docker.from_env.return_value = mock_docker_client
            sandbox = DockerSandbox()
            config = SandboxConfig(
                cpu_limit=2.0,
                memory_limit="1g",
                network_enabled=True,
            )
            await sandbox.start("/tmp/repo", config=config)

            call_kwargs = mock_docker_client.containers.run.call_args[1]
            assert call_kwargs["nano_cpus"] == 2_000_000_000
            assert call_kwargs["mem_limit"] == "1g"
            assert "network_mode" not in call_kwargs

    @pytest.mark.asyncio
    async def test_start_already_running(self, mock_docker_client: MagicMock) -> None:
        with patch("agent_forge.sandbox.docker.docker") as mock_docker:
            mock_docker.from_env.return_value = mock_docker_client
            sandbox = DockerSandbox()
            await sandbox.start("/tmp/repo")

            with pytest.raises(RuntimeError, match="already running"):
                await sandbox.start("/tmp/repo")

    @pytest.mark.asyncio
    async def test_stop(self, mock_docker_client: MagicMock) -> None:
        with patch("agent_forge.sandbox.docker.docker") as mock_docker:
            mock_docker.from_env.return_value = mock_docker_client
            sandbox = DockerSandbox()
            await sandbox.start("/tmp/repo")
            await sandbox.stop()

            assert sandbox.state == SandboxState.STOPPED
            mock_docker_client.containers.run.return_value.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_when_idle(self) -> None:
        with patch("agent_forge.sandbox.docker.docker") as mock_docker:
            mock_docker.from_env.return_value = MagicMock()
            sandbox = DockerSandbox()
            await sandbox.stop()
            assert sandbox.state == SandboxState.STOPPED

    @pytest.mark.asyncio
    async def test_is_alive(self, mock_docker_client: MagicMock) -> None:
        with patch("agent_forge.sandbox.docker.docker") as mock_docker:
            mock_docker.from_env.return_value = mock_docker_client
            sandbox = DockerSandbox()
            await sandbox.start("/tmp/repo")

            assert await sandbox.is_alive() is True

    @pytest.mark.asyncio
    async def test_is_alive_when_idle(self) -> None:
        with patch("agent_forge.sandbox.docker.docker") as mock_docker:
            mock_docker.from_env.return_value = MagicMock()
            sandbox = DockerSandbox()
            assert await sandbox.is_alive() is False


# ---------------------------------------------------------------------------
# Exec
# ---------------------------------------------------------------------------


class TestSandboxExec:
    @pytest.mark.asyncio
    async def test_exec_success(self, mock_docker_client: MagicMock) -> None:
        with patch("agent_forge.sandbox.docker.docker") as mock_docker:
            mock_docker.from_env.return_value = mock_docker_client
            sandbox = DockerSandbox()
            await sandbox.start("/tmp/repo")

            result = await sandbox.exec("echo hello")

            assert result.exit_code == 0
            assert result.stdout == "hello"
            container = mock_docker_client.containers.run.return_value
            container.exec_run.assert_called_once_with(["bash", "-c", "echo hello"], demux=True)

    @pytest.mark.asyncio
    async def test_exec_with_stderr(self, mock_docker_client: MagicMock) -> None:
        container = mock_docker_client.containers.run.return_value
        container.exec_run.return_value = (1, (b"", b"command not found"))

        with patch("agent_forge.sandbox.docker.docker") as mock_docker:
            mock_docker.from_env.return_value = mock_docker_client
            sandbox = DockerSandbox()
            await sandbox.start("/tmp/repo")

            result = await sandbox.exec("bad_cmd")

            assert result.exit_code == 1
            assert result.stderr == "command not found"

    @pytest.mark.asyncio
    async def test_exec_not_running(self) -> None:
        with patch("agent_forge.sandbox.docker.docker") as mock_docker:
            mock_docker.from_env.return_value = MagicMock()
            sandbox = DockerSandbox()

            with pytest.raises(RuntimeError, match="not running"):
                await sandbox.exec("echo hello")


# ---------------------------------------------------------------------------
# File Operations
# ---------------------------------------------------------------------------


class TestSandboxFileOps:
    @pytest.mark.asyncio
    async def test_read_file(self, mock_docker_client: MagicMock) -> None:
        container = mock_docker_client.containers.run.return_value
        container.exec_run.return_value = (0, (b"file contents", b""))

        with patch("agent_forge.sandbox.docker.docker") as mock_docker:
            mock_docker.from_env.return_value = mock_docker_client
            sandbox = DockerSandbox()
            await sandbox.start("/tmp/repo")

            content = await sandbox.read_file("/workspace/main.py")
            assert content == "file contents"

    @pytest.mark.asyncio
    async def test_read_file_not_found(self, mock_docker_client: MagicMock) -> None:
        container = mock_docker_client.containers.run.return_value
        container.exec_run.return_value = (1, (b"", b"No such file"))

        with patch("agent_forge.sandbox.docker.docker") as mock_docker:
            mock_docker.from_env.return_value = mock_docker_client
            sandbox = DockerSandbox()
            await sandbox.start("/tmp/repo")

            with pytest.raises(FileNotFoundError):
                await sandbox.read_file("/workspace/missing.py")

    @pytest.mark.asyncio
    async def test_write_file(self, mock_docker_client: MagicMock) -> None:
        container = mock_docker_client.containers.run.return_value
        container.exec_run.return_value = (0, (b"", b""))

        with patch("agent_forge.sandbox.docker.docker") as mock_docker:
            mock_docker.from_env.return_value = mock_docker_client
            sandbox = DockerSandbox()
            await sandbox.start("/tmp/repo")

            await sandbox.write_file("/workspace/new.py", "print('hi')")
            # Should have called exec at least twice (mkdir -p + write)
            assert container.exec_run.call_count >= 2


# ---------------------------------------------------------------------------
# Volume Mounts
# ---------------------------------------------------------------------------


class TestSandboxMounts:
    @pytest.mark.asyncio
    async def test_repo_mounted(self, mock_docker_client: MagicMock) -> None:
        with patch("agent_forge.sandbox.docker.docker") as mock_docker:
            mock_docker.from_env.return_value = mock_docker_client
            sandbox = DockerSandbox()
            await sandbox.start("/home/user/project")

            call_kwargs = mock_docker_client.containers.run.call_args[1]
            volumes = call_kwargs["volumes"]
            assert "/home/user/project" in volumes
            assert volumes["/home/user/project"]["bind"] == "/workspace"
            assert volumes["/home/user/project"]["mode"] == "rw"
