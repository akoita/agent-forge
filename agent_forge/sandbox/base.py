"""Sandbox base class — abstract interface for isolated execution environments.

Defines the contract that tools use to interact with the sandbox,
plus SandboxConfig and SandboxState.
"""

from __future__ import annotations

import ipaddress
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agent_forge.sandbox.sessions import SessionSnapshot

_SENSITIVE_BIND_SEGMENTS: frozenset[str] = frozenset(
    {
        ".ssh",
        ".aws",
        ".gnupg",
        ".kube",
        ".agent-forge",
        ".netrc",
        ".git-credentials",
    }
)
_SENSITIVE_BIND_SUFFIXES: tuple[tuple[str, ...], ...] = (
    (".config", "gcloud"),
    (".docker", "config.json"),
)


def validate_ro_bind_path(raw_path: str) -> str:
    """Validate an extra read-only bind path and reject credential directories."""
    cleaned = raw_path.strip()
    if not cleaned:
        msg = "Read-only bind path cannot be empty"
        raise ValueError(msg)
    if not cleaned.startswith(("/", "~")):
        msg = f"Read-only bind path must be absolute or '~'-prefixed: '{raw_path}'"
        raise ValueError(msg)
    resolved = Path(cleaned).expanduser().resolve()
    home_dir = Path.home().resolve()
    if resolved == home_dir:
        msg = "Refusing to mount entire user home directory into sandbox"
        raise ValueError(msg)
    parts = resolved.parts
    if any(seg in parts for seg in _SENSITIVE_BIND_SEGMENTS):
        msg = f"Refusing to mount sensitive credential path: '{raw_path}'"
        raise ValueError(msg)
    for suffix in _SENSITIVE_BIND_SUFFIXES:
        for idx in range(len(parts) - len(suffix) + 1):
            if parts[idx : idx + len(suffix)] == suffix:
                msg = f"Refusing to mount sensitive credential path: '{raw_path}'"
                raise ValueError(msg)
    return str(resolved)


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


_CACHE_ENV_DEFAULTS: tuple[tuple[str, str], ...] = (
    ("HOME", "/cache/home"),
    ("XDG_CACHE_HOME", "/cache/xdg"),
    ("PIP_CACHE_DIR", "/cache/pip"),
    ("NPM_CONFIG_CACHE", "/cache/npm"),
    ("YARN_CACHE_FOLDER", "/cache/yarn"),
    ("PNPM_HOME", "/cache/pnpm-home"),
    ("PNPM_STORE_DIR", "/cache/pnpm-store"),
    ("GOCACHE", "/cache/go-build"),
    ("GOMODCACHE", "/cache/go-mod"),
    ("CARGO_HOME", "/cache/cargo"),
    ("GRADLE_USER_HOME", "/cache/gradle"),
    ("MISE_DATA_DIR", "/cache/mise"),
    ("MISE_CACHE_DIR", "/cache/mise-cache"),
    ("MISE_CONFIG_DIR", "/cache/mise-config"),
)

_SETUP_CACHE_SUBDIRS: tuple[str, ...] = ("home", "xdg", "mise", "mise-cache", "mise-config")


def apply_sandbox_cache_env(env: dict[str, str], *, use_cache: bool) -> None:
    """Populate default cache or offline writable ``HOME`` environment variables."""
    if use_cache:
        for key, value in _CACHE_ENV_DEFAULTS:
            env.setdefault(key, value)
    else:
        env.setdefault("HOME", "/tmp/home")  # noqa: S108
        env.setdefault("XDG_CACHE_HOME", "/tmp/xdg")  # noqa: S108


def create_setup_cache_dir() -> str:
    """Create a temporary host directory mounted at ``/cache`` across setup and agent phases."""
    cache_dir = Path(tempfile.mkdtemp(prefix="agent-forge-cache-"))
    for subdir in _SETUP_CACHE_SUBDIRS:
        (cache_dir / subdir).mkdir(parents=True, exist_ok=True)
    return str(cache_dir)


class SandboxState(Enum):
    """Container lifecycle state."""

    IDLE = "idle"
    RUNNING = "running"
    STOPPED = "stopped"


@dataclass(frozen=True)
class PortMapping:
    """Validated host-to-container loopback port publication mapping."""

    host_ip: str
    host_port: int
    container_port: int
    protocol: str = "tcp"


