"""Git worktree lifecycle and merge-time integration for parallel agent runs."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from agent_forge.config import USER_CONFIG_DIR
from agent_forge.observability import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from typing import TypeVar

    _T = TypeVar("_T")

logger = get_logger("worktree")

DEFAULT_BRANCH_PREFIX = "agent-forge/run"
_LOCKS_GUARD = threading.Lock()
_REPO_LOCKS: dict[Path, threading.Lock] = {}


class WorktreeError(RuntimeError):
    """Raised when a Git worktree operation fails."""


class WorktreeState(Enum):
    """Lifecycle states of a managed run worktree."""

    ACTIVE = "active"
    COMMITTED = "committed"
    REMOVED = "removed"
    INTEGRATED = "integrated"
    CONFLICT = "conflict"


@dataclass
class WorktreeLease:
    """Tracking metadata for a single run's isolated Git worktree."""

    run_id: str
    repo_root: Path
    worktree_path: Path
    branch: str
    base_ref: str
    base_revision: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    state: WorktreeState = WorktreeState.ACTIVE
    has_changes: bool = False
    commit_sha: str | None = None
    patch: str = ""


@dataclass
class IntegrationResult:
    """Outcome of integrating one isolated run branch into a target branch."""

    run_id: str
    branch: str
    target_branch: str
    strategy: Literal["merge", "rebase"]
    status: Literal["merged", "no_changes", "conflict", "error"]
    merged_revision: str | None = None
    conflicted_files: list[str] = field(default_factory=list)
    detail: str = ""


@dataclass
class WorktreeInfo:
    """Parsed metadata from ``git worktree list --porcelain``."""

    path: Path
    head: str
    branch: str | None = None
    bare: bool = False
    prunable: bool = False
    managed: bool = False
    run_id: str | None = None


def _get_repo_lock(repo_root: Path) -> threading.Lock:
    resolved = repo_root.resolve()
    with _LOCKS_GUARD:
        lock = _REPO_LOCKS.get(resolved)
        if lock is None:
            lock = threading.Lock()
            _REPO_LOCKS[resolved] = lock
        return lock


def _git_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    if extra:
        env.update(extra)
    return env


