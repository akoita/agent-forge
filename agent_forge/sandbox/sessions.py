"""Resumable PTY/pipe terminal session management and bounded output pagination."""

from __future__ import annotations

import asyncio
import contextlib
import os
import pty
import select
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


@dataclass(frozen=True)
class SessionLimits:
    """Resource limits governing terminal sessions within a single sandbox."""

    max_concurrent_sessions: int = 4
    max_retained_bytes: int = 2 * 1024 * 1024  # 2 MB rolling window
    default_max_duration_seconds: int = 300
    default_page_bytes: int = 32 * 1024
    max_page_bytes: int = 64 * 1024


@dataclass(frozen=True)
class OutputPage:
    """Bounded slice of terminal output paired with explicit truncation metadata."""

    text: str
    offset: int
    next_offset: int
    returned_bytes: int
    total_bytes: int
    bytes_elided_before: int
    bytes_remaining_after: int
    truncated: bool
    window_start_offset: int

    def to_metadata(self) -> dict[str, Any]:
        """Serialize byte counters and pagination pointers for ToolResult.metadata."""
        return {
            "truncated": self.truncated,
            "offset": self.offset,
            "next_offset": self.next_offset,
            "returned_bytes": self.returned_bytes,
            "total_bytes": self.total_bytes,
            "bytes_elided_before": self.bytes_elided_before,
            "bytes_remaining_after": self.bytes_remaining_after,
            "window_start_offset": self.window_start_offset,
        }

    def format_notice(self, session_id: str) -> str:
        """Render an explicit metadata notice when bytes were elided or remain unread."""
        if not self.truncated:
            return ""
        parts = [
            f"returned {self.returned_bytes} bytes "
            f"({self.offset}..{self.next_offset} of {self.total_bytes} total)"
        ]
        if self.bytes_elided_before > 0:
            parts.append(f"{self.bytes_elided_before} bytes elided before retained window")
        if self.bytes_remaining_after > 0:
            parts.append(
                f"{self.bytes_remaining_after} bytes remaining; "
                f"call shell_poll(session_id={session_id!r}, offset={self.next_offset}) "
                "to read more"
            )
        return "[Output truncated: " + "; ".join(parts) + "]"


@dataclass(frozen=True)
class SessionSnapshot:
    """Point-in-time status and paginated output for a terminal session."""

    session_id: str
    command: str
    running: bool
    exit_code: int | None
    pty: bool
    page: OutputPage
    elapsed_seconds: float
    timed_out: bool = False


class BoundedOutputSpool:
    """Thread-safe rolling byte buffer with monotonic offsets for large outputs."""

    def __init__(self, max_retained_bytes: int = 2 * 1024 * 1024) -> None:
        if max_retained_bytes < 1:
            msg = "max_retained_bytes must be >= 1"
            raise ValueError(msg)
        self._max_retained_bytes = max_retained_bytes
        self._lock = threading.Lock()
        self._buffer = bytearray()
        self._window_start_offset = 0
        self._total_bytes_written = 0
        self._read_cursor = 0

    @property
    def retained_bytes(self) -> int:
        """Current number of bytes retained in memory."""
        with self._lock:
            return len(self._buffer)

    @property
    def total_bytes(self) -> int:
        """Total bytes observed since session start."""
        with self._lock:
            return self._total_bytes_written

    @property
    def cursor_offset(self) -> int:
        """Current internal cursor offset used when polling without an explicit offset."""
        with self._lock:
            return self._read_cursor

    def append(self, chunk: bytes) -> None:
        """Append raw bytes, evicting oldest bytes beyond the retained window."""
        if not chunk:
            return
        with self._lock:
            self._total_bytes_written += len(chunk)
            if len(chunk) >= self._max_retained_bytes:
                tail = chunk[-self._max_retained_bytes :]
                self._buffer = bytearray(tail)
                self._window_start_offset = self._total_bytes_written - len(self._buffer)
                return
            self._buffer.extend(chunk)
            excess = len(self._buffer) - self._max_retained_bytes
            if excess > 0:
                del self._buffer[:excess]
                self._window_start_offset += excess

    def read(self, offset: int | None = None, max_bytes: int = 32 * 1024) -> OutputPage:
        """Read up to ``max_bytes`` from ``offset`` (or current cursor) with metadata."""
        limit = max(1, max_bytes)
        with self._lock:
            advance_cursor = offset is None
            requested = self._read_cursor if offset is None else max(0, offset)
            bytes_elided_before = max(0, self._window_start_offset - requested)
            start = max(requested, self._window_start_offset)
            start = min(start, self._total_bytes_written)

            rel_start = start - self._window_start_offset
            rel_end = min(len(self._buffer), rel_start + limit)
            raw_slice = bytes(self._buffer[rel_start:rel_end])
            returned = len(raw_slice)
            next_offset = start + returned
            if advance_cursor:
                self._read_cursor = next_offset
            bytes_remaining = self._total_bytes_written - next_offset
            return OutputPage(
                text=raw_slice.decode("utf-8", errors="replace"),
                offset=start,
                next_offset=next_offset,
                returned_bytes=returned,
                total_bytes=self._total_bytes_written,
                bytes_elided_before=bytes_elided_before,
                bytes_remaining_after=bytes_remaining,
                truncated=bytes_elided_before > 0 or bytes_remaining > 0,
                window_start_offset=self._window_start_offset,
            )


