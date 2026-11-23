"""Sandbox base class — abstract interface for isolated execution environments.

Defines the contract that tools use to interact with the sandbox,
plus SandboxConfig and SandboxState.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_forge.sandbox.sessions import SessionSnapshot

_SENSITIVE_ENV_SUFFIXES: tuple[str, ...] = (
    "_API_KEY",
    "_SECRET",
    "_SECRET_KEY",
    "_ACCESS_KEY",
    "_TOKEN",
    "_PASSWORD",
)
_SENSITIVE_ENV_EXACT: frozenset[str] = frozenset(
    {
        "GOOGLE_APPLICATION_CREDENTIALS",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    }
)


def sanitize_sandbox_env(env_vars: dict[str, str]) -> dict[str, str]:
    """Strip host/cloud credential variables before injecting env into a sandbox."""
    sanitized: dict[str, str] = {}
    for key, value in env_vars.items():
        upper = key.upper()
        if upper in _SENSITIVE_ENV_EXACT or upper.endswith(_SENSITIVE_ENV_SUFFIXES):
            continue
        sanitized[key] = value
    return sanitized


class SandboxState(Enum):
    """Container lifecycle state."""

    IDLE = "idle"
    RUNNING = "running"
    STOPPED = "stopped"


@dataclass
class SandboxConfig:
    """Configuration for a sandbox container."""

    backend: str = "docker"
    image: str = "agent-forge-sandbox:latest"
    workspace_path: str = "/workspace"
    cpu_limit: float = 1.0
    memory_limit: str = "512m"
    timeout_seconds: int = 300
    network_enabled: bool = False
    writable_cache_mounts: bool = False
    env_vars: dict[str, str] = field(default_factory=dict)


@dataclass
class ExecResult:
    """Result of executing a command inside the sandbox."""

    exit_code: int
    stdout: str
    stderr: str


class Sandbox(ABC):
    """Abstract base class for sandbox execution environments."""

    @abstractmethod
    async def start(self, repo_path: str, config: SandboxConfig | None = None) -> None:
        """Create and start the sandbox container with the repo mounted.

        Args:
            repo_path: Local path to the repository to mount.
            config: Sandbox configuration. Defaults to ``SandboxConfig()``.
        """
        ...

    @abstractmethod
    async def exec(
        self,
        command: str,
        *,
        timeout_seconds: int = 30,
    ) -> ExecResult:
        """Execute a shell command inside the sandbox.

        Args:
            command: Shell command to execute.
            timeout_seconds: Max time to wait for the command.

        Returns:
            ExecResult with exit_code, stdout, and stderr.
        """
        ...

    @abstractmethod
    async def read_file(self, path: str) -> str:
        """Read the contents of a file inside the sandbox.

        Args:
            path: Absolute path inside the sandbox.

        Returns:
            File contents as a string.

        Raises:
            FileNotFoundError: If the file does not exist.
        """
        ...

    @abstractmethod
    async def write_file(self, path: str, content: str) -> None:
        """Write content to a file inside the sandbox.

        Creates parent directories if needed.

        Args:
            path: Absolute path inside the sandbox.
            content: File content to write.
        """
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Stop and remove the sandbox container."""
        ...

    @abstractmethod
    async def is_alive(self) -> bool:
        """Check if the sandbox is still running."""
        ...

    async def start_session(
        self,
        command: str,
        *,
        pty: bool = True,
        max_duration_seconds: int | None = None,
        wait_seconds: float = 0.5,
        max_bytes: int = 32 * 1024,
    ) -> SessionSnapshot:
        """Start a resumable PTY or pipe session inside the sandbox."""
        del command, pty, max_duration_seconds, wait_seconds, max_bytes
        msg = f"{type(self).__name__} does not support interactive terminal sessions"
        raise NotImplementedError(msg)

    async def poll_session(
        self,
        session_id: str,
        *,
        wait_seconds: float = 0.5,
        offset: int | None = None,
        max_bytes: int = 32 * 1024,
    ) -> SessionSnapshot:
        """Poll a terminal session for new or paginated output."""
        del session_id, wait_seconds, offset, max_bytes
        msg = f"{type(self).__name__} does not support interactive terminal sessions"
        raise NotImplementedError(msg)

    async def write_session(
        self,
        session_id: str,
        data: str,
        *,
        wait_seconds: float = 0.5,
        max_bytes: int = 32 * 1024,
    ) -> SessionSnapshot:
        """Write input to a running terminal session and return recent output."""
        del session_id, data, wait_seconds, max_bytes
        msg = f"{type(self).__name__} does not support interactive terminal sessions"
        raise NotImplementedError(msg)

    async def cancel_session(self, session_id: str) -> SessionSnapshot:
        """Terminate a running terminal session and clean up its process group."""
        del session_id
        msg = f"{type(self).__name__} does not support interactive terminal sessions"
        raise NotImplementedError(msg)

    async def list_sessions(self) -> list[SessionSnapshot]:
        """List snapshots of tracked terminal sessions."""
        return []

    async def close_all_sessions(self) -> None:  # noqa: B027 - default no-op for sandboxes without sessions
        """Cancel all active terminal sessions on sandbox shutdown."""
