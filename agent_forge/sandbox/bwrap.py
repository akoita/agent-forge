"""Bubblewrap-based sandbox implementation."""

from __future__ import annotations

import asyncio
import os
import platform
import shlex
import shutil
from pathlib import Path
from typing import Final

from agent_forge.llm.errors import SandboxStartupError
from agent_forge.observability import get_logger
from agent_forge.sandbox.base import ExecResult, Sandbox, SandboxConfig, SandboxState

logger = get_logger("sandbox")

_READ_ONLY_DIRS: Final[tuple[str, ...]] = (
    "/bin",
    "/etc",
    "/lib",
    "/lib64",
    "/opt",
    "/sbin",
    "/usr",
)


class BwrapSandbox(Sandbox):
    """Bubblewrap-backed sandbox for fast, daemonless isolation on Linux.

    The sandbox keeps only configuration state between calls. Each ``exec``
    launches a fresh ``bwrap`` process over the same workspace mount.
    """

    def __init__(self) -> None:
        self._config = SandboxConfig(backend="bwrap")
        self._state = SandboxState.IDLE
        self._repo_path: str | None = None
        self._bwrap_path = shutil.which("bwrap")

    @property
    def state(self) -> SandboxState:
        """Current lifecycle state."""
        return self._state

    @property
    def timeout_cap_seconds(self) -> int:
        """Effective command timeout cap exposed to tools."""
        return self._config.timeout_seconds

    async def start(self, repo_path: str, config: SandboxConfig | None = None) -> None:
        """Validate availability and cache the sandbox configuration."""
        if self._state == SandboxState.RUNNING:
            msg = "Sandbox is already running"
            raise RuntimeError(msg)

        if platform.system() != "Linux":
            msg = "bubblewrap sandbox is only available on Linux"
            raise SandboxStartupError(msg)
        if self._bwrap_path is None:
            msg = "bubblewrap sandbox requires the 'bwrap' executable"
            raise SandboxStartupError(msg)

        cfg = config or SandboxConfig(backend="bwrap")
        self._config = cfg
        self._repo_path = str(Path(repo_path).resolve())
        self._state = SandboxState.RUNNING
        logger.info("sandbox_started", backend="bwrap", repo_path=self._repo_path)

    async def exec(
        self,
        command: str,
        *,
        timeout_seconds: int = 30,
    ) -> ExecResult:
        """Execute a command inside a fresh bubblewrap process."""
        self._ensure_running()
        argv = self._build_bwrap_argv(command)
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout_raw, stderr_raw = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            process.kill()
            await process.communicate()
            return ExecResult(
                exit_code=124,
                stdout="",
                stderr=f"Command timed out after {timeout_seconds}s",
            )

        return ExecResult(
            exit_code=process.returncode or 0,
            stdout=(stdout_raw or b"").decode("utf-8", errors="replace"),
            stderr=(stderr_raw or b"").decode("utf-8", errors="replace"),
        )

    async def read_file(self, path: str) -> str:
        """Read a file from the sandbox filesystem."""
        result = await self.exec(f"cat {shlex.quote(path)}")
        if result.exit_code != 0:
            msg = f"File not found in sandbox: {path}"
            raise FileNotFoundError(msg)
        return result.stdout

    async def write_file(self, path: str, content: str) -> None:
        """Write content to a file inside the sandbox."""
        quoted_path = shlex.quote(path)
        parent = os.path.dirname(path)
        if parent:
            mkdir_result = await self.exec(f"mkdir -p {shlex.quote(parent)}")
            if mkdir_result.exit_code != 0:
                msg = f"Failed to create parent directory: {parent} — {mkdir_result.stderr}"
                raise OSError(msg)

        escaped = shlex.quote(content)
        result = await self.exec(f"printf %s {escaped} > {quoted_path}")
        if result.exit_code != 0:
            msg = f"Failed to write file: {path} — {result.stderr}"
            raise OSError(msg)

    async def stop(self) -> None:
        """Reset the logical sandbox state."""
        self._repo_path = None
        self._state = SandboxState.STOPPED
        logger.info("sandbox_stopped", backend="bwrap")

    async def is_alive(self) -> bool:
        """Check if the sandbox has been started and not yet stopped."""
        return self._state == SandboxState.RUNNING and self._repo_path is not None

    def _build_bwrap_argv(self, command: str) -> list[str]:
        assert self._repo_path is not None  # noqa: S101
        assert self._bwrap_path is not None  # noqa: S101

        argv = [
            self._bwrap_path,
            "--die-with-parent",
            "--new-session",
            "--unshare-pid",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",  # noqa: S108
        ]
        if not self._config.network_enabled:
            argv.append("--unshare-net")
        if self._config.network_enabled and self._config.writable_cache_mounts:
            argv.extend(["--tmpfs", "/cache"])

        for host_path in _READ_ONLY_DIRS:
            if Path(host_path).exists():
                argv.extend(["--ro-bind", host_path, host_path])

        argv.extend(
            [
                "--bind",
                self._repo_path,
                self._config.workspace_path,
                "--chdir",
                self._config.workspace_path,
            ]
        )

        for key, value in self._build_env_vars().items():
            argv.extend(["--setenv", key, value])

        shell_path = "/bin/bash" if Path("/bin/bash").exists() else "/bin/sh"
        argv.extend([shell_path, "-lc", command])
        return argv

    def _build_env_vars(self) -> dict[str, str]:
        env_vars = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        env_vars.update(self._config.env_vars)
        if self._config.network_enabled and self._config.writable_cache_mounts:
            env_vars.setdefault("HOME", "/cache/home")
            env_vars.setdefault("XDG_CACHE_HOME", "/cache/xdg")
            env_vars.setdefault("PIP_CACHE_DIR", "/cache/pip")
            env_vars.setdefault("NPM_CONFIG_CACHE", "/cache/npm")
            env_vars.setdefault("YARN_CACHE_FOLDER", "/cache/yarn")
            env_vars.setdefault("PNPM_HOME", "/cache/pnpm-home")
            env_vars.setdefault("PNPM_STORE_DIR", "/cache/pnpm-store")
        return env_vars

    def _ensure_running(self) -> None:
        if self._state != SandboxState.RUNNING or self._repo_path is None:
            msg = "Sandbox is not running. Call start() first."
            raise RuntimeError(msg)
