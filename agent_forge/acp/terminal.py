"""Workspace-contained POSIX PTY terminal session for the ACP companion API."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import fcntl
import os
import pty
import select
import signal
import struct
import subprocess
import termios
from pathlib import Path

_ALLOWED_SHELLS: tuple[str, ...] = ("/bin/bash", "/bin/sh", "/usr/bin/zsh", "/bin/zsh")
_SAFE_ENV_KEYS: tuple[str, ...] = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL")
_SECRET_SUFFIXES: tuple[str, ...] = ("_API_KEY", "_SECRET", "_TOKEN", "_PASSWORD")


def select_allowed_shell() -> str:
    """Return the first present shell from the fixed executable allowlist."""
    for candidate in _ALLOWED_SHELLS:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    msg = "No allowed interactive shell binary found on host"
    raise FileNotFoundError(msg)


def build_sanitized_terminal_env(cwd: Path) -> dict[str, str]:
    """Construct a minimal environment stripping API keys and ACP transport tokens."""
    env: dict[str, str] = {"TERM": "xterm-256color", "PWD": str(cwd)}
    for key in _SAFE_ENV_KEYS:
        val = os.environ.get(key)
        if val and not any(key.endswith(suffix) for suffix in _SECRET_SUFFIXES):
            env[key] = val
    return env


class WorkspaceTerminalSession:
    """Interactive POSIX PTY shell confined to the verified workspace directory."""

    def __init__(self, cwd: str | Path) -> None:
        resolved = Path(cwd).resolve()
        if not resolved.is_dir():
            msg = f"Terminal cwd must be an existing directory: {cwd!r}"
            raise ValueError(msg)
        self._cwd = resolved
        self._master_fd: int | None = None
        self._proc: subprocess.Popen[bytes] | None = None
        self._closed = False

    def start(self, *, cols: int = 80, rows: int = 24) -> None:
        """Spawn an allowlisted interactive shell inside a newly allocated PTY."""
        if self._proc is not None:
            return
        shell_bin = select_allowed_shell()
        master_fd, slave_fd = pty.openpty()
        self._apply_winsize(master_fd, cols=cols, rows=rows)
        try:
            self._proc = subprocess.Popen(  # noqa: S603 — shell_bin is from hardcoded allowlist
                [shell_bin, "-i"],
                cwd=str(self._cwd),
                env=build_sanitized_terminal_env(self._cwd),
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,
                close_fds=True,
            )
        except Exception:
            os.close(master_fd)
            os.close(slave_fd)
            raise
        os.close(slave_fd)
        self._master_fd = master_fd

    @staticmethod
    def _apply_winsize(fd: int, *, cols: int, rows: int) -> None:
        clamped_cols = max(1, min(cols, 500))
        clamped_rows = max(1, min(rows, 200))
        winsize = struct.pack("HHHH", clamped_rows, clamped_cols, 0, 0)
        with contextlib.suppress(OSError):
            fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)

    def resize(self, *, cols: int, rows: int) -> None:
        """Update PTY window dimensions."""
        if self._master_fd is not None and not self._closed:
            self._apply_winsize(self._master_fd, cols=cols, rows=rows)

    def write(self, data: bytes) -> None:
        """Write user input bytes into the PTY master fd."""
        if self._master_fd is None or self._closed or not data:
            return
        os.write(self._master_fd, data)

    def _read_blocking_once(self, max_bytes: int, timeout: float) -> bytes | None:
        fd = self._master_fd
        if fd is None or self._closed:
            return None
        try:
            readable, _, _ = select.select([fd], [], [], timeout)
        except (OSError, ValueError):
            return None
        if not readable:
            return b""
        try:
            chunk = os.read(fd, max_bytes)
        except OSError as exc:
            if exc.errno in (errno.EIO, errno.EBADF):
                return None
            raise
        return chunk if chunk else None

    async def read_chunk(self, *, max_bytes: int = 4096, timeout: float = 0.1) -> bytes | None:
        """Read next available PTY output chunk, or ``None`` on EOF/close."""
        return await asyncio.to_thread(self._read_blocking_once, max_bytes, timeout)

    def poll_exit_code(self) -> int | None:
        """Return child process exit code if terminated, otherwise ``None``."""
        if self._proc is None:
            return None
        return self._proc.poll()

    def close(self) -> None:
        """Terminate child shell process and release PTY descriptors idempotently."""
        if self._closed:
            return
        self._closed = True
        if self._proc is not None and self._proc.poll() is None:
            with contextlib.suppress(OSError):
                os.killpg(self._proc.pid, signal.SIGTERM)
            try:
                self._proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError):
                    os.killpg(self._proc.pid, signal.SIGKILL)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    self._proc.wait(timeout=1.0)
        if self._master_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._master_fd)
            self._master_fd = None
