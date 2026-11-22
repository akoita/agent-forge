"""Unit tests for ``scripts/cleanup_git.py``."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "cleanup_git.py"


def _load_module() -> ModuleType:
    """Import ``scripts/cleanup_git.py`` as a module from its file path."""
    spec = importlib.util.spec_from_file_location("cleanup_git", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cg: ModuleType = _load_module()


def _git(cwd: Path, *args: str) -> str:
    """Run git synchronously inside *cwd* and assert success."""
    git_exe = shutil.which("git")
    assert git_exe is not None
    proc = subprocess.run(  # noqa: S603
        [git_exe, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def test_parse_worktrees_identifies_main_linked_and_detached() -> None:
    porcelain = (
        "worktree /repo/main\n"
        "HEAD 1111111111111111111111111111111111111111\n"
        "branch refs/heads/main\n"
        "\n"
        "worktree /repo/wt-feature\n"
        "HEAD 2222222222222222222222222222222222222222\n"
        "branch refs/heads/feat/10-test\n"
        "\n"
        "worktree /repo/wt-detached\n"
        "HEAD 3333333333333333333333333333333333333333\n"
        "detached\n"
    )
    items = cg.parse_worktrees(porcelain)

    assert len(items) == 3
    assert items[0].is_main is True
    assert items[0].branch == "main"
    assert items[1].is_main is False
    assert items[1].branch == "feat/10-test"
    assert items[2].is_main is False
    assert items[2].is_detached is True
    assert items[2].branch is None


def test_parse_local_branches_tracks_gone_and_head() -> None:
    output = (
        "main\torigin/main\t\t*\n"
        "feat/merged\torigin/feat/merged\t[gone]\t \n"
        "feat/wip\torigin/feat/wip\t[ahead 1]\t \n"
    )
    branches = cg.parse_local_branches(output)

    assert [b.name for b in branches] == ["main", "feat/merged", "feat/wip"]
    assert branches[0].is_head is True
    assert branches[0].upstream_gone is False
    assert branches[1].upstream_gone is True
    assert branches[2].upstream_gone is False


def test_cleanup_removes_only_clean_merged_worktrees_and_branches(tmp_path: Path) -> None:
    origin = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    wt_clean_merged = tmp_path / "wt-clean-merged"
    wt_dirty_merged = tmp_path / "wt-dirty-merged"

    _git(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))
    _git(tmp_path, "clone", str(origin), str(repo))
    _git(repo, "config", "user.name", "Tester")
    _git(repo, "config", "user.email", "tester@example.com")

    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    _git(repo, "push", "-u", "origin", "main")

    # Merged branch inside a clean linked worktree.
    _git(repo, "worktree", "add", "-b", "feat/clean-merged", str(wt_clean_merged), "main")

    # Merged branch inside a dirty linked worktree (must be preserved).
    _git(repo, "worktree", "add", "-b", "feat/dirty-merged", str(wt_dirty_merged), "main")
    (wt_dirty_merged / "scratch.txt").write_text("untracked\n", encoding="utf-8")

    # Unmerged standalone local branch (must be preserved).
    _git(repo, "checkout", "-b", "feat/unmerged-wip", "main")
    (repo / "wip.txt").write_text("wip\n", encoding="utf-8")
    _git(repo, "add", "wip.txt")
    _git(repo, "commit", "-m", "wip")

    # Standalone merged local branch (no worktree; should be deleted).
    _git(repo, "checkout", "-b", "feat/standalone-merged", "main")
    _git(repo, "checkout", "main")

    plan = cg.find_cleanup_candidates(
        repo,
        current_cwd=repo,
        base_ref="origin/main",
        include_gh=False,
    )

    assert [wt.path.resolve() for wt in plan.worktrees_to_remove] == [wt_clean_merged.resolve()]
    assert [wt.path.resolve() for wt in plan.dirty_worktrees] == [wt_dirty_merged.resolve()]
    assert dict(plan.branches_to_delete) == {
        "feat/clean-merged": False,
        "feat/standalone-merged": False,
    }

    cg.apply_cleanup(plan, repo, prune_remote=False)

    assert not wt_clean_merged.exists()
    assert wt_dirty_merged.exists()
    remaining = set(_git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads").split())
    assert remaining == {"main", "feat/dirty-merged", "feat/unmerged-wip"}


def test_hook_post_tool_use_ignores_non_merge_and_invalid_json(tmp_path: Path) -> None:
    assert cg.handle_post_tool_use_hook("not-json", tmp_path) is None
    non_merge = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git status"}})
    assert cg.handle_post_tool_use_hook(non_merge, tmp_path) is None


def test_hook_post_tool_use_warns_when_delete_branch_omitted(tmp_path: Path) -> None:
    without_flag = json.dumps(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "gh pr merge 172 --squash"},
            "cwd": str(tmp_path),
        }
    )
    raw = cg.handle_post_tool_use_hook(without_flag, tmp_path)
    assert raw is not None
    parsed = json.loads(raw)
    hook_out = parsed["hookSpecificOutput"]
    assert hook_out["hookEventName"] == "PostToolUse"
    assert "--delete-branch" in hook_out["additionalContext"]
    assert "scripts/cleanup_git.py --apply" in hook_out["additionalContext"]

    with_flag = json.dumps(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "gh pr merge 172 --squash --delete-branch"},
            "cwd": str(tmp_path),
        }
    )
    raw_with_flag = cg.handle_post_tool_use_hook(with_flag, tmp_path)
    assert raw_with_flag is not None
    parsed_with_flag = json.loads(raw_with_flag)
    ctx_with_flag = parsed_with_flag["hookSpecificOutput"]["additionalContext"]
    assert "omitted --delete-branch" not in ctx_with_flag
    assert "scripts/cleanup_git.py --apply" in ctx_with_flag