@dataclass
class ManagedSessionBase:
    """Common per-session tracking fields shared by local and Docker backends."""

    session_id: str
    command: str
    pty: bool
    spool: BoundedOutputSpool
    started_at: float
    max_duration_seconds: int
    exit_code: int | None = None
    timed_out: bool = False
    eof: bool = False
    closed: bool = False
    timeout_task: asyncio.Task[None] | None = None


def check_session_capacity(
    sessions: dict[str, Any],
    limits: SessionLimits,
    refresh_fn: Callable[[Any], bool],
) -> None:
    """Enforce ``limits.max_concurrent_sessions`` across currently running sessions."""
    running_count = 0
    for rec in sessions.values():
        refresh_fn(rec)
        if not rec.closed and rec.exit_code is None:
            running_count += 1
    if running_count >= limits.max_concurrent_sessions:
        msg = (
            f"Maximum concurrent terminal sessions reached ({limits.max_concurrent_sessions}). "
            "Cancel or wait for an existing session before starting another."
        )
        raise RuntimeError(msg)


def schedule_session_timeout(
    session: ManagedSessionBase,
    cancel_fn: Callable[[str, bool], Awaitable[SessionSnapshot]],
) -> None:
    """Schedule automatic cancellation when a session exceeds ``max_duration_seconds``."""

    async def _watchdog() -> None:
        await asyncio.sleep(session.max_duration_seconds)
        if not session.closed and session.exit_code is None:
            await cancel_fn(session.session_id, True)

    session.timeout_task = asyncio.create_task(_watchdog())


def cancel_watchdog_task(session: ManagedSessionBase) -> None:
    """Cancel and detach the per-session timeout watchdog if active."""
    task = session.timeout_task
    session.timeout_task = None
    if task is not None and not task.done():
        current = asyncio.current_task()
        if current is not task:
            task.cancel()


async def wait_for_session_activity(
    session: ManagedSessionBase,
    *,
    wait_seconds: float,
    baseline_bytes: int,
    refresh_fn: Callable[[Any], bool],
) -> None:
    """Wait up to ``wait_seconds`` until output arrives or the session exits."""
    if wait_seconds <= 0:
        refresh_fn(session)
        return
    deadline = time.monotonic() + wait_seconds
    saw_output_at: float | None = None
    while True:
        drained = refresh_fn(session)
        if drained:
            return
        if session.spool.total_bytes > baseline_bytes and saw_output_at is None:
            saw_output_at = time.monotonic() + 0.05
        now = time.monotonic()
        if saw_output_at is not None and now >= saw_output_at:
            return
        if now >= deadline:
            return
        await asyncio.sleep(min(0.02, max(0.001, deadline - now)))


