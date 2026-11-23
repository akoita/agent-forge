"""Docker container PTY and pipe session manager."""

from __future__ import annotations

import asyncio
import contextlib
import select
import shlex
import struct
import threading
import time
import uuid
from typing import TYPE_CHECKING, Any

from docker.errors import APIError

from agent_forge.sandbox.sessions import (
    BoundedOutputSpool,
    ManagedSessionBase,
    SessionLimits,
    SessionSnapshot,
    build_snapshot,
    cancel_watchdog_task,
    check_session_capacity,
    schedule_session_timeout,
    wait_for_session_activity,
)

if TYPE_CHECKING:
    from collections.abc import Callable


class _DockerSession(ManagedSessionBase):
    """Per-exec Docker session state."""

    def __init__(
        self,
        *,
        session_id: str,
        command: str,
        pty: bool,
        spool: BoundedOutputSpool,
        started_at: float,
        max_duration_seconds: int,
        exec_id: str,
        sock: Any,
        raw_sock: Any,
    ) -> None:
        super().__init__(
            session_id=session_id,
            command=command,
            pty=pty,
            spool=spool,
            started_at=started_at,
            max_duration_seconds=max_duration_seconds,
        )
        self.exec_id = exec_id
        self.sock = sock
        self.raw_sock = raw_sock
        self.reader_thread: threading.Thread | None = None
        self.last_inspect_at = 0.0


def _recv_exact(raw_sock: Any, length: int, stop_event: threading.Event) -> bytes | None:
    """Read ``length`` bytes from non-blocking socket or return ``None`` on EOF/stop."""
    chunks = bytearray()
    while len(chunks) < length and not stop_event.is_set():
        try:
            ready, _, _ = select.select([raw_sock], [], [], 0.05)
        except (OSError, ValueError):
            return None
        if not ready:
            continue
        try:
            part = raw_sock.recv(length - len(chunks))
        except BlockingIOError:
            continue
        except OSError:
            return None
        if not part:
            return None
        chunks.extend(part)
    if len(chunks) < length:
        return None
    return bytes(chunks)


def _pgid_file(session_id: str) -> str:
    return f"/tmp/.af_sess_{session_id}.pgid"  # noqa: S108 - container-local tmpfs


def _drain_pty_once(raw_sock: Any, spool: BoundedOutputSpool) -> bool:
    """Read one non-blocking PTY chunk into ``spool``; return False on EOF/error."""
    try:
        ready, _, _ = select.select([raw_sock], [], [], 0.05)
    except (OSError, ValueError):
        return False
    if not ready:
        return True
    try:
        chunk = raw_sock.recv(8192)
    except BlockingIOError:
        return True
    except OSError:
        return False
    if not chunk:
        return False
    spool.append(chunk)
    return True


def _drain_docker_socket(
    raw_sock: Any,
    *,
    pty_enabled: bool,
    spool: BoundedOutputSpool,
    stop_event: threading.Event,
    on_eof: Callable[[], None],
) -> None:
    """Drain Docker exec socket output (raw for TTY, 8-byte framed otherwise)."""
    try:
        while not stop_event.is_set():
            if pty_enabled:
                if not _drain_pty_once(raw_sock, spool):
                    break
                continue

            header = _recv_exact(raw_sock, 8, stop_event)
            if header is None:
                break
            _, payload_size = struct.unpack(">BxxxI", header)
            if payload_size == 0:
                continue
            payload = _recv_exact(raw_sock, payload_size, stop_event)
            if payload is None:
                break
            spool.append(payload)
    finally:
        on_eof()