def _parse_port_number(raw: str) -> int:
    stripped = raw.strip()
    if not stripped.isdigit():
        msg = f"Invalid port number: '{raw}'"
        raise ValueError(msg)
    port = int(stripped)
    if not 1 <= port <= 65535:
        msg = f"Port out of range (1-65535): {port}"
        raise ValueError(msg)
    return port


def _split_port_spec_host(address_part: str) -> tuple[str, int, int]:
    if address_part.startswith("["):
        end_idx = address_part.find("]")
        if end_idx == -1 or len(address_part) <= end_idx + 2 or address_part[end_idx + 1] != ":":
            msg = f"Invalid bracketed IPv6 port mapping: '{address_part}'"
            raise ValueError(msg)
        host_ip = address_part[1:end_idx]
        ipaddress.ip_address(host_ip)
        port_parts = address_part[end_idx + 2 :].split(":")
        if len(port_parts) != 2:
            msg = f"Expected [host_ip]:host_port:container_port in '{address_part}'"
            raise ValueError(msg)
        return host_ip, _parse_port_number(port_parts[0]), _parse_port_number(port_parts[1])

    parts = address_part.split(":")
    if len(parts) == 1:
        port = _parse_port_number(parts[0])
        return "127.0.0.1", port, port
    if len(parts) == 2:
        return "127.0.0.1", _parse_port_number(parts[0]), _parse_port_number(parts[1])
    if len(parts) == 3:
        host_ip = parts[0].strip()
        ipaddress.ip_address(host_ip)
        return host_ip, _parse_port_number(parts[1]), _parse_port_number(parts[2])
    msg = f"Invalid port mapping format: '{address_part}'"
    raise ValueError(msg)


def parse_published_port(spec: str) -> PortMapping:
    """Parse a port publication string such as ``8080:3000`` or ``127.0.0.1:8080:3000/tcp``."""
    cleaned = spec.strip()
    if not cleaned:
        msg = "Port mapping specification cannot be empty"
        raise ValueError(msg)

    protocol = "tcp"
    address_part = cleaned
    if "/" in cleaned:
        if cleaned.count("/") > 1:
            msg = f"Invalid port protocol suffix in '{spec}'"
            raise ValueError(msg)
        address_part, raw_proto = cleaned.split("/", 1)
        protocol = raw_proto.strip().lower()
        if protocol not in {"tcp", "udp"}:
            msg = f"Unsupported port protocol '{raw_proto}' (expected tcp or udp)"
            raise ValueError(msg)

    host_ip, host_port, container_port = _split_port_spec_host(address_part)
    return PortMapping(
        host_ip=host_ip,
        host_port=host_port,
        container_port=container_port,
        protocol=protocol,
    )


def parse_published_ports(specs: Sequence[str]) -> dict[str, tuple[str, int]]:
    """Parse published port specs into Docker SDK ``ports`` bindings."""
    bindings: dict[str, tuple[str, int]] = {}
    for spec in specs:
        mapping = parse_published_port(spec)
        key = f"{mapping.container_port}/{mapping.protocol}"
        if key in bindings:
            msg = f"Duplicate published container port binding: {key}"
            raise ValueError(msg)
        bindings[key] = (mapping.host_ip, mapping.host_port)
    return bindings


@dataclass
class SandboxConfig:
    """Configuration for a sandbox container."""

    backend: str = "docker"
    image: str = "agent-forge-sandbox:latest"
    toolchain: str = "auto"
    dockerfile: str = ""
    build_context: str = ""
    build_args: dict[str, str] = field(default_factory=dict)
    build_target: str | None = None
    setup_command: str = ""
    setup_timeout_seconds: int = 300
    workspace_path: str = "/workspace"
    cpu_limit: float = 1.0
    memory_limit: str = "512m"
    timeout_seconds: int = 300
    network_enabled: bool = False
    writable_cache_mounts: bool = False
    mount_host_toolchains: bool = True
    extra_ro_binds: list[str] = field(default_factory=list)
    published_ports: list[str] = field(default_factory=list)
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

    @property
    def host_workspace_path(self) -> str | None:
        """Return the resolved host-side path mounted at workspace_path, if any."""
        return None

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
