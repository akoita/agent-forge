"""Docker-based sandbox implementation.

Uses the Docker SDK to manage isolated containers for tool execution.
Implements security constraints per spec § 4.3.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
from pathlib import Path
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
    apply_sandbox_cache_env,
    create_setup_cache_dir,
    parse_published_ports,
    sanitize_sandbox_env,
)
from agent_forge.sandbox.devcontainer import DevcontainerSpec, ensure_dockerfile_image
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
        self._setup_cache_dir: str | None = None
        self._shell: str = "bash"
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

    async def _prepare_image(self, cfg: SandboxConfig, repo_path: str) -> str:
        if not cfg.dockerfile:
            return cfg.image
        resolved_repo = Path(repo_path).resolve()
        raw_df = Path(cfg.dockerfile)
        df_path = raw_df.resolve() if raw_df.is_absolute() else (resolved_repo / raw_df).resolve()
        raw_ctx = Path(cfg.build_context) if cfg.build_context else resolved_repo
        ctx_path = (
            raw_ctx.resolve() if raw_ctx.is_absolute() else (resolved_repo / raw_ctx).resolve()
        )
        spec = DevcontainerSpec(
            source_path=df_path,
            dockerfile=df_path,
            build_context=ctx_path,
            build_args=dict(cfg.build_args),
            build_target=cfg.build_target,
        )
        return await asyncio.to_thread(ensure_dockerfile_image, self._client, spec)

    async def _run_setup_phase(self, repo_path: str, image: str, cfg: SandboxConfig) -> str:
        cache_dir = create_setup_cache_dir()
        setup_container: Container | None = None
        run_kwargs = self._build_container_run_kwargs(
            image=image,
            cfg=cfg,
            repo_path=repo_path,
            network_enabled=True,
            published_ports={},
            setup_cache_dir=cache_dir,
        )
        try:
            setup_container = await asyncio.to_thread(
                lambda: self._client.containers.run(**run_kwargs)
            )
            assert setup_container is not None  # noqa: S101
            result, _ = await self._exec_container_with_fallback(
                setup_container,
                cfg.setup_command,
                timeout_seconds=cfg.setup_timeout_seconds,
            )
        except Exception as exc:
            shutil.rmtree(cache_dir, ignore_errors=True)
            if isinstance(exc, SandboxStartupError):
                raise
            msg = f"Sandbox setup_command failed: {exc}"
            raise SandboxStartupError(msg) from exc
        finally:
            if setup_container is not None:
                await self._stop_container_instance(setup_container)

        if result.exit_code != 0:
            shutil.rmtree(cache_dir, ignore_errors=True)
            detail = (result.stderr or result.stdout).strip()
            msg = f"Sandbox setup_command failed (exit {result.exit_code}): {detail}"
            raise SandboxStartupError(msg)
        return cache_dir

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
        self._shell = "bash"

        published_ports = self._resolve_published_ports(cfg)
        image = await self._prepare_image(cfg, repo_path)
        self._setup_cache_dir = (
            await self._run_setup_phase(repo_path, image, cfg)
            if cfg.setup_command.strip()
            else None
        )
        run_kwargs = self._build_container_run_kwargs(
            image=image,
            cfg=cfg,
            repo_path=repo_path,
            network_enabled=cfg.network_enabled,
            published_ports=published_ports,
            setup_cache_dir=self._setup_cache_dir,
        )

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
                    image=image,
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

        self._cleanup_setup_cache()
        self._state = SandboxState.STOPPED
        msg = (
            f"Failed to start sandbox container after {_START_MAX_RETRIES + 1} attempts: {last_exc}"
        )
        raise SandboxStartupError(msg) from last_exc

    @staticmethod
    async def _stop_container_instance(container: Container) -> None:
        try:
            await asyncio.to_thread(container.stop, timeout=2)
        except NotFound:
            pass
        except APIError as exc:
            logger.warning("container_stop_error_forcing_remove", error=str(exc))
            try:
                await asyncio.to_thread(container.remove, force=True)
            except NotFound:
                pass
            except APIError as remove_exc:
                logger.warning("container_force_remove_error", error=str(remove_exc))

    def _cleanup_setup_cache(self) -> None:
        if self._setup_cache_dir is not None:
            shutil.rmtree(self._setup_cache_dir, ignore_errors=True)
            self._setup_cache_dir = None

    async def stop(self) -> None:
        """Stop and remove the sandbox container."""
        if self._container is None:
            self._cleanup_setup_cache()
            self._state = SandboxState.STOPPED
            return

        try:
            await self.close_all_sessions()
            await self._stop_container_instance(self._container)
        finally:
            self._container = None
            self._repo_path = None
            self._cleanup_setup_cache()
            self._shell = "bash"
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

    @staticmethod
    def _is_missing_bash(message: str) -> bool:
        lower = message.lower()
        return "bash" in lower and any(
            marker in lower for marker in ("not found", "no such file", "executable file not found")
        )

    async def _run_shell_command(
        self,
        container: Container,
        shell: str,
        command: str,
        timeout_seconds: int,
    ) -> tuple[ExecResult, bool]:
        try:
            exit_code, output = await asyncio.wait_for(
                asyncio.to_thread(
                    container.exec_run,
                    [shell, "-c", command],
                    demux=True,
                    user=f"{self._host_uid}:{self._host_gid}",
                ),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            return (
                ExecResult(
                    exit_code=124,
                    stdout="",
                    stderr=f"Command timed out after {timeout_seconds}s",
                ),
                False,
            )
        except APIError as exc:
            err_msg = str(exc)
            return (
                ExecResult(exit_code=1, stdout="", stderr=f"Docker exec error: {err_msg}"),
                shell == "bash" and self._is_missing_bash(err_msg),
            )

        output_tuple = cast("tuple[bytes | None, bytes | None] | None", output)
        stdout_raw, stderr_raw = output_tuple or (None, None)
        normalized_exit_code = int(exit_code) if exit_code is not None else 1
        stdout_text = (stdout_raw or b"").decode("utf-8", errors="replace")
        stderr_text = (stderr_raw or b"").decode("utf-8", errors="replace")
        missing_bash = (
            shell == "bash"
            and normalized_exit_code in (126, 127)
            and not stdout_text
            and self._is_missing_bash(stderr_text)
        )
        return ExecResult(normalized_exit_code, stdout_text, stderr_text), missing_bash

    async def _exec_container_with_fallback(
        self,
        container: Container,
        command: str,
        *,
        timeout_seconds: int,
    ) -> tuple[ExecResult, bool]:
        result, retry_with_sh = await self._run_shell_command(
            container, self._shell, command, timeout_seconds
        )
        if retry_with_sh:
            self._shell = "sh"
            result, _ = await self._run_shell_command(container, "sh", command, timeout_seconds)
        return result, retry_with_sh

    async def exec(
        self,
        command: str,
        *,
        timeout_seconds: int = 30,
    ) -> ExecResult:
        """Execute a command inside the running sandbox."""
        self._ensure_running()
        assert self._container is not None  # noqa: S101
        result, _ = await self._exec_container_with_fallback(
            self._container,
            command,
            timeout_seconds=timeout_seconds,
        )
        return result

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

    def _resolve_published_ports(self, cfg: SandboxConfig) -> dict[str, tuple[str, int]]:
        if not cfg.published_ports:
            return {}
        try:
            ports = parse_published_ports(cfg.published_ports)
        except ValueError as exc:
            raise SandboxStartupError(str(exc)) from exc
        if not cfg.network_enabled:
            msg = "Publishing sandbox ports requires sandbox.network_enabled=True (--network)."
            raise SandboxStartupError(msg)
        return ports

    def _build_container_run_kwargs(
        self,
        *,
        image: str,
        cfg: SandboxConfig,
        repo_path: str,
        network_enabled: bool,
        published_ports: dict[str, tuple[str, int]],
        setup_cache_dir: str | None,
    ) -> dict[str, Any]:
        use_cache = setup_cache_dir is not None or (network_enabled and cfg.writable_cache_mounts)
        tmpfs = self._build_tmpfs(
            cfg,
            network_enabled=network_enabled,
            mount_cache_tmpfs=setup_cache_dir is None,
        )
        environment = self._build_environment(cfg, use_cache=use_cache)
        volumes: dict[str, dict[str, str]] = {
            repo_path: {"bind": cfg.workspace_path, "mode": "rw"},
        }
        if setup_cache_dir is not None:
            volumes[setup_cache_dir] = {"bind": "/cache", "mode": "rw"}

        run_kwargs: dict[str, Any] = {
            "image": image,
            "entrypoint": "",
            "command": ["sh", "-c", "while :; do sleep 3600; done"],
            "detach": True,
            "init": True,
            "remove": True,
            "read_only": True,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges"],
            "pids_limit": 256,
            "tmpfs": tmpfs,
            "nano_cpus": int(cfg.cpu_limit * 1e9),
            "mem_limit": cfg.memory_limit,
            "environment": environment,
            "volumes": volumes,
            "working_dir": cfg.workspace_path,
            "user": f"{self._host_uid}:{self._host_gid}",
        }
        if not network_enabled:
            run_kwargs["network_mode"] = "none"
        elif published_ports:
            run_kwargs["ports"] = published_ports
        return run_kwargs

    def _build_environment(
        self,
        cfg: SandboxConfig,
        *,
        use_cache: bool | None = None,
    ) -> dict[str, str]:
        environment = sanitize_sandbox_env(cfg.env_vars)
        effective_cache = (
            use_cache
            if use_cache is not None
            else (cfg.network_enabled and cfg.writable_cache_mounts)
        )
        apply_sandbox_cache_env(environment, use_cache=effective_cache)
        return environment

    def _build_tmpfs(
        self,
        cfg: SandboxConfig,
        *,
        network_enabled: bool | None = None,
        mount_cache_tmpfs: bool = True,
    ) -> dict[str, str]:
        """Build tmpfs mounts for scratch space and optional package caches."""
        net_enabled = cfg.network_enabled if network_enabled is None else network_enabled
        tmpfs = {
            "/tmp": self._tmpfs_options(size="128m", executable=False),  # noqa: S108
        }
        if mount_cache_tmpfs and net_enabled and cfg.writable_cache_mounts:
            tmpfs["/cache"] = self._tmpfs_options(size="1g", executable=True)
        return tmpfs

    def _tmpfs_options(self, *, size: str, executable: bool) -> str:
        exec_flag = "exec" if executable else "noexec"
        return (
            f"rw,{exec_flag},nosuid,nodev,size={size},"
            f"uid={self._host_uid},gid={self._host_gid},mode=1777"
        )