def build_snapshot(
    session: ManagedSessionBase,
    *,
    offset: int | None,
    max_bytes: int,
    limits: SessionLimits,
) -> SessionSnapshot:
    """Construct a paginated :class:`SessionSnapshot` from a session record."""
    bounded_bytes = min(max(1, max_bytes), limits.max_page_bytes)
    page = session.spool.read(offset=offset, max_bytes=bounded_bytes)
    running = (not session.closed) and session.exit_code is None
    return SessionSnapshot(
        session_id=session.session_id,
        command=session.command,
        running=running,
        exit_code=session.exit_code,
        pty=session.pty,
        page=page,
        elapsed_seconds=round(time.monotonic() - session.started_at, 3),
        timed_out=session.timed_out,
    )


@dataclass
class _LocalSession(ManagedSessionBase):
    process: subprocess.Popen[bytes] | None = None
    io_fd: int | None = None
    reader_thread: threading.Thread | None = None


def _drain_fd_into_spool(
    fd: int,
    spool: BoundedOutputSpool,
    stop_event: threading.Event,
    on_eof: Callable[[], None],
) -> None:
    """Continuously read PTY/pipe output into ``spool`` until EOF or close."""
    try:
        while not stop_event.is_set():
            try:
                ready, _, _ = select.select([fd], [], [], 0.05)
            except (OSError, ValueError):
                break
            if not ready:
                continue
            try:
                chunk = os.read(fd, 8192)
            except OSError:
                break
            if not chunk:
                break
            spool.append(chunk)
    finally:
        on_eof()


def _terminate_process_group(proc: subprocess.Popen[bytes]) -> int:
    """Terminate a process and all descendants in its session/process group."""
    if proc.poll() is not None:
        return proc.returncode
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
    deadline = time.monotonic() + 0.3
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    if proc.poll() is None:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=2.0)
    return proc.returncode if proc.returncode is not None else -signal.SIGKILL


