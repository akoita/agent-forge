"""Docker-based sandbox implementation.

Uses the Docker SDK to manage isolated containers for tool execution.
Implements security constraints per spec § 4.3.
"""

from __future__ import annotations

import asyncio
import os
import shlex
from typing import TYPE_CHECKING, Any, cast

import docker
from docker.errors import APIError, NotFound

from agent_forge.llm.errors import SandboxStartupError
from agent_forge.observability import get_logger
from agent_forge.sandbox.base import (
    ExecResult,
    Sandbox,
    SandboxConfig,
    SandboxState,
    sanitize_sandbox_env,
)
from agent_forge.sandbox.docker_sessions import DockerSessionManager
from agent_forge.sandbox.sessions import SessionLimits, SessionSnapshot

if TYPE_CHECKING:
    from docker.models.containers import Container

logger = get_logger("sandbox")

# Retry policy for sandbox start (spec § 7.2)
_START_MAX_RETRIES = 2
_START_RETRY_DELAY_S = 5.0


class DockerSandbox(Sandbox):
    """Docker-based sandbox for isolated tool execution.

    Each instance manages a single container with security constraints:
    - No host network (``--network none``)
    - Read-only root filesystem (``--read-only``)
    - No new privileges (``--security-opt no-new-privileges``)
    - PID limit (``--pids-limit 256``)
    - Tmpfs for ``/tmp`` (``noexec``, ``nosuid``, 64m)
    - CPU and memory limits from config
    """

    def __init__(self) -> None:
        self._client: docker.DockerClient = docker.from_env()
        self._container: Container | None = None
        self._state = SandboxState.IDLE
        self._config = SandboxConfig()
        self._sessions: DockerSessionManager | None = None
        self._repo_path: str | None = None
        self._host_uid = os.getuid()
        self._host_gid = os.getgid()

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
        """Return the resolved host workspace path mounted into the container."""
        return self._repo_path

    @property
    def container_id(self) -> str | None:
        """Return the underlying Docker container ID while active, if any."""
        return str(self._container.id) if self._container is not None else None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self, repo_path: str, config: SandboxConfig | None = None) -> None:
        """Create and start the sandbox container.

        Retries up to 2 times with a 5s delay if the Docker daemon is
        temporarily unavailable (spec § 7.2).
        """
        if self._state == SandboxState.RUNNING:
            msg = "Sandbox is already running"
            raise RuntimeError(msg)

        cfg = config or SandboxConfig()
        self._config = cfg

        security_opts: list[str] = ["no-new-privileges"]
        tmpfs = self._build_tmpfs(cfg)
        environment = sanitize_sandbox_env(cfg.env_vars)
        if cfg.network_enabled and cfg.writable_cache_mounts:
            environment.setdefault("HOME", "/cache/home")
            environment.setdefault("XDG_CACHE_HOME", "/cache/xdg")
            environment.setdefault("PIP_CACHE_DIR", "/cache/pip")
            environment.setdefault("NPM_CONFIG_CACHE", "/cache/npm")
            environment.setdefault("YARN_CACHE_FOLDER", "/cache/yarn")
            environment.setdefault("PNPM_HOME", "/cache/pnpm-home")
            environment.setdefault("PNPM_STORE_DIR", "/cache/pnpm-store")

        # Parse memory limit to bytes for Docker SDK
        mem_limit = cfg.memory_limit

        run_kwargs: dict[str, Any] = {
            "image": cfg.image,
            "command": "sleep infinity",
            "detach": True,
            "init": True,
            "remove": True,
            "read_only": True,
            "cap_drop": ["ALL"],
            "security_opt": security_opts,
            "pids_limit": 256,
            "tmpfs": tmpfs,
            "nano_cpus": int(cfg.cpu_limit * 1e9),
            "mem_limit": mem_limit,
            "environment": environment,
            "volumes": {
                repo_path: {
                    "bind": cfg.workspace_path,
                    "mode": "rw",
                }
            },
            "working_dir": cfg.workspace_path,
        }

        # Network isolation
        if not cfg.network_enabled:
            run_kwargs["network_mode"] = "none"

        # Run as the host user so files are owned by them (avoids PermissionError
        # when the host reads workspace files after the container writes them).
        run_kwargs["user"] = f"{self._host_uid}:{self._host_gid}"

        last_exc: Exception | None = None
        for attempt in range(_START_MAX_RETRIES + 1):
            try:
                self._container = await asyncio.to_thread(
                    lambda: self._client.containers.run(**run_kwargs)
                )
                assert self._container is not None  # noqa: S101
                self._sessions = DockerSessionManager(
                    self._client,
                    container_id=str(self._container.id),
                    user=f"{self._host_uid}:{self._host_gid}",
                    limits=SessionLimits(default_max_duration_seconds=cfg.timeout_seconds),
                )

                self._repo_path = os.path.realpath(repo_path)
                self._state = SandboxState.RUNNING
                logger.info(
                    "sandbox_started",
                    container=self._container.short_id,
                    image=cfg.image,
                    attempt=attempt + 1,
                )
                return
            except (APIError, Exception) as exc:  # noqa: BLE001
                last_exc = exc
                if attempt < _START_MAX_RETRIES:
                    logger.warning(
                        "sandbox_start_retry",
                        attempt=attempt + 1,
                        delay_s=_START_RETRY_DELAY_S,
                        error=str(exc),
                    )
                    await asyncio.sleep(_START_RETRY_DELAY_S)

        self._state = SandboxState.STOPPED
        msg = (
            f"Failed to start sandbox container after {_START_MAX_RETRIES + 1} attempts: {last_exc}"
        )
        raise SandboxStartupError(msg) from last_exc

    async def stop(self) -> None:
        """Stop and remove the sandbox container."""
        if self._container is None:
            self._state = SandboxState.STOPPED
            return

        try:
            await self.close_all_sessions()
            await asyncio.to_thread(self._container.stop, timeout=2)
        except NotFound:
            # Container already removed (--rm flag)
            pass
        except APIError as exc:
            logger.warning("container_stop_error_forcing_remove", error=str(exc))
            try:
                await asyncio.to_thread(self._container.remove, force=True)
            except NotFound:
                pass
            except APIError as remove_exc:
                logger.warning("container_force_remove_error", error=str(remove_exc))
        finally:
            self._container = None
            self._repo_path = None
            self._state = SandboxState.STOPPED
            logger.info("sandbox_stopped")

    async def is_alive(self) -> bool:
        """Check if the sandbox container is still running."""
        if self._container is None:
            return False
        try:
            await asyncio.to_thread(self._container.reload)
            status: str = self._container.status
            return status == "running"
        except (NotFound, APIError):
            self._state = SandboxState.STOPPED
            return False

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def exec(
        self,
        command: str,
        *,
        timeout_seconds: int = 30,
    ) -> ExecResult:
        """Execute a command inside the running sandbox."""
        self._ensure_running()
        assert self._container is not None  # noqa: S101

        try:
            exit_code, output = await asyncio.wait_for(
                asyncio.to_thread(
                    self._container.exec_run,
                    ["bash", "-c", command],
                    demux=True,
                    user=f"{self._host_uid}:{self._host_gid}",
                ),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            return ExecResult(
                exit_code=124,
                stdout="",
                stderr=f"Command timed out after {timeout_seconds}s",
            )
        except APIError as exc:
            return ExecResult(
                exit_code=1,
                stdout="",
                stderr=f"Docker exec error: {exc}",
            )

        output_tuple = cast("tuple[bytes | None, bytes | None] | None", output)
        stdout_raw, stderr_raw = output_tuple or (None, None)
        normalized_exit_code = int(exit_code) if exit_code is not None else 1
        return ExecResult(
            exit_code=normalized_exit_code,
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
        # Create parent directories first
        parent = "/".join(path.rsplit("/", 1)[:-1])
        if parent:
            mkdir_result = await self.exec(f"mkdir -p {shlex.quote(parent)}")
            if mkdir_result.exit_code != 0:
                msg = f"Failed to create parent directory: {parent} — {mkdir_result.stderr}"
                raise OSError(msg)

        # Pass content via quoted shell argument
        self._ensure_running()
        assert self._container is not None  # noqa: S101

        escaped = shlex.quote(content)
        result = await self.exec(f"printf %s {escaped} > {shlex.quote(path)}")
        if result.exit_code != 0:
            msg = f"Failed to write file: {path} — {result.stderr}"
            raise OSError(msg)

    async def start_session(
        self,
        command: str,
        *,
        pty: bool = True,
        max_duration_seconds: int | None = None,
        wait_seconds: float = 0.5,
        max_bytes: int = 32 * 1024,
    ) -> SessionSnapshot:
        """Start a resumable PTY or pipe session inside the running container."""
        self._ensure_running()
        assert self._sessions is not None  # noqa: S101
        return await self._sessions.start(
            command,
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
        """Poll a container session for new or paginated output."""
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
        """Write input bytes to a running container session."""
        self._ensure_running()
        assert self._sessions is not None  # noqa: S101
        return await self._sessions.write(
            session_id,
            data,
            wait_seconds=wait_seconds,
            max_bytes=max_bytes,
        )

    async def cancel_session(self, session_id: str) -> SessionSnapshot:
        """Cancel a container session and kill its process group."""
        self._ensure_running()
        assert self._sessions is not None  # noqa: S101
        return await self._sessions.cancel(session_id)

    async def list_sessions(self) -> list[SessionSnapshot]:
        """List snapshots of tracked container sessions."""
        if self._sessions is None:
            return []
        return await self._sessions.list_sessions()

    async def close_all_sessions(self) -> None:
        """Cancel any active container sessions."""
        if self._sessions is not None:
            try:
                await self._sessions.close_all()
            finally:
                self._sessions = None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _ensure_running(self) -> None:
        """Raise if the sandbox is not in RUNNING state."""
        if self._state != SandboxState.RUNNING or self._container is None:
            msg = "Sandbox is not running. Call start() first."
            raise RuntimeError(msg)

    def _build_tmpfs(self, cfg: SandboxConfig) -> dict[str, str]:
        """Build tmpfs mounts for scratch space and optional package caches."""
        tmpfs = {
            "/tmp": self._tmpfs_options(size="128m", executable=False),  # noqa: S108
        }
        if cfg.network_enabled and cfg.writable_cache_mounts:
            tmpfs["/cache"] = self._tmpfs_options(size="1g", executable=True)
        return tmpfs

    def _tmpfs_options(self, *, size: str, executable: bool) -> str:
        exec_flag = "exec" if executable else "noexec"
        return (
            f"rw,{exec_flag},nosuid,nodev,size={size},"
            f"uid={self._host_uid},gid={self._host_gid},mode=1777"
        )
