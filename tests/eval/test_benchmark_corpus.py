"""Deterministic grader checks for every built-in benchmark corpus task."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from agent_forge.eval import (
    BUILTIN_TASKS,
    BenchmarkTask,
    check_deterministic_gates,
    materialize_task_repo,
    run_deterministic_eval,
)

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


def _write_files(repo: Path, files: dict[str, str]) -> None:
    for rel_path, content in files.items():
        target = repo / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


@pytest.mark.parametrize("task", BUILTIN_TASKS, ids=[t.id for t in BUILTIN_TASKS])
def test_builtin_task_discriminates_base_empty_incomplete_and_fixed(
    tmp_path: Path,
    task: BenchmarkTask,
) -> None:
    """Verify every pinned task fails on base/empty/incomplete and passes on its known fix."""
    assert len(BUILTIN_TASKS) >= 12
    assert task.hidden_tests, f"{task.id} must define hidden verifier tests"
    assert task.incomplete_files, f"{task.id} must define an incomplete partial fix"

    repo = materialize_task_repo(task, tmp_path / task.id)
    _write_files(repo, task.hidden_tests)

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

    # Incomplete / partial fix modifies tracked source but fails combined visible + hidden checks
    _write_files(repo, task.incomplete_files)
    incomplete_diff = subprocess.run(
        ["git", "diff", "--name-only"],  # noqa: S607
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert incomplete_diff.stdout.strip() != ""
    assert _run_unittest(repo).returncode != 0, f"{task.id} incomplete fix unexpectedly passed"

    # Reset and apply known-good fix -> produces non-empty diff and passes all tests
    subprocess.run(
        ["git", "checkout", "--", "."],  # noqa: S607
        cwd=repo,
        check=True,
        capture_output=True,
        timeout=10,
    )
    _write_files(repo, task.known_good_files)

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


@pytest.mark.asyncio
async def test_deterministic_eval_smoke_gates_and_trajectories(tmp_path: Path) -> None:
    """Offline smoke eval passes deterministic oracle and rejects noop/incomplete runs."""
    traj_dir = tmp_path / "trajectories"
    report_json = tmp_path / "report.json"
    report_md = tmp_path / "report.md"

    report = await run_deterministic_eval(
        suite="smoke",
        output_json=report_json,
        output_md=report_md,
        trajectory_dir=traj_dir,
    )

    assert len(report.tasks) == 4
    assert check_deterministic_gates(report) == []
    assert report_json.is_file()
    assert report_md.is_file()

    by_profile = {item.profile: item for item in report.profiles}
    assert by_profile["deterministic-oracle"].verified_fixes == 4
    assert by_profile["deterministic-noop"].verified_fixes == 0
    assert by_profile["deterministic-incomplete"].verified_fixes == 0

    for summary in report.profiles:
        for attempt in summary.results:
            assert attempt.trajectory_path is not None
            traj_file = (
                tmp_path / "trajectories" / f"{summary.profile}__{attempt.task_id}__attempt-1.jsonl"
            )
            assert traj_file.is_file()
            kinds = [
                json.loads(line)["kind"]
                for line in traj_file.read_text(encoding="utf-8").splitlines()
            ]
            assert "run_header" in kinds
            assert "message" in kinds
            assert "tool_invocation" in kinds
            assert "verification" in kinds
            assert kinds[-1] == "attempt_result"