class LocalPtySessionManager:
    """Manage local PTY and pipe subprocess sessions for Linux sandboxes."""

    def __init__(self, limits: SessionLimits | None = None) -> None:
        self._limits = limits or SessionLimits()
        self._sessions: dict[str, _LocalSession] = {}
        self._stop_events: dict[str, threading.Event] = {}

    async def start(
        self,
        argv: list[str],
        *,
        command_label: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        pty_enabled: bool = True,
        max_duration_seconds: int | None = None,
        initial_wait_seconds: float = 0.5,
        max_bytes: int | None = None,
    ) -> SessionSnapshot:
        """Spawn a new PTY or pipe session and return initial output."""
        check_session_capacity(self._sessions, self._limits, self._refresh_session)
        session_id = f"sess-{uuid.uuid4().hex[:8]}"
        duration = max_duration_seconds or self._limits.default_max_duration_seconds
        spool = BoundedOutputSpool(self._limits.max_retained_bytes)
        stop_event = threading.Event()
        self._stop_events[session_id] = stop_event

        session = _LocalSession(
            session_id=session_id,
            command=command_label,
            pty=pty_enabled,
            spool=spool,
            started_at=time.monotonic(),
            max_duration_seconds=max(1, duration),
        )
        self._spawn_subprocess(session, argv, cwd=cwd, env=env, stop_event=stop_event)
        self._sessions[session_id] = session
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

    def _spawn_subprocess(
        self,
        session: _LocalSession,
        argv: list[str],
        *,
        cwd: str | None,
        env: dict[str, str] | None,
        stop_event: threading.Event,
    ) -> None:
        proc_env = {} if env is None else env
        if session.pty:
            master_fd, slave_fd = pty.openpty()
            try:
                proc = subprocess.Popen(  # noqa: S603
                    argv,
                    cwd=cwd,
                    env=proc_env,
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    start_new_session=True,
                    close_fds=True,
                )
            except BaseException:
                os.close(master_fd)
                os.close(slave_fd)
                raise
            os.close(slave_fd)
            session.process = proc
            session.io_fd = master_fd
            read_fd = master_fd
        else:
            proc = subprocess.Popen(  # noqa: S603
                argv,
                cwd=cwd,
                env=proc_env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
            assert proc.stdout is not None  # noqa: S101
            session.process = proc
            read_fd = proc.stdout.fileno()

        def _mark_eof() -> None:
            session.eof = True

        reader = threading.Thread(
            target=_drain_fd_into_spool,
            args=(read_fd, session.spool, stop_event, _mark_eof),
            daemon=True,
            name=f"af-tty-{session.session_id}",
        )
        session.reader_thread = reader
        reader.start()

    def _refresh_session(self, session: _LocalSession) -> bool:
        proc = session.process
        if proc is None:
            return True
        if session.exit_code is None:
            rc = proc.poll()
            if rc is not None:
                session.exit_code = rc
        if session.exit_code is not None and not session.eof and session.reader_thread is not None:
            session.reader_thread.join(timeout=0.1)
        if session.exit_code is not None and (
            session.eof or session.reader_thread is None or not session.reader_thread.is_alive()
        ):
            self._close_handles(session)
            return True
        return False

    def _close_handles(self, session: _LocalSession) -> None:
        if session.closed:
            return
        session.closed = True
        cancel_watchdog_task(session)
        stop_event = self._stop_events.get(session.session_id)
        if stop_event is not None:
            stop_event.set()
        if session.io_fd is not None:
            fd = session.io_fd
            session.io_fd = None
            with contextlib.suppress(OSError):
                os.close(fd)
        proc = session.process
        if proc is not None and not session.pty:
            for stream in (proc.stdin, proc.stdout):
                if stream is not None:
                    with contextlib.suppress(OSError, ValueError):
                        stream.close()

    def _require(self, session_id: str) -> _LocalSession:
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
        """Poll a session for new output (or from an explicit byte offset)."""
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
        """Write input bytes to a running session and wait briefly for output."""
        session = self._require(session_id)
        self._refresh_session(session)
        if session.closed or session.exit_code is not None:
            msg = f"Terminal session '{session_id}' has already exited (exit={session.exit_code})"
            raise RuntimeError(msg)

        baseline = session.spool.total_bytes
        payload = data.encode("utf-8")
        if session.pty:
            assert session.io_fd is not None  # noqa: S101
            os.write(session.io_fd, payload)
        else:
            assert session.process is not None  # noqa: S101
            assert session.process.stdin is not None  # noqa: S101
            session.process.stdin.write(payload)
            session.process.stdin.flush()

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

    async def _cancel_for_timeout(self, session_id: str, timed_out: bool) -> SessionSnapshot:
        return await self.cancel(session_id, timed_out=timed_out)

    async def cancel(
        self,
        session_id: str,
        *,
        timed_out: bool = False,
    ) -> SessionSnapshot:
        """Terminate a session's process group, close FDs, and return final output."""
        session = self._require(session_id)
        if timed_out:
            session.timed_out = True
        cancel_watchdog_task(session)
        proc = session.process
        if proc is not None and session.exit_code is None:
            session.exit_code = await asyncio.to_thread(_terminate_process_group, proc)
        stop_event = self._stop_events.get(session_id)
        if stop_event is not None:
            stop_event.set()
        self._close_handles(session)
        if session.reader_thread is not None and session.reader_thread.is_alive():
            await asyncio.to_thread(session.reader_thread.join, 1.0)
        return build_snapshot(
            session,
            offset=None,
            max_bytes=self._limits.default_page_bytes,
            limits=self._limits,
        )

    async def list_sessions(self) -> list[SessionSnapshot]:
        """List snapshots of all tracked sessions."""
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
        """Cancel and reap every tracked session when the sandbox stops."""
        for session_id in list(self._sessions):
            session = self._sessions[session_id]
            if not session.closed or session.exit_code is None:
                await self.cancel(session_id)
