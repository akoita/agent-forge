"""Sandbox base class — abstract interface for isolated execution environments.

Defines the contract that tools use to interact with the sandbox,
plus SandboxConfig and SandboxState.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum


class SandboxState(Enum):
    """Container lifecycle state."""

    IDLE = "idle"
    RUNNING = "running"
    STOPPED = "stopped"


@dataclass
class SandboxConfig:
    """Configuration for a sandbox container."""

    image: str = "agent-forge-sandbox:latest"
    workspace_path: str = "/workspace"
    cpu_limit: float = 1.0
    memory_limit: str = "512m"
    timeout_seconds: int = 300
    network_enabled: bool = False
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
