"""Bubblewrap-based sandbox implementation."""

from __future__ import annotations

import asyncio
import contextlib
import os
import platform
import shlex
import shutil
from pathlib import Path
from typing import Final

from agent_forge.llm.errors import SandboxStartupError
from agent_forge.observability import get_logger
from agent_forge.sandbox.base import (
    ExecResult,
    Sandbox,
    SandboxConfig,
    SandboxState,
    parse_published_port,
    parse_published_ports,
    sanitize_sandbox_env,
    validate_ro_bind_path,
)
from agent_forge.sandbox.sessions import LocalPtySessionManager, SessionLimits, SessionSnapshot

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
_SYSTEM_TOOLCHAIN_DIRS: Final[tuple[str, ...]] = ("/nix",)
_USER_TOOLCHAIN_SUBDIRS: Final[tuple[str, ...]] = (
    ".pyenv",
    ".nvm",
    ".fnm",
    ".volta",
    ".asdf",
    ".mise",
    ".local/share/mise",
    ".local/bin",
    ".cargo",
    ".rustup",
    ".go",
    "go",
    ".sdkman",
    ".jenv",
    ".rbenv",
    ".phpenv",
    ".dotnet",
    ".ghcup",
    ".cabal",
    ".stack",
    ".foundry",
    ".solcx",
    ".elixir",
    ".mix",
    ".opam",
    ".juliaup",
    ".deno",
    ".bun",
    ".zig",
    ".nimble",
    ".swiftly",
    "quicklisp",
    "miniconda3",
    "mambaforge",
    "anaconda3",
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
        self._active_processes: set[asyncio.subprocess.Process] = set()
        self._sessions: LocalPtySessionManager | None = None

    @property
    def state(self) -> SandboxState:
        """Current lifecycle state."""
        return self._state

    @property
    def timeout_cap_seconds(self) -> int:
        """Effective command timeout cap exposed to tools."""
        return self._config.timeout_seconds

    @property
    def host_workspace_path(self) -> str | None:
        """Return the resolved host workspace path while running, if any."""
        return self._repo_path

    @staticmethod
    def _validate_start_config(cfg: SandboxConfig) -> None:
        if cfg.published_ports:
            try:
                parse_published_ports(cfg.published_ports)
            except ValueError as exc:
                raise SandboxStartupError(str(exc)) from exc
            if not cfg.network_enabled:
                msg = "Publishing sandbox ports requires sandbox.network_enabled=True (--network)."
                raise SandboxStartupError(msg)
            remapped = [
                spec
                for spec in cfg.published_ports
                if (m := parse_published_port(spec)).host_port != m.container_port
            ]
            if remapped:
                logger.warning(
                    "bwrap_port_remapping_unsupported",
                    ports=remapped,
                    detail="bubblewrap shares host networking directly when network_enabled=True",
                )
        for raw_bind in cfg.extra_ro_binds:
            try:
                validate_ro_bind_path(raw_bind)
            except ValueError as exc:
                raise SandboxStartupError(str(exc)) from exc

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
        self._validate_start_config(cfg)
        self._config = cfg
        self._repo_path = str(Path(repo_path).resolve())
        self._sessions = LocalPtySessionManager(
            limits=SessionLimits(default_max_duration_seconds=cfg.timeout_seconds)
        )
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
            env={},
        )
        self._active_processes.add(process)

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
        except asyncio.CancelledError:
            process.kill()
            with contextlib.suppress(BaseException):
                await process.communicate()
            raise
        finally:
            self._active_processes.discard(process)

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
        """Kill active child processes and reset the logical sandbox state."""
        await self.close_all_sessions()
        for process in list(self._active_processes):
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                with contextlib.suppress(BaseException):
                    await process.communicate()
        self._active_processes.clear()
        self._repo_path = None
        self._state = SandboxState.STOPPED
        logger.info("sandbox_stopped", backend="bwrap")

    async def is_alive(self) -> bool:
        """Check if the sandbox has been started and not yet stopped."""
        return self._state == SandboxState.RUNNING and self._repo_path is not None

    async def start_session(
        self,
        command: str,
        *,
        pty: bool = True,
        max_duration_seconds: int | None = None,
        wait_seconds: float = 0.5,
        max_bytes: int = 32 * 1024,
    ) -> SessionSnapshot:
        """Start a resumable PTY or pipe session inside bubblewrap."""
        self._ensure_running()
        assert self._sessions is not None  # noqa: S101
        argv = self._build_bwrap_argv(command)
        return await self._sessions.start(
            argv,
            command_label=command,
            env={},
            pty_enabled=pty,
            max_duration_seconds=max_duration_seconds,
            initial_wait_seconds=wait_seconds,
            max_bytes=max_bytes,
        )

    async def poll_session(
        self,
        session_id: str,
        *,
        wait_seconds: float = 0.5,
        offset: int | None = None,
        max_bytes: int = 32 * 1024,
    ) -> SessionSnapshot:
        """Poll a bubblewrap session for new or paginated output."""
        self._ensure_running()
        assert self._sessions is not None  # noqa: S101
        return await self._sessions.poll(
            session_id,
            wait_seconds=wait_seconds,
            offset=offset,
            max_bytes=max_bytes,
        )

    async def write_session(
        self,
        session_id: str,
        data: str,
        *,
        wait_seconds: float = 0.5,
        max_bytes: int = 32 * 1024,
    ) -> SessionSnapshot:
        """Write input bytes to a running bubblewrap session."""
        self._ensure_running()
        assert self._sessions is not None  # noqa: S101
        return await self._sessions.write(
            session_id,
            data,
            wait_seconds=wait_seconds,
            max_bytes=max_bytes,
        )

    async def cancel_session(self, session_id: str) -> SessionSnapshot:
        """Terminate a bubblewrap session and its process group."""
        self._ensure_running()
        assert self._sessions is not None  # noqa: S101
        return await self._sessions.cancel(session_id)

    async def list_sessions(self) -> list[SessionSnapshot]:
        """List snapshots of tracked bubblewrap sessions."""
        if self._sessions is None:
            return []
        return await self._sessions.list_sessions()

    async def close_all_sessions(self) -> None:
        """Cancel and reap all bubblewrap sessions."""
        if self._sessions is not None:
            try:
                await self._sessions.close_all()
            finally:
                self._sessions = None

    def _collect_toolchain_ro_binds(self) -> list[str]:
        candidates: list[Path] = []
        if self._config.mount_host_toolchains:
            for sys_dir in _SYSTEM_TOOLCHAIN_DIRS:
                candidates.append(Path(sys_dir))
            home_dir = Path.home()
            for subdir in _USER_TOOLCHAIN_SUBDIRS:
                candidates.append(home_dir / subdir)
        for extra in self._config.extra_ro_binds:
            candidates.append(Path(validate_ro_bind_path(extra)))

        seen = set(_READ_ONLY_DIRS)
        binds: list[str] = []
        for path in candidates:
            if not path.exists():
                continue
            resolved = str(path.resolve())
            if resolved in seen:
                continue
            seen.add(resolved)
            binds.append(resolved)
        return binds

    def _build_bwrap_argv(self, command: str) -> list[str]:
        assert self._repo_path is not None  # noqa: S101
        assert self._bwrap_path is not None  # noqa: S101

        argv = [
            self._bwrap_path,
            "--clearenv",
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

        for toolchain_path in self._collect_toolchain_ro_binds():
            argv.extend(["--ro-bind", toolchain_path, toolchain_path])

        argv.extend(
            [
                "--bind",
                self._repo_path,
                self._config.workspace_path,
                "--remount-ro",
                "/",
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
        env_vars.update(sanitize_sandbox_env(self._config.env_vars))
        if self._config.network_enabled and self._config.writable_cache_mounts:
            env_vars.setdefault("HOME", "/cache/home")
            env_vars.setdefault("XDG_CACHE_HOME", "/cache/xdg")
            env_vars.setdefault("PIP_CACHE_DIR", "/cache/pip")
            env_vars.setdefault("NPM_CONFIG_CACHE", "/cache/npm")
            env_vars.setdefault("YARN_CACHE_FOLDER", "/cache/yarn")
            env_vars.setdefault("PNPM_HOME", "/cache/pnpm-home")
            env_vars.setdefault("PNPM_STORE_DIR", "/cache/pnpm-store")
            env_vars.setdefault("GOCACHE", "/cache/go-build")
            env_vars.setdefault("GOMODCACHE", "/cache/go-mod")
            env_vars.setdefault("CARGO_HOME", "/cache/cargo")
            env_vars.setdefault("GRADLE_USER_HOME", "/cache/gradle")
        else:
            env_vars.setdefault("HOME", "/tmp/home")  # noqa: S108
            env_vars.setdefault("XDG_CACHE_HOME", "/tmp/xdg")  # noqa: S108
        return env_vars

    def _ensure_running(self) -> None:
        if self._state != SandboxState.RUNNING or self._repo_path is None:
            msg = "Sandbox is not running. Call start() first."
            raise RuntimeError(msg)