class DockerSessionManager:
    """Manage interactive PTY and background exec sessions inside a Docker container."""

    def __init__(
        self,
        client: Any,
        *,
        container_id: str,
        user: str,
        limits: SessionLimits | None = None,
    ) -> None:
        self._client = client
        self._container_id = container_id
        self._user = user
        self._limits = limits or SessionLimits()
        self._sessions: dict[str, _DockerSession] = {}
        self._stop_events: dict[str, threading.Event] = {}

    async def start(
        self,
        command: str,
        *,
        pty_enabled: bool = True,
        max_duration_seconds: int | None = None,
        initial_wait_seconds: float = 0.5,
        max_bytes: int | None = None,
    ) -> SessionSnapshot:
        """Create and start a Docker exec session with PGID tracking."""
        check_session_capacity(self._sessions, self._limits, self._refresh_session)
        session_id = f"sess-{uuid.uuid4().hex[:8]}"
        duration = max_duration_seconds or self._limits.default_max_duration_seconds
        spool = BoundedOutputSpool(self._limits.max_retained_bytes)
        stop_event = threading.Event()
        self._stop_events[session_id] = stop_event

        pgid_file = _pgid_file(session_id)
        wrapped = (
            f"printf '%s\\n' \"$$\" > {shlex.quote(pgid_file)}; exec bash -c {shlex.quote(command)}"
        )
        exec_id, sock = await asyncio.to_thread(self._create_and_attach_exec, wrapped, pty_enabled)
        raw_sock = getattr(sock, "_sock", sock)
        with contextlib.suppress(OSError, AttributeError):
            raw_sock.setblocking(False)

        session = _DockerSession(
            session_id=session_id,
            command=command,
            pty=pty_enabled,
            spool=spool,
            started_at=time.monotonic(),
            max_duration_seconds=max(1, duration),
            exec_id=exec_id,
            sock=sock,
            raw_sock=raw_sock,
        )

        def _mark_eof() -> None:
            session.eof = True

        reader = threading.Thread(
            target=_drain_docker_socket,
            kwargs={
                "raw_sock": raw_sock,
                "pty_enabled": pty_enabled,
                "spool": spool,
                "stop_event": stop_event,
                "on_eof": _mark_eof,
            },
            daemon=True,
            name=f"af-dkr-{session_id}",
        )
        session.reader_thread = reader
        self._sessions[session_id] = session
        reader.start()
        schedule_session_timeout(session, self._cancel_for_timeout)

        await wait_for_session_activity(
            session,
            wait_seconds=initial_wait_seconds,
            baseline_bytes=0,
            refresh_fn=self._refresh_session,
        )
        return build_snapshot(
            session,
            offset=None,
            max_bytes=max_bytes or self._limits.default_page_bytes,
            limits=self._limits,
        )

    def _create_and_attach_exec(self, wrapped_command: str, pty_enabled: bool) -> tuple[str, Any]:
        created = self._client.api.exec_create(
            self._container_id,
            ["bash", "-c", wrapped_command],
            stdin=True,
            stdout=True,
            stderr=True,
            tty=pty_enabled,
            user=self._user,
        )
        exec_id = str(created["Id"])
        sock = self._client.api.exec_start(exec_id, socket=True, tty=pty_enabled)
        return exec_id, sock

    def _inspect_exit_code(self, session: _DockerSession) -> int | None:
        try:
            info = self._client.api.exec_inspect(session.exec_id)
        except APIError:
            return session.exit_code
        if info.get("Running", False):
            return None
        raw_exit = info.get("ExitCode")
        return int(raw_exit) if raw_exit is not None else 0

    def _refresh_session(self, session: _DockerSession) -> bool:
        now = time.monotonic()
        if session.exit_code is None and (session.eof or now - session.last_inspect_at >= 0.05):
            session.last_inspect_at = now
            exit_code = self._inspect_exit_code(session)
            if exit_code is not None:
                session.exit_code = exit_code
        if session.exit_code is not None and not session.eof and session.reader_thread is not None:
            session.reader_thread.join(timeout=0.1)
        if session.exit_code is not None and (
            session.eof or session.reader_thread is None or not session.reader_thread.is_alive()
        ):
            self._close_socket(session)
            return True
        return False

    def _close_socket(self, session: _DockerSession) -> None:
        if session.closed:
            return
        session.closed = True
        cancel_watchdog_task(session)
        stop_event = self._stop_events.get(session.session_id)
        if stop_event is not None:
            stop_event.set()
        if session.sock is not None:
            sock = session.sock
            session.sock = None
            session.raw_sock = None
            with contextlib.suppress(OSError, ValueError, AttributeError):
                sock.close()

    def _require(self, session_id: str) -> _DockerSession:
        if session_id not in self._sessions:
            msg = f"Unknown terminal session: {session_id}"
            raise KeyError(msg)
        return self._sessions[session_id]

    async def poll(
        self,
        session_id: str,
        *,
        wait_seconds: float = 0.0,
        offset: int | None = None,
        max_bytes: int | None = None,
    ) -> SessionSnapshot:
        """Poll a container session for new output (or from an explicit offset)."""
        session = self._require(session_id)
        baseline = session.spool.total_bytes
        await wait_for_session_activity(
            session,
            wait_seconds=wait_seconds,
            baseline_bytes=baseline,
            refresh_fn=self._refresh_session,
        )
        return build_snapshot(
            session,
            offset=offset,
            max_bytes=max_bytes or self._limits.default_page_bytes,
            limits=self._limits,
        )

    async def write(
        self,
        session_id: str,
        data: str,
        *,
        wait_seconds: float = 0.5,
        max_bytes: int | None = None,
    ) -> SessionSnapshot:
        """Write input bytes into a running container session and wait briefly."""
        session = self._require(session_id)
        self._refresh_session(session)
        if session.closed or session.exit_code is not None or session.raw_sock is None:
            msg = f"Terminal session '{session_id}' has already exited (exit={session.exit_code})"
            raise RuntimeError(msg)

        baseline = session.spool.total_bytes
        payload = data.encode("utf-8")
        await asyncio.to_thread(session.raw_sock.sendall, payload)

        await wait_for_session_activity(
            session,
            wait_seconds=wait_seconds,
            baseline_bytes=baseline,
            refresh_fn=self._refresh_session,
        )
        return build_snapshot(
            session,
            offset=None,
            max_bytes=max_bytes or self._limits.default_page_bytes,
            limits=self._limits,
        )

    def _kill_remote_pgid(self, session_id: str) -> None:
        pgid_file = shlex.quote(_pgid_file(session_id))
        kill_script = (
            f"p=$(cat {pgid_file} 2>/dev/null || true); "
            'if [ -n "$p" ]; then '
            'kill -TERM -"$p" 2>/dev/null || kill -TERM "$p" 2>/dev/null || true; '
            "sleep 0.1; "
            'kill -KILL -"$p" 2>/dev/null || kill -KILL "$p" 2>/dev/null || true; '
            "fi; "
            f"rm -f {pgid_file}"
        )
        with contextlib.suppress(APIError, Exception):
            created = self._client.api.exec_create(
                self._container_id,
                ["bash", "-c", kill_script],
                user=self._user,
            )
            self._client.api.exec_start(created["Id"], demux=True)

    async def _cancel_for_timeout(self, session_id: str, timed_out: bool) -> SessionSnapshot:
        return await self.cancel(session_id, timed_out=timed_out)

    async def cancel(
        self,
        session_id: str,
        *,
        timed_out: bool = False,
    ) -> SessionSnapshot:
        """Kill the session process group inside the container and close its socket."""
        session = self._require(session_id)
        if timed_out:
            session.timed_out = True
        cancel_watchdog_task(session)
        if session.exit_code is None:
            await asyncio.to_thread(self._kill_remote_pgid, session_id)
        stop_event = self._stop_events.get(session_id)
        if stop_event is not None:
            stop_event.set()
        self._close_socket(session)
        if session.reader_thread is not None and session.reader_thread.is_alive():
            await asyncio.to_thread(session.reader_thread.join, 1.0)
        if session.exit_code is None:
            exit_code = await asyncio.to_thread(self._inspect_exit_code, session)
            session.exit_code = exit_code if exit_code is not None else -9
        return build_snapshot(
            session,
            offset=None,
            max_bytes=self._limits.default_page_bytes,
            limits=self._limits,
        )

    async def list_sessions(self) -> list[SessionSnapshot]:
        """List snapshots of all tracked container sessions."""
        snapshots: list[SessionSnapshot] = []
        for session in self._sessions.values():
            self._refresh_session(session)
            snapshots.append(
                build_snapshot(
                    session,
                    offset=session.spool.cursor_offset,
                    max_bytes=self._limits.default_page_bytes,
                    limits=self._limits,
                )
            )
        return snapshots

    async def close_all(self) -> None:
        """Terminate and reap all tracked container sessions."""
        for session_id in list(self._sessions):
            session = self._sessions[session_id]
            if not session.closed or session.exit_code is None:
                await self.cancel(session_id)
