"""Workspace-contained file, watch, and Git operations for the ACP companion API."""

from __future__ import annotations

import asyncio
import os
import posixpath
import tempfile
from pathlib import Path
from typing import Any

_IGNORED_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    }
)


class PayloadTooLargeError(ValueError):
    """Raised when a workspace file read/write exceeds the configured byte cap."""


class WorkspaceBridge:
    """Strictly contained host-workspace file, watch, and Git operations."""

    def __init__(
        self,
        root_dir: str | Path,
        *,
        max_file_bytes: int = 1_048_576,
        max_tree_entries: int = 2_000,
        max_diff_chars: int = 256_000,
    ) -> None:
        resolved = Path(root_dir).resolve()
        if not resolved.is_dir():
            msg = f"Workspace root must be an existing directory: {root_dir!r}"
            raise ValueError(msg)
        self._root = resolved
        self._root_prefix = str(resolved) + os.sep
        self._max_file_bytes = max_file_bytes
        self._max_tree_entries = max_tree_entries
        self._max_diff_chars = max_diff_chars

    @property
    def root(self) -> Path:
        """Resolved canonical workspace root directory."""
        return self._root

    def _is_within_root(self, candidate: Path) -> bool:
        return candidate == self._root or str(candidate).startswith(self._root_prefix)

    def _validate_relative_parts(self, rel_path: str, *, allow_root: bool) -> list[str]:
        if "\x00" in rel_path or "\\" in rel_path:
            msg = f"Invalid path characters in {rel_path!r}"
            raise ValueError(msg)
        cleaned = rel_path.strip()
        if not cleaned or cleaned == ".":
            if allow_root:
                return []
            msg = "Workspace path must not be empty"
            raise ValueError(msg)
        if cleaned.startswith("/"):
            msg = f"Absolute workspace paths are forbidden: {rel_path!r}"
            raise ValueError(msg)
        normalized = posixpath.normpath(cleaned)
        parts = [p for p in normalized.split("/") if p and p != "."]
        if any(p == ".." for p in parts) or (parts and parts[0] == ".git"):
            msg = f"Path escapes allowed workspace boundary: {rel_path!r}"
            raise ValueError(msg)
        return parts

    def _check_ancestor_symlinks(self, parts: list[str]) -> None:
        current = self._root
        for part in parts:
            current = current / part
            if current.exists() or current.is_symlink():
                if not self._is_within_root(current.resolve()):
                    msg = f"Symlink escapes workspace root: {part!r}"
                    raise ValueError(msg)
            else:
                break

    def resolve_safe_path(self, rel_path: str, *, allow_root: bool = False) -> Path:
        """Resolve *rel_path* inside :attr:`root`, blocking traversal and symlink escapes."""
        parts = self._validate_relative_parts(rel_path, allow_root=allow_root)
        if not parts:
            return self._root
        self._check_ancestor_symlinks(parts)
        candidate = (self._root / Path(*parts)).resolve()
        if not self._is_within_root(candidate) or candidate == self._root:
            msg = f"Resolved path escapes workspace boundary: {rel_path!r}"
            raise ValueError(msg)
        return candidate

    def list_tree(self, rel_path: str = "", *, max_depth: int = 6) -> list[dict[str, Any]]:
        """List workspace files and directories cleanly under *rel_path*."""
        start_dir = self.resolve_safe_path(rel_path, allow_root=True)
        if not start_dir.is_dir():
            msg = f"Not a directory: {rel_path!r}"
            raise ValueError(msg)
        bounded_depth = max(1, min(max_depth, 16))
        entries: list[dict[str, Any]] = []
        self._walk_tree(start_dir, depth=0, max_depth=bounded_depth, entries=entries)
        return entries

    def _walk_tree(
        self,
        directory: Path,
        *,
        depth: int,
        max_depth: int,
        entries: list[dict[str, Any]],
    ) -> None:
        if depth >= max_depth or len(entries) >= self._max_tree_entries:
            return
        try:
            children = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError:
            return
        for child in children:
            if len(entries) >= self._max_tree_entries:
                return
            if child.name in _IGNORED_DIRS:
                continue
            resolved_child = child.resolve()
            if not self._is_within_root(resolved_child):
                continue
            self._append_tree_entry(
                child, resolved_child, depth=depth, max_depth=max_depth, entries=entries
            )

    def _append_tree_entry(
        self,
        child: Path,
        resolved_child: Path,
        *,
        depth: int,
        max_depth: int,
        entries: list[dict[str, Any]],
    ) -> None:
        try:
            stat = resolved_child.stat()
        except OSError:
            return
        is_dir = resolved_child.is_dir()
        rel = resolved_child.relative_to(self._root).as_posix()
        entries.append(
            {
                "path": rel,
                "name": child.name,
                "kind": "directory" if is_dir else "file",
                "size": 0 if is_dir else stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        )
        if is_dir:
            self._walk_tree(resolved_child, depth=depth + 1, max_depth=max_depth, entries=entries)

    def read_file(self, rel_path: str) -> dict[str, Any]:
        """Read a UTF-8 text file safely inside the workspace root."""
        target = self.resolve_safe_path(rel_path)
        if not target.is_file():
            msg = f"File not found: {rel_path!r}"
            raise FileNotFoundError(msg)
        stat = target.stat()
        if stat.st_size > self._max_file_bytes:
            msg = f"File exceeds max_file_bytes ({self._max_file_bytes}): {rel_path!r}"
            raise PayloadTooLargeError(msg)
        raw = target.read_bytes()
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            msg = f"File is not valid UTF-8 text: {rel_path!r}"
            raise ValueError(msg) from exc
        rel = target.relative_to(self._root).as_posix()
        return {
            "path": rel,
            "content": content,
            "size": len(raw),
            "mtime_ns": stat.st_mtime_ns,
            "encoding": "utf-8",
        }

    def write_file(self, rel_path: str, content: str) -> dict[str, Any]:
        """Atomically write UTF-8 *content* to *rel_path* inside the workspace root."""
        raw = content.encode("utf-8")
        if len(raw) > self._max_file_bytes:
            msg = f"Payload exceeds max_file_bytes ({self._max_file_bytes})"
            raise PayloadTooLargeError(msg)
        target = self.resolve_safe_path(rel_path)
        if target.exists() and target.is_dir():
            msg = f"Target is a directory: {rel_path!r}"
            raise ValueError(msg)
        target.parent.mkdir(parents=True, exist_ok=True)
        resolved_parent = target.parent.resolve()
        if not self._is_within_root(resolved_parent):
            msg = f"Parent directory escapes workspace boundary: {rel_path!r}"
            raise ValueError(msg)
        fd, tmp_name = tempfile.mkstemp(prefix=".af-tmp-", dir=str(resolved_parent))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
                handle.flush()
            os.replace(tmp_name, target)
        except Exception:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise
        stat = target.stat()
        rel = target.relative_to(self._root).as_posix()
        return {"path": rel, "size": len(raw), "mtime_ns": stat.st_mtime_ns}

    def snapshot_files(self) -> dict[str, tuple[int, int]]:
        """Capture ``{rel_path: (mtime_ns, size)}`` across non-ignored workspace files."""
        snap: dict[str, tuple[int, int]] = {}
        self._walk_snapshot(self._root, snap)
        return snap

    def _walk_snapshot(self, directory: Path, snap: dict[str, tuple[int, int]]) -> None:
        if len(snap) >= self._max_tree_entries:
            return
        try:
            items = sorted(directory.iterdir())
        except OSError:
            return
        for item in items:
            if len(snap) >= self._max_tree_entries or item.name in _IGNORED_DIRS:
                continue
            resolved = item.resolve()
            if not self._is_within_root(resolved):
                continue
            if resolved.is_dir():
                self._walk_snapshot(resolved, snap)
            elif resolved.is_file():
                try:
                    st = resolved.stat()
                    snap[resolved.relative_to(self._root).as_posix()] = (st.st_mtime_ns, st.st_size)
                except OSError:
                    continue

    @staticmethod
    def diff_snapshots(
        prev: dict[str, tuple[int, int]],
        curr: dict[str, tuple[int, int]],
    ) -> dict[str, list[str]]:
        """Compute added, modified, and deleted relative paths between two snapshots."""
        added = sorted(path for path in curr if path not in prev)
        deleted = sorted(path for path in prev if path not in curr)
        modified = sorted(path for path in curr if path in prev and curr[path] != prev[path])
        return {"added": added, "modified": modified, "deleted": deleted}

    async def _run_git(self, *args: str) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(self._root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return (
            proc.returncode if proc.returncode is not None else 1,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )

    async def git_status(self) -> dict[str, Any]:
        """Return Git branch and per-file porcelain status inside the workspace."""
        code, stdout, stderr = await self._run_git("status", "--porcelain=v1", "-b")
        if code != 0:
            return {"branch": "", "entries": [], "error": stderr.strip()}
        lines = stdout.splitlines()
        branch = ""
        entries: list[dict[str, str]] = []
        for line in lines:
            if line.startswith("## "):
                branch = line[3:].split("...", 1)[0].strip()
                continue
            if len(line) >= 4:
                entries.append(
                    {
                        "index": line[0],
                        "worktree": line[1],
                        "path": line[3:].strip(),
                    }
                )
        return {"branch": branch, "entries": entries}

    async def git_diff(
        self, rel_path: str | None = None, *, staged: bool = False
    ) -> dict[str, Any]:
        """Return bounded Git unified diff optionally scoped to a single safe path."""
        args = ["diff"]
        if staged:
            args.append("--cached")
        if rel_path:
            safe_target = self.resolve_safe_path(rel_path)
            rel = safe_target.relative_to(self._root).as_posix()
            args.extend(["--", rel])
        code, stdout, stderr = await self._run_git(*args)
        if code != 0:
            return {"diff": "", "truncated": False, "error": stderr.strip()}
        truncated = len(stdout) > self._max_diff_chars
        text = stdout[: self._max_diff_chars] if truncated else stdout
        return {"diff": text, "truncated": truncated}
