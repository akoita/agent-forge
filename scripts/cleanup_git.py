#!/usr/bin/env python3
"""Audit and clean up merged branches and clean unused git worktrees.

Usage::

    python3 scripts/cleanup_git.py                 # dry-run report
    python3 scripts/cleanup_git.py --apply         # remove clean merged worktrees/branches
    python3 scripts/cleanup_git.py --hook-post-tool-use < event.json
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

PROTECTED_BRANCHES: frozenset[str] = frozenset({"main", "master"})
MERGE_CMD_RE = re.compile(r"\b(?:gh\s+pr\s+(?:merge|close)|git\s+merge)\b")
_GH_PR_MERGE_RE = re.compile(r"\bgh\s+pr\s+merge\b")
_DELETE_BRANCH_FLAG_RE = re.compile(r"(?:^|\s)(?:--delete-branch|-d)(?=\s|$)")


@dataclass(frozen=True)
class WorktreeInfo:
    """Metadata describing a git worktree entry."""

    path: Path
    head: str
    branch: str | None
    is_main: bool
    is_detached: bool
    is_bare: bool


@dataclass(frozen=True)
class BranchInfo:
    """Metadata describing a local git branch."""

    name: str
    upstream: str
    upstream_gone: bool
    is_head: bool


@dataclass(frozen=True)
class CleanupPlan:
    """Computed cleanup candidates and safety exclusions."""

    worktrees_to_remove: list[WorktreeInfo]
    dirty_worktrees: list[WorktreeInfo]
    branches_to_delete: list[tuple[str, bool]]


def _run_cmd(
    args: list[str],
    *,
    cwd: Path,
    timeout: int = 15,
) -> subprocess.CompletedProcess[str] | None:
    """Run an external command without shell interpolation, returning None if missing."""
    exe = shutil.which(args[0])
    if exe is None:
        return None
    try:
        return subprocess.run(  # noqa: S603
            [exe, *args[1:]],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _parse_worktree_block(block: str, *, is_main: bool) -> WorktreeInfo | None:
    """Parse one stanza from ``git worktree list --porcelain``."""
    wt_path: Path | None = None
    head = ""
    branch: str | None = None
    is_detached = False
    is_bare = False

    for raw_line in block.splitlines():
        line = raw_line.strip()
        if line.startswith("worktree "):
            wt_path = Path(line.removeprefix("worktree "))
        elif line.startswith("HEAD "):
            head = line.removeprefix("HEAD ")
        elif line.startswith("branch "):
            branch = line.removeprefix("branch ").removeprefix("refs/heads/")
        elif line == "detached":
            is_detached = True
        elif line == "bare":
            is_bare = True

    if wt_path is None:
        return None
    return WorktreeInfo(
        path=wt_path,
        head=head,
        branch=branch,
        is_main=is_main,
        is_detached=is_detached,
        is_bare=is_bare,
    )


def parse_worktrees(porcelain: str) -> list[WorktreeInfo]:
    """Parse ``git worktree list --porcelain`` output into WorktreeInfo items."""
    blocks = [b for b in re.split(r"\r?\n\r?\n", porcelain.strip()) if b.strip()]
    worktrees: list[WorktreeInfo] = []
    for idx, block in enumerate(blocks):
        info = _parse_worktree_block(block, is_main=(idx == 0))
        if info is not None:
            worktrees.append(info)
    return worktrees


def parse_local_branches(output: str) -> list[BranchInfo]:
    """Parse ``git for-each-ref`` tab-separated branch metadata."""
    branches: list[BranchInfo] = []
    for raw_line in output.splitlines():
        if not raw_line.strip():
            continue
        parts = raw_line.split("\t")
        name = parts[0].strip()
        upstream = parts[1].strip() if len(parts) > 1 else ""
        track = parts[2].strip() if len(parts) > 2 else ""
        head_marker = parts[3].strip() if len(parts) > 3 else ""
        branches.append(
            BranchInfo(
                name=name,
                upstream=upstream,
                upstream_gone="gone" in track,
                is_head=(head_marker == "*"),
            )
        )
    return branches


def is_worktree_clean(path: Path) -> bool:
    """Return True only when *path* exists and ``git status --porcelain`` is empty."""
    if not path.exists():
        return False
    proc = _run_cmd(["git", "-C", str(path), "status", "--porcelain"], cwd=path)
    if proc is None or proc.returncode != 0:
        return False
    return proc.stdout.strip() == ""


def is_ancestor(ref: str, target: str, cwd: Path) -> bool:
    """Return True if *ref* is an ancestor of *target*."""
    proc = _run_cmd(["git", "merge-base", "--is-ancestor", ref, target], cwd=cwd)
    return proc is not None and proc.returncode == 0


def fetch_gh_pr_branches(cwd: Path, state: str) -> set[str] | None:
    """Return PR head branch names for *state* via ``gh``, or None if unavailable."""
    proc = _run_cmd(
        ["gh", "pr", "list", "--state", state, "--limit", "200", "--json", "headRefName"],
        cwd=cwd,
        timeout=10,
    )
    if proc is None or proc.returncode != 0:
        return None
    try:
        items = json.loads(proc.stdout)
    except ValueError:
        return None
    if not isinstance(items, list):
        return None
    names: set[str] = set()
    for item in items:
        if isinstance(item, dict):
            head_ref = item.get("headRefName")
            if isinstance(head_ref, str) and head_ref:
                names.add(head_ref)
    return names


def _resolve_repo_root(cwd: Path) -> Path | None:
    """Resolve the top-level directory of the current git worktree."""
    proc = _run_cmd(["git", "rev-parse", "--show-toplevel"], cwd=cwd)
    if proc is None or proc.returncode != 0:
        return None
    root = proc.stdout.strip()
    return Path(root) if root else None


def _list_worktrees(repo_root: Path) -> list[WorktreeInfo]:
    proc = _run_cmd(["git", "worktree", "list", "--porcelain"], cwd=repo_root)
    if proc is None or proc.returncode != 0:
        return []
    return parse_worktrees(proc.stdout)


def _list_local_branches(repo_root: Path) -> list[BranchInfo]:
    fmt = "%(refname:short)%09%(upstream:short)%09%(upstream:track)%09%(HEAD)"
    proc = _run_cmd(["git", "for-each-ref", f"--format={fmt}", "refs/heads"], cwd=repo_root)
    if proc is None or proc.returncode != 0:
        return []
    return parse_local_branches(proc.stdout)


def _branch_merge_status(
    branch: BranchInfo,
    *,
    repo_root: Path,
    base_ref: str,
    merged_prs: set[str],
) -> tuple[bool, bool]:
    """Return ``(eligible, force_delete)`` for a local branch."""
    if is_ancestor(branch.name, base_ref, repo_root):
        return (True, False)
    if branch.upstream_gone or branch.name in merged_prs:
        return (True, True)
    return (False, False)


def _build_protected_branches(
    worktrees: list[WorktreeInfo],
    *,
    resolved_cwd: Path,
    open_prs: set[str],
) -> set[str]:
    """Collect branches that must never be removed."""
    protected = set(PROTECTED_BRANCHES) | open_prs
    for wt in worktrees:
        if wt.branch is None:
            continue
        if wt.is_main or resolved_cwd.is_relative_to(wt.path.resolve()):
            protected.add(wt.branch)
    return protected


def _is_worktree_stale(
    wt: WorktreeInfo,
    *,
    repo_root: Path,
    base_ref: str,
    protected: set[str],
    branch_map: dict[str, BranchInfo],
    merged_prs: set[str],
) -> bool:
    """Return True if a linked worktree points to merged/superseded history."""
    if wt.is_detached:
        return bool(wt.head) and is_ancestor(wt.head, base_ref, repo_root)
    if wt.branch is None or wt.branch in protected:
        return False
    branch_info = branch_map.get(wt.branch)
    if branch_info is None:
        return is_ancestor(wt.branch, base_ref, repo_root) or wt.branch in merged_prs
    eligible, _ = _branch_merge_status(
        branch_info,
        repo_root=repo_root,
        base_ref=base_ref,
        merged_prs=merged_prs,
    )
    return eligible


def find_cleanup_candidates(
    repo_root: Path,
    *,
    current_cwd: Path,
    base_ref: str = "origin/main",
    include_gh: bool = True,
) -> CleanupPlan:
    """Compute removable clean worktrees and merged/integrated local branches."""
    worktrees = _list_worktrees(repo_root)
    branches = _list_local_branches(repo_root)
    branch_map = {branch.name: branch for branch in branches}

    open_prs = (fetch_gh_pr_branches(repo_root, "open") or set()) if include_gh else set()
    merged_prs = (fetch_gh_pr_branches(repo_root, "merged") or set()) if include_gh else set()

    resolved_cwd = current_cwd.resolve()
    protected = _build_protected_branches(worktrees, resolved_cwd=resolved_cwd, open_prs=open_prs)

    worktrees_to_remove: list[WorktreeInfo] = []
    dirty_worktrees: list[WorktreeInfo] = []
    surviving_branches: set[str] = set()

    for wt in worktrees:
        if wt.is_main or wt.is_bare or resolved_cwd.is_relative_to(wt.path.resolve()):
            if wt.branch:
                surviving_branches.add(wt.branch)
            continue

        stale = _is_worktree_stale(
            wt,
            repo_root=repo_root,
            base_ref=base_ref,
            protected=protected,
            branch_map=branch_map,
            merged_prs=merged_prs,
        )
        if stale and is_worktree_clean(wt.path):
            worktrees_to_remove.append(wt)
        else:
            if stale:
                dirty_worktrees.append(wt)
            if wt.branch:
                surviving_branches.add(wt.branch)

    branches_to_delete: list[tuple[str, bool]] = []
    for branch in branches:
        if branch.name in protected or branch.name in surviving_branches:
            continue
        eligible, force = _branch_merge_status(
            branch,
            repo_root=repo_root,
            base_ref=base_ref,
            merged_prs=merged_prs,
        )
        if eligible:
            branches_to_delete.append((branch.name, force))

    return CleanupPlan(
        worktrees_to_remove=worktrees_to_remove,
        dirty_worktrees=dirty_worktrees,
        branches_to_delete=branches_to_delete,
    )


def apply_cleanup(
    plan: CleanupPlan,
    repo_root: Path,
    *,
    prune_remote: bool = True,
) -> list[str]:
    """Execute worktree removals, branch deletions, and remote-ref pruning."""
    actions: list[str] = []

    for wt in plan.worktrees_to_remove:
        proc = _run_cmd(["git", "worktree", "remove", str(wt.path)], cwd=repo_root)
        if proc is not None and proc.returncode == 0:
            actions.append(f"Removed worktree {wt.path}")
        else:
            err = proc.stderr.strip() if proc is not None else "git unavailable"
            actions.append(f"Failed removing worktree {wt.path}: {err}")

    if plan.worktrees_to_remove:
        _run_cmd(["git", "worktree", "prune"], cwd=repo_root)

    for branch_name, force in plan.branches_to_delete:
        flag = "-D" if force else "-d"
        proc = _run_cmd(["git", "branch", flag, branch_name], cwd=repo_root)
        if proc is not None and proc.returncode == 0:
            actions.append(f"Deleted branch {branch_name} ({flag})")
        else:
            err = proc.stderr.strip() if proc is not None else "git unavailable"
            actions.append(f"Failed deleting branch {branch_name}: {err}")

    if prune_remote:
        proc = _run_cmd(["git", "fetch", "--prune", "origin"], cwd=repo_root, timeout=30)
        if proc is not None and proc.returncode == 0:
            actions.append("Pruned stale remote-tracking refs (git fetch --prune origin)")
        else:
            err = proc.stderr.strip() if proc is not None else "git unavailable"
            actions.append(f"Skipped/failed remote prune: {err}")

    return actions


def _extract_hook_command(payload: object) -> str | None:
    """Extract the shell command string from a PostToolUse stdin payload."""
    if not isinstance(payload, dict):
        return None
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        for key in ("command", "cmd"):
            value = tool_input.get(key)
            if isinstance(value, str):
                return value
    return None


def _extract_hook_cwd(payload: object, fallback_cwd: Path) -> Path:
    """Return a valid working directory from hook payload if provided."""
    if isinstance(payload, dict):
        raw_cwd = payload.get("cwd")
        if isinstance(raw_cwd, str) and raw_cwd:
            candidate = Path(raw_cwd)
            if candidate.is_dir():
                return candidate
    return fallback_cwd


def handle_post_tool_use_hook(stdin_text: str, cwd: Path) -> str | None:
    """Build PostToolUse hook JSON after merge/close commands, or return None."""
    try:
        payload = json.loads(stdin_text)
    except ValueError:
        return None

    command = _extract_hook_command(payload)
    if command is None or MERGE_CMD_RE.search(command) is None:
        return None

    hook_cwd = _extract_hook_cwd(payload, cwd)
    notes: list[str] = []

    if _GH_PR_MERGE_RE.search(command) and not _DELETE_BRANCH_FLAG_RE.search(command):
        notes.append(
            "Merged PR command omitted --delete-branch; verify remote branch deletion "
            "(delete_branch_on_merge is enabled on GitHub)."
        )

    repo_root = _resolve_repo_root(hook_cwd)
    if repo_root is not None:
        plan = find_cleanup_candidates(repo_root, current_cwd=hook_cwd, include_gh=False)
        linked_wts = [wt for wt in _list_worktrees(repo_root) if not wt.is_main and not wt.is_bare]
        if plan.worktrees_to_remove:
            wt_list = ", ".join(str(wt.path) for wt in plan.worktrees_to_remove)
            notes.append(f"Stale clean worktrees detected: {wt_list}.")
        elif linked_wts:
            wt_list = ", ".join(str(wt.path) for wt in linked_wts)
            notes.append(f"Linked worktrees still present: {wt_list}.")
        if plan.branches_to_delete:
            br_list = ", ".join(name for name, _ in plan.branches_to_delete)
            notes.append(f"Merged/stale local branches detected: {br_list}.")

    notes.append(
        "Run 'python3 scripts/cleanup_git.py --apply' (and remove finished linked worktrees) "
        "before starting the next task."
    )

    output = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": " ".join(notes),
        }
    }
    return json.dumps(output)


def _format_plan(plan: CleanupPlan) -> str:
    """Render a human-readable summary of planned cleanups."""
    lines: list[str] = []
    if plan.worktrees_to_remove:
        lines.append("Worktrees to remove:")
        for wt in plan.worktrees_to_remove:
            label = wt.branch if wt.branch else f"detached@{wt.head[:7]}"
            lines.append(f"  - {wt.path} [{label}]")
    if plan.dirty_worktrees:
        lines.append("Skipped dirty stale worktrees:")
        for wt in plan.dirty_worktrees:
            lines.append(f"  - {wt.path} (has uncommitted/untracked changes)")
    if plan.branches_to_delete:
        lines.append("Local branches to delete:")
        for name, force in plan.branches_to_delete:
            mode = "-D (squash/gone)" if force else "-d (ancestor)"
            lines.append(f"  - {name} [{mode}]")
    if not lines:
        return "No merged branches or clean stale worktrees found."
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for git branch/worktree cleanup and hook handling."""
    parser = argparse.ArgumentParser(
        description="Audit and clean up merged branches and clean unused git worktrees."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Remove eligible clean worktrees and merged branches (default is dry-run).",
    )
    parser.add_argument(
        "--base",
        default="origin/main",
        help="Base ref to check ancestry against (default: origin/main).",
    )
    parser.add_argument(
        "--no-gh",
        action="store_true",
        help="Skip querying GitHub PR state via gh CLI.",
    )
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="Skip 'git fetch --prune origin' during --apply.",
    )
    parser.add_argument(
        "--hook-post-tool-use",
        action="store_true",
        help="Read PostToolUse hook JSON from stdin and print hook context JSON.",
    )
    args = parser.parse_args(argv)

    cwd = Path.cwd()
    if args.hook_post_tool_use:
        response = handle_post_tool_use_hook(sys.stdin.read(), cwd)
        if response is not None:
            print(response)
        return 0

    repo_root = _resolve_repo_root(cwd)
    if repo_root is None:
        print("Not inside a git repository.", file=sys.stderr)
        return 1

    plan = find_cleanup_candidates(
        repo_root,
        current_cwd=cwd,
        base_ref=args.base,
        include_gh=not args.no_gh,
    )
    print(_format_plan(plan))

    if args.apply:
        for line in apply_cleanup(plan, repo_root, prune_remote=not args.no_fetch):
            print(line)
    elif plan.worktrees_to_remove or plan.branches_to_delete:
        print("\nDry-run only. Re-run with --apply to execute.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
