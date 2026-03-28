"""Sandbox backend selection helpers."""

from __future__ import annotations

import platform
import shutil
from typing import Final

import docker

from agent_forge.llm.errors import SandboxStartupError
from agent_forge.sandbox.base import Sandbox, SandboxConfig
from agent_forge.sandbox.bwrap import BwrapSandbox
from agent_forge.sandbox.docker import DockerSandbox

_VALID_BACKENDS: Final[set[str]] = {"auto", "bwrap", "docker"}


def create_sandbox(config: SandboxConfig | None = None) -> Sandbox:
    """Create the configured sandbox backend.

    ``auto`` prefers Docker when the daemon is reachable and otherwise falls
    back to bubblewrap on Linux.
    """

    cfg = config or SandboxConfig()
    backend = cfg.backend
    if backend not in _VALID_BACKENDS:
        msg = f"Unknown sandbox backend: {backend}"
        raise SandboxStartupError(msg)

    if backend == "docker":
        return DockerSandbox()
    if backend == "bwrap":
        return BwrapSandbox()
    if _docker_available():
        return DockerSandbox()
    if _bwrap_available():
        return BwrapSandbox()
    msg = "No supported sandbox backend is available (tried Docker, then bubblewrap)"
    raise SandboxStartupError(msg)


def _docker_available() -> bool:
    """Return True when the Docker daemon is reachable."""
    try:
        client = docker.from_env()
        try:
            client.ping()
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()
    except Exception:  # noqa: BLE001
        return False
    return True


def _bwrap_available() -> bool:
    """Return True when bubblewrap is installed on Linux."""
    return platform.system() == "Linux" and shutil.which("bwrap") is not None
