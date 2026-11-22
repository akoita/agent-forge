"""Deterministic grader checks for every built-in benchmark corpus task."""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from agent_forge.eval import BUILTIN_TASKS, BenchmarkTask, materialize_task_repo

if TYPE_CHECKING:
    from pathlib import Path


def _run_unittest(repo: Path) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]
    return subprocess.run(  # noqa: S603
        cmd,
        cwd=repo,
        env={
            "PATH": os.environ.get("PATH", os.defpath),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@pytest.mark.parametrize("task", BUILTIN_TASKS, ids=[t.id for t in BUILTIN_TASKS])
def test_builtin_task_fails_on_base_and_empty_patch_and_passes_on_fix(
    tmp_path: Path,
    task: BenchmarkTask,
) -> None:
    """Verify every pinned task fails on base/empty patch and passes on its known fix."""
    repo = materialize_task_repo(task, tmp_path / task.id)

    base_check = _run_unittest(repo)
    assert base_check.returncode != 0, f"{task.id} unexpectedly passed on base"

    # Empty patch check (working tree clean against HEAD)
    empty_diff = subprocess.run(
        ["git", "diff", "HEAD"],  # noqa: S607
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert empty_diff.stdout == ""
    assert _run_unittest(repo).returncode != 0

    # Known-good patch produces a non-empty diff and passes independent tests
    for rel_path, fixed_content in task.known_good_files.items():
        (repo / rel_path).write_text(fixed_content, encoding="utf-8")

    fixed_diff = subprocess.run(
        ["git", "diff"],  # noqa: S607
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert fixed_diff.stdout.strip() != ""

    fixed_check = _run_unittest(repo)
    assert fixed_check.returncode == 0, fixed_check.stdout + fixed_check.stderr