def _git_raw(
    cwd: Path,
    *args: str,
    env_extra: dict[str, str] | None = None,
    timeout: int = 30,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603
        ["git", "-c", f"core.hooksPath={os.devnull}", *args],  # noqa: S607
        cwd=cwd,
        env=_git_env(env_extra),
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _git(
    cwd: Path,
    *args: str,
    env_extra: dict[str, str] | None = None,
    timeout: int = 30,
) -> bytes:
    proc = _git_raw(cwd, *args, env_extra=env_extra, timeout=timeout)
    if proc.returncode != 0:
        detail = proc.stderr.decode(errors="replace").strip()
        if not detail:
            detail = proc.stdout.decode(errors="replace").strip()
        cmd_name = args[0] if args else "git"
        msg = f"Git {cmd_name} failed: {detail}"
        raise WorktreeError(msg)
    return proc.stdout


def is_git_repo(path: str | Path) -> bool:
    """Return ``True`` if *path* points inside a Git working tree."""
    candidate = Path(path)
    if not candidate.is_dir():
        return False
    proc = _git_raw(candidate, "rev-parse", "--is-inside-work-tree", timeout=5)
    return proc.returncode == 0 and proc.stdout.decode(errors="replace").strip() == "true"


def _sanitize_slug(raw: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", raw.strip()).strip("-.")
    return cleaned[:48] or "run"


def _default_worktrees_root(repo_root: Path) -> Path:
    digest = hashlib.sha256(str(repo_root).encode("utf-8")).hexdigest()[:10]
    return USER_CONFIG_DIR / "worktrees" / f"{repo_root.name}-{digest}"


def _parse_worktree_porcelain(
    raw: str,
    *,
    worktrees_root: Path,
    branch_prefix: str,
    run_id_by_path: dict[Path, str],
) -> list[WorktreeInfo]:
    items: list[WorktreeInfo] = []
    prefix_ref = f"{branch_prefix.rstrip('/')}/"
    for block in raw.strip().split("\n\n"):
        if not block.strip():
            continue
        info = _parse_single_worktree_block(
            block,
            worktrees_root=worktrees_root,
            prefix_ref=prefix_ref,
            run_id_by_path=run_id_by_path,
        )
        if info is not None:
            items.append(info)
    return items


def _parse_single_worktree_block(
    block: str,
    *,
    worktrees_root: Path,
    prefix_ref: str,
    run_id_by_path: dict[Path, str],
) -> WorktreeInfo | None:
    path_val: Path | None = None
    head = ""
    branch: str | None = None
    bare = False
    prunable = False
    for line in block.splitlines():
        if line.startswith("worktree "):
            path_val = Path(line.split(" ", 1)[1].strip()).resolve()
        elif line.startswith("HEAD "):
            head = line.split(" ", 1)[1].strip()
        elif line.startswith("branch "):
            ref = line.split(" ", 1)[1].strip()
            branch = ref.removeprefix("refs/heads/")
        elif line == "bare":
            bare = True
        elif line.startswith("prunable"):
            prunable = True
    if path_val is None:
        return None
    is_under_root = path_val.is_relative_to(worktrees_root)
    has_managed_branch = branch is not None and branch.startswith(prefix_ref)
    managed = is_under_root or has_managed_branch
    run_id = run_id_by_path.get(path_val)
    if run_id is None and branch and branch.startswith(prefix_ref):
        run_id = branch[len(prefix_ref) :]
    return WorktreeInfo(
        path=path_val,
        head=head,
        branch=branch,
        bare=bare,
        prunable=prunable,
        managed=managed,
        run_id=run_id,
    )


class WorktreeManager:
    """Create, track, clean up, and integrate isolated Git worktrees per run."""

    def __init__(
        self,
        repo_path: str | Path,
        *,
        worktrees_root: str | Path | None = None,
        branch_prefix: str = DEFAULT_BRANCH_PREFIX,
        base_ref: str | None = None,
    ) -> None:
        candidate = Path(repo_path).resolve()
        if not candidate.is_dir():
            msg = f"Repository directory does not exist: {repo_path}"
            raise WorktreeError(msg)
        top_raw = _git(candidate, "rev-parse", "--show-toplevel").decode(errors="replace").strip()
        self.repo_root = Path(top_raw).resolve()
        self.branch_prefix = branch_prefix.strip().rstrip("/") or DEFAULT_BRANCH_PREFIX
        self.default_base_ref = base_ref or "HEAD"
        self.worktrees_root = (
            Path(worktrees_root).expanduser().resolve()
            if worktrees_root is not None and str(worktrees_root).strip()
            else _default_worktrees_root(self.repo_root).resolve()
        )
        self._lock = _get_repo_lock(self.repo_root)
        self._leases: dict[str, WorktreeLease] = {}

    @property
    def leases(self) -> dict[str, WorktreeLease]:
        """Return a snapshot mapping of all tracked leases keyed by run ID."""
        return dict(self._leases)

    @property
    def active_leases(self) -> dict[str, WorktreeLease]:
        """Return currently active worktree leases."""
        return {
            rid: lease for rid, lease in self._leases.items() if lease.state == WorktreeState.ACTIVE
        }

    def get_lease(self, run_id: str) -> WorktreeLease | None:
        """Return the tracked lease for *run_id*, if any."""
        return self._leases.get(run_id)

    async def _run_locked(self, fn: Callable[..., _T], *args: object) -> _T:
        def _call() -> _T:
            with self._lock:
                return fn(*args)

        return await asyncio.to_thread(_call)

    async def create(self, run_id: str, *, base_ref: str | None = None) -> WorktreeLease:
        """Provision a fresh branch and isolated Git worktree for *run_id*."""
        return await self._run_locked(self._create_sync, run_id, base_ref)

    def _branch_exists_sync(self, branch: str) -> bool:
        proc = _git_raw(
            self.repo_root,
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}",
        )
        return proc.returncode == 0

    def _allocate_target_sync(self, run_id: str) -> tuple[str, Path]:
        slug = _sanitize_slug(run_id)
        branch = f"{self.branch_prefix}/{slug}"
        worktree_path = self.worktrees_root / slug
        if worktree_path.exists() or self._branch_exists_sync(branch):
            suffix = uuid.uuid4().hex[:6]
            branch = f"{branch}-{suffix}"
            worktree_path = self.worktrees_root / f"{slug}-{suffix}"
        return branch, worktree_path

    def _create_sync(self, run_id: str, base_ref: str | None) -> WorktreeLease:
        existing = self._leases.get(run_id)
        if existing is not None and existing.state == WorktreeState.ACTIVE:
            msg = f"Worktree lease already active for run {run_id}"
            raise WorktreeError(msg)
        resolved_ref = base_ref or self.default_base_ref
        base_revision = (
            _git(self.repo_root, "rev-parse", "--verify", f"{resolved_ref}^{{commit}}")
            .decode(errors="replace")
            .strip()
        )
        self.worktrees_root.mkdir(parents=True, exist_ok=True)
        branch, worktree_path = self._allocate_target_sync(run_id)
        _git(
            self.repo_root,
            "worktree",
            "add",
            "-b",
            branch,
            str(worktree_path),
            base_revision,
        )
        lease = WorktreeLease(
            run_id=run_id,
            repo_root=self.repo_root,
            worktree_path=worktree_path.resolve(),
            branch=branch,
            base_ref=resolved_ref,
            base_revision=base_revision,
        )
        self._leases[run_id] = lease
        logger.info(
            "worktree_created",
            run_id=run_id,
            branch=branch,
            worktree_path=str(lease.worktree_path),
            base_revision=base_revision,
        )
        return lease

    def _working_tree_dirty_sync(self, worktree_path: Path) -> bool:
        if not worktree_path.exists():
            return False
        proc = _git_raw(worktree_path, "status", "--porcelain")
        return bool(proc.stdout.strip())

    def _branch_commit_count_sync(self, base_revision: str, branch: str) -> int:
        if not self._branch_exists_sync(branch):
            return 0
        out = _git(self.repo_root, "rev-list", "--count", f"{base_revision}..{branch}")
        return int(out.decode(errors="replace").strip() or "0")

    def _has_changes_sync(self, lease: WorktreeLease) -> bool:
        return self._working_tree_dirty_sync(lease.worktree_path) or (
            self._branch_commit_count_sync(lease.base_revision, lease.branch) > 0
        )

    async def has_changes(self, lease: WorktreeLease) -> bool:
        """Return whether the isolated worktree or branch differs from its base revision."""
        return await self._run_locked(self._has_changes_sync, lease)

    def _capture_diff_sync(self, lease: WorktreeLease) -> str:
        if not lease.worktree_path.exists():
            if lease.patch:
                return lease.patch
            if self._branch_exists_sync(lease.branch):
                raw = _git(
                    self.repo_root,
                    "diff",
                    "--binary",
                    "--no-ext-diff",
                    "--no-textconv",
                    f"{lease.base_revision}..{lease.branch}",
                    "--",
                )
                return raw.decode(errors="surrogateescape")
            return ""
        with tempfile.TemporaryDirectory(prefix="af-wt-idx-") as tmp_dir:
            index_file = str(Path(tmp_dir) / "index")
            env_extra = {"GIT_INDEX_FILE": index_file}
            _git(lease.worktree_path, "read-tree", "HEAD", env_extra=env_extra)
            _git(
                lease.worktree_path,
                "--work-tree",
                str(lease.worktree_path),
                "add",
                "-A",
                "--",
                ".",
                env_extra=env_extra,
            )
            diff_bytes = _git(
                lease.worktree_path,
                "diff",
                "--cached",
                "--binary",
                "--no-ext-diff",
                "--no-textconv",
                lease.base_revision,
                "--",
                env_extra=env_extra,
            )
        return diff_bytes.decode(errors="surrogateescape")

    async def capture_diff(self, lease: WorktreeLease) -> str:
        """Return a unified patch of all committed and uncommitted changes since base."""
        return await self._run_locked(self._capture_diff_sync, lease)

    def _prune_sync(self) -> None:
        _git_raw(self.repo_root, "worktree", "prune")

    def _remove_worktree_sync(self, worktree_path: Path) -> None:
        if worktree_path.exists():
            proc = _git_raw(self.repo_root, "worktree", "remove", "--force", str(worktree_path))
            if proc.returncode != 0 and worktree_path.exists():
                shutil.rmtree(worktree_path, ignore_errors=True)
        self._prune_sync()

    def _delete_branch_sync(self, branch: str) -> None:
        if self._branch_exists_sync(branch):
            _git_raw(self.repo_root, "branch", "-D", branch)

    def _abort_sync(self, run_id: str) -> WorktreeLease | None:
        with self._lock:
            lease = self._leases.get(run_id)
            if lease is None:
                return None
            self._remove_worktree_sync(lease.worktree_path)
            self._delete_branch_sync(lease.branch)
            lease.has_changes = False
            lease.state = WorktreeState.REMOVED
            logger.info("worktree_aborted", run_id=run_id, branch=lease.branch)
            return lease

    def _commit_changes_sync(self, lease: WorktreeLease, message: str) -> str:
        if self._working_tree_dirty_sync(lease.worktree_path):
            _git(lease.worktree_path, "add", "-A", "--", ".")
            _git(
                lease.worktree_path,
                "-c",
                "user.name=Agent Forge",
                "-c",
                "user.email=agent-forge@local",
                "commit",
                "--no-verify",
                "-m",
                message,
            )
        return (
            _git(self.repo_root, "rev-parse", "--verify", lease.branch)
            .decode(errors="replace")
            .strip()
        )

    def _finalize_sync(
        self,
        run_id: str,
        commit_message: str | None,
        keep_changed_worktree: bool,
        auto_remove_unchanged: bool,
        abort: bool,
    ) -> WorktreeLease:
        lease = self._leases.get(run_id)
        if lease is None:
            msg = f"Unknown worktree run ID: {run_id}"
            raise WorktreeError(msg)
        if lease.state in (WorktreeState.REMOVED, WorktreeState.INTEGRATED):
            return lease
        if abort:
            self._remove_worktree_sync(lease.worktree_path)
            self._delete_branch_sync(lease.branch)
            lease.has_changes = False
            lease.state = WorktreeState.REMOVED
            return lease
        if not self._has_changes_sync(lease):
            lease.has_changes = False
            if auto_remove_unchanged:
                self._remove_worktree_sync(lease.worktree_path)
                self._delete_branch_sync(lease.branch)
                lease.state = WorktreeState.REMOVED
                logger.info("worktree_removed_unchanged", run_id=run_id, branch=lease.branch)
            return lease
        lease.patch = self._capture_diff_sync(lease)
        msg_text = commit_message or f"agent-forge: apply changes for run {run_id}"
        lease.commit_sha = self._commit_changes_sync(lease, msg_text)
        lease.has_changes = True
        lease.state = WorktreeState.COMMITTED
        if not keep_changed_worktree:
            self._remove_worktree_sync(lease.worktree_path)
        logger.info(
            "worktree_committed",
            run_id=run_id,
            branch=lease.branch,
            commit_sha=lease.commit_sha,
        )
        return lease

    async def finalize(
        self,
        run_id: str,
        *,
        commit_message: str | None = None,
        keep_changed_worktree: bool = False,
        auto_remove_unchanged: bool = True,
        abort: bool = False,
    ) -> WorktreeLease:
        """Commit run changes or remove unchanged/aborted worktrees."""
        return await self._run_locked(
            self._finalize_sync,
            run_id,
            commit_message,
            keep_changed_worktree,
            auto_remove_unchanged,
            abort,
        )

    def _remove_sync(self, run_id: str, delete_branch: bool) -> None:
        lease = self._leases.get(run_id)
        if lease is None:
            return
        self._remove_worktree_sync(lease.worktree_path)
        if delete_branch:
            self._delete_branch_sync(lease.branch)
        lease.state = WorktreeState.REMOVED

    async def remove(self, run_id: str, *, delete_branch: bool = True) -> None:
        """Force-remove a tracked worktree and optionally delete its branch."""
        await self._run_locked(self._remove_sync, run_id, delete_branch)

    @contextlib.asynccontextmanager
    async def lease(
        self,
        run_id: str,
        *,
        base_ref: str | None = None,
        commit_message: str | None = None,
        keep_changed_worktree: bool = False,
        auto_remove_unchanged: bool = True,
    ) -> AsyncIterator[WorktreeLease]:
        """Acquire an isolated worktree lease and guarantee cleanup on abort."""
        worktree_lease = await self.create(run_id, base_ref=base_ref)
        try:
            yield worktree_lease
        except BaseException:
            # Run synchronous cleanup so CancelledError cannot interrupt removal.
            self._abort_sync(run_id)
            raise
        await self.finalize(
            run_id,
            commit_message=commit_message,
            keep_changed_worktree=keep_changed_worktree,
            auto_remove_unchanged=auto_remove_unchanged,
        )

    def _list_worktrees_sync(self) -> list[WorktreeInfo]:
        raw = _git(self.repo_root, "worktree", "list", "--porcelain").decode(errors="replace")
        run_id_by_path = {lease.worktree_path.resolve(): rid for rid, lease in self._leases.items()}
        return _parse_worktree_porcelain(
            raw,
            worktrees_root=self.worktrees_root,
            branch_prefix=self.branch_prefix,
            run_id_by_path=run_id_by_path,
        )

    async def list_worktrees(self) -> list[WorktreeInfo]:
        """List all registered Git worktrees for this repository."""
        return await self._run_locked(self._list_worktrees_sync)

    def _conflicted_files_sync(self, directory: Path) -> list[str]:
        proc = _git_raw(directory, "diff", "--name-only", "--diff-filter=U")
        lines = proc.stdout.decode(errors="replace").splitlines()
        return sorted({line.strip() for line in lines if line.strip()})

    def _find_checkout_for_branch_sync(self, branch: str) -> Path | None:
        for info in self._list_worktrees_sync():
            if info.branch == branch and info.path != self.repo_root and info.path.exists():
                return info.path
        return None

    def _resolve_target_branch_sync(self, target_branch: str | None) -> str | None:
        if target_branch:
            return target_branch
        proc = _git_raw(self.repo_root, "symbolic-ref", "--short", "HEAD")
        if proc.returncode != 0:
            return None
        return proc.stdout.decode(errors="replace").strip() or None

    def _mark_lease_integrated_sync(
        self,
        branch: str,
        state: WorktreeState,
        *,
        run_id: str | None = None,
    ) -> None:
        for rid, lease in self._leases.items():
            if lease.branch == branch or (run_id is not None and rid == run_id):
                lease.state = state

    def _merge_branch_sync(
        self,
        branch: str,
        target: str,
        resolved_run_id: str,
        delete_branch_on_success: bool,
    ) -> IntegrationResult:
        current = self._resolve_target_branch_sync(None)
        if current != target:
            _git(self.repo_root, "checkout", target)
        proc = _git_raw(
            self.repo_root,
            "-c",
            "user.name=Agent Forge",
            "-c",
            "user.email=agent-forge@local",
            "merge",
            "--no-ff",
            "-m",
            f"Integrate agent run {resolved_run_id}",
            branch,
        )
        if proc.returncode != 0:
            conflicts = self._conflicted_files_sync(self.repo_root)
            detail = (
                proc.stderr.decode(errors="replace").strip()
                or proc.stdout.decode(errors="replace").strip()
            )
            _git_raw(self.repo_root, "merge", "--abort")
            self._mark_lease_integrated_sync(branch, WorktreeState.CONFLICT, run_id=resolved_run_id)
            return IntegrationResult(
                run_id=resolved_run_id,
                branch=branch,
                target_branch=target,
                strategy="merge",
                status="conflict",
                conflicted_files=conflicts,
                detail=detail,
            )
        merged_rev = _git(self.repo_root, "rev-parse", "HEAD").decode(errors="replace").strip()
        checkout = self._find_checkout_for_branch_sync(branch)
        if checkout is not None:
            self._remove_worktree_sync(checkout)
        if delete_branch_on_success:
            self._delete_branch_sync(branch)
        self._mark_lease_integrated_sync(branch, WorktreeState.INTEGRATED, run_id=resolved_run_id)
        return IntegrationResult(
            run_id=resolved_run_id,
            branch=branch,
            target_branch=target,
            strategy="merge",
            status="merged",
            merged_revision=merged_rev,
        )

    def _rebase_branch_sync(
        self,
        branch: str,
        target: str,
        resolved_run_id: str,
        delete_branch_on_success: bool,
    ) -> IntegrationResult:
        current = self._resolve_target_branch_sync(None)
        if current != target:
            _git(self.repo_root, "checkout", target)
        existing_checkout = self._find_checkout_for_branch_sync(branch)
        temp_checkout = existing_checkout is None
        rebase_dir = (
            existing_checkout
            if existing_checkout is not None
            else self.worktrees_root / f".rebase-{uuid.uuid4().hex[:8]}"
        )
        if temp_checkout:
            self.worktrees_root.mkdir(parents=True, exist_ok=True)
            _git(self.repo_root, "worktree", "add", str(rebase_dir), branch)
        proc = _git_raw(
            rebase_dir,
            "-c",
            "user.name=Agent Forge",
            "-c",
            "user.email=agent-forge@local",
            "rebase",
            target,
        )
        if proc.returncode != 0:
            conflicts = self._conflicted_files_sync(rebase_dir)
            detail = (
                proc.stderr.decode(errors="replace").strip()
                or proc.stdout.decode(errors="replace").strip()
            )
            _git_raw(rebase_dir, "rebase", "--abort")
            if temp_checkout:
                self._remove_worktree_sync(rebase_dir)
            self._mark_lease_integrated_sync(branch, WorktreeState.CONFLICT, run_id=resolved_run_id)
            return IntegrationResult(
                run_id=resolved_run_id,
                branch=branch,
                target_branch=target,
                strategy="rebase",
                status="conflict",
                conflicted_files=conflicts,
                detail=detail,
            )
        self._remove_worktree_sync(rebase_dir)
        _git(self.repo_root, "merge", "--ff-only", branch)
        merged_rev = _git(self.repo_root, "rev-parse", "HEAD").decode(errors="replace").strip()
        if delete_branch_on_success:
            self._delete_branch_sync(branch)
        self._mark_lease_integrated_sync(branch, WorktreeState.INTEGRATED, run_id=resolved_run_id)
        return IntegrationResult(
            run_id=resolved_run_id,
            branch=branch,
            target_branch=target,
            strategy="rebase",
            status="merged",
            merged_revision=merged_rev,
        )

    def _integrate_branch_sync(
        self,
        branch: str,
        run_id: str | None,
        target_branch: str | None,
        strategy: Literal["merge", "rebase"],
        delete_branch_on_success: bool,
    ) -> IntegrationResult:
        resolved_run_id = run_id or branch
        resolved_target = self._resolve_target_branch_sync(target_branch)
        if resolved_target is None:
            return IntegrationResult(
                run_id=resolved_run_id,
                branch=branch,
                target_branch="",
                strategy=strategy,
                status="error",
                detail="Repository HEAD is detached; provide an explicit target branch.",
            )
        if not self._branch_exists_sync(branch):
            return IntegrationResult(
                run_id=resolved_run_id,
                branch=branch,
                target_branch=resolved_target,
                strategy=strategy,
                status="error",
                detail=f"Branch does not exist: {branch}",
            )
        if self._working_tree_dirty_sync(self.repo_root):
            return IntegrationResult(
                run_id=resolved_run_id,
                branch=branch,
                target_branch=resolved_target,
                strategy=strategy,
                status="error",
                detail="Primary repository has uncommitted changes.",
            )
        ahead_raw = _git(self.repo_root, "rev-list", "--count", f"{resolved_target}..{branch}")
        if int(ahead_raw.decode(errors="replace").strip() or "0") == 0:
            checkout = self._find_checkout_for_branch_sync(branch)
            if checkout is not None:
                self._remove_worktree_sync(checkout)
            if delete_branch_on_success:
                self._delete_branch_sync(branch)
            self._mark_lease_integrated_sync(
                branch,
                WorktreeState.REMOVED,
                run_id=resolved_run_id,
            )
            return IntegrationResult(
                run_id=resolved_run_id,
                branch=branch,
                target_branch=resolved_target,
                strategy=strategy,
                status="no_changes",
            )
        if strategy == "rebase":
            return self._rebase_branch_sync(
                branch,
                resolved_target,
                resolved_run_id,
                delete_branch_on_success,
            )
        return self._merge_branch_sync(
            branch,
            resolved_target,
            resolved_run_id,
            delete_branch_on_success,
        )

    async def integrate_branch(
        self,
        branch: str,
        *,
        run_id: str | None = None,
        target_branch: str | None = None,
        strategy: Literal["merge", "rebase"] = "merge",
        delete_branch_on_success: bool = True,
    ) -> IntegrationResult:
        """Integrate an isolated run *branch* into *target_branch* via merge or rebase."""
        return await self._run_locked(
            self._integrate_branch_sync,
            branch,
            run_id,
            target_branch,
            strategy,
            delete_branch_on_success,
        )

    async def integrate(
        self,
        run_id: str,
        *,
        target_branch: str | None = None,
        strategy: Literal["merge", "rebase"] = "merge",
        delete_branch_on_success: bool = True,
    ) -> IntegrationResult:
        """Integrate the committed branch for *run_id* into *target_branch*."""
        lease = self._leases.get(run_id)
        if lease is None:
            return IntegrationResult(
                run_id=run_id,
                branch="",
                target_branch=target_branch or "",
                strategy=strategy,
                status="error",
                detail=f"Unknown worktree run ID: {run_id}",
            )
        if lease.state == WorktreeState.REMOVED and not lease.has_changes:
            target = target_branch or lease.base_ref
            return IntegrationResult(
                run_id=run_id,
                branch=lease.branch,
                target_branch=target,
                strategy=strategy,
                status="no_changes",
            )
        return await self.integrate_branch(
            lease.branch,
            run_id=run_id,
            target_branch=target_branch,
            strategy=strategy,
            delete_branch_on_success=delete_branch_on_success,
        )

    async def integrate_all(
        self,
        run_ids: list[str] | None = None,
        *,
        target_branch: str | None = None,
        strategy: Literal["merge", "rebase"] = "merge",
        delete_branch_on_success: bool = True,
        stop_on_conflict: bool = False,
    ) -> list[IntegrationResult]:
        """Integrate multiple committed run branches in order at merge time."""
        candidates = (
            list(run_ids)
            if run_ids is not None
            else [
                rid for rid, lease in self._leases.items() if lease.state == WorktreeState.COMMITTED
            ]
        )
        results: list[IntegrationResult] = []
        for rid in candidates:
            result = await self.integrate(
                rid,
                target_branch=target_branch,
                strategy=strategy,
                delete_branch_on_success=delete_branch_on_success,
            )
            results.append(result)
            if stop_on_conflict and result.status == "conflict":
                break
        return results

    def _cleanup_orphans_sync(self, remove_branches: bool) -> list[str]:
        cleaned: list[str] = []
        self._prune_sync()
        active_paths = {lease.worktree_path.resolve() for lease in self.active_leases.values()}
        for info in self._list_worktrees_sync():
            if info.managed and info.path != self.repo_root and info.path not in active_paths:
                self._remove_worktree_sync(info.path)
                cleaned.append(str(info.path))
        if self.worktrees_root.is_dir():
            for child in sorted(self.worktrees_root.iterdir()):
                resolved = child.resolve()
                if resolved not in active_paths and child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                    if str(resolved) not in cleaned:
                        cleaned.append(str(resolved))
        self._prune_sync()
        if remove_branches:
            preserved = {
                lease.branch
                for lease in self._leases.values()
                if lease.state in (WorktreeState.ACTIVE, WorktreeState.COMMITTED)
            }
            ref_proc = _git_raw(
                self.repo_root,
                "for-each-ref",
                "--format=%(refname:short)",
                f"refs/heads/{self.branch_prefix}/",
            )
            for branch in ref_proc.stdout.decode(errors="replace").splitlines():
                name = branch.strip()
                if name and name not in preserved:
                    self._delete_branch_sync(name)
                    cleaned.append(f"branch:{name}")
        return sorted(cleaned)

    async def cleanup_orphans(self, *, remove_branches: bool = True) -> list[str]:
        """Prune stale worktrees, leftover directories, and orphaned run branches."""
        return await self._run_locked(self._cleanup_orphans_sync, remove_branches)
