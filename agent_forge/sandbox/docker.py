"""Docker-based sandbox implementation.

Uses the Docker SDK to manage isolated containers for tool execution.
Implements security constraints per spec § 4.3.
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING, Any

import docker
from docker.errors import APIError, NotFound

from agent_forge.llm.errors import SandboxStartupError
from agent_forge.observability import get_logger
from agent_forge.sandbox.base import ExecResult, Sandbox, SandboxConfig, SandboxState

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
        self._host_uid = os.getuid()
        self._host_gid = os.getgid()

    @property
    def state(self) -> SandboxState:
        """Current lifecycle state."""
        return self._state

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
        tmpfs: dict[str, str] = {"/tmp": "rw,noexec,nosuid,size=64m"}

        # Parse memory limit to bytes for Docker SDK
        mem_limit = cfg.memory_limit

        run_kwargs: dict[str, Any] = {
            "image": cfg.image,
            "command": "sleep infinity",
            "detach": True,
            "remove": True,
            "read_only": True,
            "security_opt": security_opts,
            "pids_limit": 256,
            "tmpfs": tmpfs,
            "nano_cpus": int(cfg.cpu_limit * 1e9),
            "mem_limit": mem_limit,
            "environment": cfg.env_vars,
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

                self._state = SandboxState.RUNNING
                logger.info(
                    "sandbox_started",
                    container=self._container.short_id,  # type: ignore[union-attr]
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
            f"Failed to start sandbox container after "
            f"{_START_MAX_RETRIES + 1} attempts: {last_exc}"
        )
        raise SandboxStartupError(msg) from last_exc

    async def stop(self) -> None:
        """Stop and remove the sandbox container."""
        if self._container is None:
            self._state = SandboxState.STOPPED
            return

        try:
            await asyncio.to_thread(self._container.stop, timeout=5)
        except NotFound:
            # Container already removed (--rm flag)
            pass
        except APIError as exc:
            logger.warning("container_stop_error", error=str(exc))
        finally:
            self._container = None
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

        stdout_raw, stderr_raw = output or (None, None)
        return ExecResult(
            exit_code=exit_code,
            stdout=(stdout_raw or b"").decode("utf-8", errors="replace"),
            stderr=(stderr_raw or b"").decode("utf-8", errors="replace"),
        )

    async def read_file(self, path: str) -> str:
        """Read a file from the sandbox filesystem."""
        result = await self.exec(f"cat {path}")
        if result.exit_code != 0:
            msg = f"File not found in sandbox: {path}"
            raise FileNotFoundError(msg)
        return result.stdout

    async def write_file(self, path: str, content: str) -> None:
        """Write content to a file inside the sandbox."""
        # Create parent directories first
        parent = "/".join(path.rsplit("/", 1)[:-1])
        if parent:
            await self.exec(f"mkdir -p {parent}")

        # Use tee to write (handles special characters better than echo)
        # Pass content via stdin using exec_run with stdin
        self._ensure_running()
        assert self._container is not None  # noqa: S101

        # Use a heredoc-style approach for content with special chars
        import shlex

        escaped = shlex.quote(content)
        result = await self.exec(f"printf %s {escaped} > {path}")
        if result.exit_code != 0:
            msg = f"Failed to write file: {path} — {result.stderr}"
            raise OSError(msg)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _ensure_running(self) -> None:
        """Raise if the sandbox is not in RUNNING state."""
        if self._state != SandboxState.RUNNING or self._container is None:
            msg = "Sandbox is not running. Call start() first."
            raise RuntimeError(msg)
