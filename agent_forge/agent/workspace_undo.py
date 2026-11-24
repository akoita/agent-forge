"""Workspace snapshot, safe turn undo, and diff rendering for interactive chat sessions."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

_SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    }
)
_MAX_SNAPSHOT_FILE_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class TurnWorkspaceSnapshot:
    """Reversible pre/post workspace snapshot captured around one chat turn."""

    turn_index: int
    pre_contents: dict[str, bytes | None]
    post_hashes: dict[str, str | None]


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _run_git(
    repo_path: Path,
    args: list[str],
    *,
    timeout: int = 15,
) -> subprocess.CompletedProcess[str] | None:
    git_exe = shutil.which("git")
    if git_exe is None:
        return None
    return subprocess.run(  # noqa: S603
        [git_exe, *args],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _is_git_repo(repo_path: Path) -> bool:
    res = _run_git(repo_path, ["rev-parse", "--is-inside-work-tree"])
    return res is not None and res.returncode == 0 and res.stdout.strip() == "true"


def _iter_git_workspace_files(repo_path: Path) -> list[Path] | None:
    res = _run_git(repo_path, ["ls-files", "-z", "--cached", "--others", "--exclude-standard"])
    if res is None or res.returncode != 0:
        return None
    paths: list[Path] = []
    for raw_rel in res.stdout.split("\0"):
        if not raw_rel:
            continue
        candidate = repo_path / raw_rel
        if candidate.is_file() and not candidate.is_symlink():
            paths.append(candidate)
    return paths


def _iter_walk_workspace_files(repo_path: Path) -> list[Path]:
    paths: list[Path] = []
    for entry in repo_path.rglob("*"):
        if any(part in _SKIP_DIRS for part in entry.relative_to(repo_path).parts):
            continue
        if entry.is_file() and not entry.is_symlink():
            paths.append(entry)
    return paths


def capture_workspace_state(
    repo_path: Path,
    *,
    max_file_bytes: int = _MAX_SNAPSHOT_FILE_BYTES,
) -> dict[str, bytes]:
    """Capture workspace relative-path byte contents while ignoring VCS and build caches."""
    if not repo_path.is_dir():
        return {}
    files = _iter_git_workspace_files(repo_path) if _is_git_repo(repo_path) else None
    if files is None:
        files = _iter_walk_workspace_files(repo_path)

    state: dict[str, bytes] = {}
    for file_path in files:
        try:
            if file_path.stat().st_size > max_file_bytes:
                continue
            rel = file_path.relative_to(repo_path).as_posix()
            state[rel] = file_path.read_bytes()
        except OSError:
            continue
    return state


def build_turn_snapshot(
    turn_index: int,
    before: dict[str, bytes],
    after: dict[str, bytes],
) -> TurnWorkspaceSnapshot | None:
    """Compute a minimal reversible snapshot of files changed, created, or deleted."""
    pre_contents: dict[str, bytes | None] = {}
    post_hashes: dict[str, str | None] = {}
    for rel in sorted(before.keys() | after.keys()):
        old_bytes = before.get(rel)
        new_bytes = after.get(rel)
        if old_bytes == new_bytes:
            continue
        pre_contents[rel] = old_bytes
        post_hashes[rel] = _sha256_bytes(new_bytes) if new_bytes is not None else None
    if not pre_contents:
        return None
    return TurnWorkspaceSnapshot(
        turn_index=turn_index,
        pre_contents=pre_contents,
        post_hashes=post_hashes,
    )


def restore_turn_snapshot(
    repo_path: Path,
    snapshot: TurnWorkspaceSnapshot,
    *,
    force: bool = False,
) -> tuple[list[str], list[str], list[str]]:
    """Restore *snapshot* onto *repo_path*, aborting on external edits unless *force*."""
    if not force:
        conflicts: list[str] = []
        for rel, expected_hash in snapshot.post_hashes.items():
            target = repo_path / rel
            current_hash = _sha256_bytes(target.read_bytes()) if target.is_file() else None
            if current_hash != expected_hash:
                conflicts.append(rel)
        if conflicts:
            return [], [], sorted(conflicts)

    restored: list[str] = []
    removed: list[str] = []
    for rel, prev_bytes in snapshot.pre_contents.items():
        target = repo_path / rel
        if prev_bytes is None:
            target.unlink(missing_ok=True)
            removed.append(rel)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(prev_bytes)
            restored.append(rel)
    return sorted(restored), sorted(removed), []


def render_workspace_diff(
    repo_path: Path,
    *,
    stat_only: bool = False,
    max_chars: int = 12_000,
) -> dict[str, Any]:
    """Inspect uncommitted Git changes in *repo_path* for interactive ``/diff`` output."""
    if not _is_git_repo(repo_path):
        return {
            "is_git_repo": False,
            "clean": True,
            "status_lines": [],
            "diff_text": "",
            "truncated": False,
            "output": "Workspace is not a Git repository; /diff requires Git.",
        }

    status_res = _run_git(repo_path, ["status", "--short"])
    status_lines = [
        line for line in (status_res.stdout.splitlines() if status_res else []) if line.strip()
    ]
    has_head = (
        head_res := _run_git(repo_path, ["rev-parse", "--verify", "HEAD"])
    ) is not None and head_res.returncode == 0
    diff_args = ["diff", "--stat"] if stat_only else ["diff"]
    if has_head:
        diff_args.append("HEAD")
    diff_res = _run_git(repo_path, diff_args)
    raw_diff = diff_res.stdout.strip() if diff_res else ""

    truncated = False
    if len(raw_diff) > max_chars:
        raw_diff = raw_diff[:max_chars].rstrip() + "\n... (diff truncated)"
        truncated = True

    if not status_lines and not raw_diff:
        return {
            "is_git_repo": True,
            "clean": True,
            "status_lines": [],
            "diff_text": "",
            "truncated": False,
            "output": "Workspace is clean (no uncommitted changes).",
        }

    sections: list[str] = []
    if status_lines:
        sections.append("Status:\n" + "\n".join(f"  {line}" for line in status_lines))
    if raw_diff:
        sections.append(raw_diff)
    return {
        "is_git_repo": True,
        "clean": False,
        "status_lines": status_lines,
        "diff_text": raw_diff,
        "truncated": truncated,
        "output": "\n\n".join(sections),
    }
