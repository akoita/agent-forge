"""Deterministic grader checks for every built-in benchmark corpus task."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_forge.agent.journal import replay_journal
from agent_forge.eval import (
    BUILTIN_TASKS,
    BenchmarkTask,
    check_deterministic_gates,
    materialize_task_repo,
    run_deterministic_eval,
)


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
        expected_final = (
            "completed" if summary.profile == "deterministic-oracle" else "verification_failed"
        )
        assert summary.false_positive_rate == 0.0
        for attempt in summary.results:
            assert attempt.trajectory_path is not None
            traj_file = (
                tmp_path / "trajectories" / f"{summary.profile}__{attempt.task_id}__attempt-1.jsonl"
            )
            assert traj_file.is_file()
            entries = [
                json.loads(line) for line in traj_file.read_text(encoding="utf-8").splitlines()
            ]
            assert all(row["schema_version"] == "run-journal-v1" for row in entries)
            kinds = [row["kind"] for row in entries]
            assert "run_started" in kinds
            assert "state_transition" in kinds
            assert "message_recorded" in kinds
            assert "action_requested" in kinds
            assert "action_result" in kinds
            assert "verification_recorded" in kinds
            assert "contract_evaluated" in kinds
            assert kinds[-1] == "attempt_result"

            replayed = replay_journal(traj_file)
            assert replayed.valid is True
            assert replayed.duplicate_side_effects == []
            assert replayed.state_sequence == ["pending", "running", expected_final]
            assert replayed.contract_evaluation is not None
            assert replayed.attempt_result is not None
            assert replayed.attempt_result["task_id"] == attempt.task_id


@pytest.mark.asyncio
async def test_completion_contract_eliminates_false_positive_completions() -> None:
    """Measure that uncontracted stops produce false positives while contracts block all."""
    import tempfile

    from agent_forge.agent.core import react_loop
    from agent_forge.agent.models import AgentConfig, AgentRun
    from agent_forge.agent.verification import VerificationWorkspace
    from agent_forge.config import ModelProfileSettings
    from agent_forge.eval.benchmark import (
        build_completed_result,
        materialize_task_repo,
        summarize_profile,
    )
    from agent_forge.eval.corpus import resolve_tasks
    from agent_forge.eval.deterministic import SubprocessEvalSandbox, _ScriptedEvalLLM
    from agent_forge.sandbox.base import SandboxConfig
    from agent_forge.tools import create_default_registry

    smoke_tasks = resolve_tasks(suite="smoke")
    tools = create_default_registry(load_plugins=False)
    sandbox_cfg = SandboxConfig(
        backend="subprocess-eval",
        network_enabled=False,
        writable_cache_mounts=False,
        timeout_seconds=20,
    )

    uncontracted_results = []
    contracted_results = []

    for mode in ("noop", "incomplete"):
        for task in smoke_tasks:
            with tempfile.TemporaryDirectory() as tmp_dir:
                repo_path = materialize_task_repo(task, Path(tmp_dir) / "repo")
                cfg = AgentConfig(
                    model=f"scripted-{mode}", provider="deterministic", max_iterations=10
                )

                # Without verifier/contract: react_loop stops cleanly -> false-positive COMPLETED.
                run_no_contract = AgentRun(task=task.prompt, repo_path=str(repo_path), config=cfg)
                sb1 = SubprocessEvalSandbox()
                await sb1.start(repo_path=str(repo_path), config=sandbox_cfg)
                try:
                    await react_loop(run_no_contract, _ScriptedEvalLLM(task, mode), tools, sb1)
                finally:
                    await sb1.stop()
                uncontracted_results.append(
                    build_completed_result(
                        task.id, 1, run_no_contract, cfg.model, None, [cfg.model], 0.1
                    )
                )

                # With harness completion contract + independent verifier: 0 false positives.
                run_contracted = AgentRun(task=task.prompt, repo_path=str(repo_path), config=cfg)
                sb2 = SubprocessEvalSandbox()
                with VerificationWorkspace(
                    str(repo_path), task.verify_command, extra_verifier_files=task.hidden_tests
                ) as ws:
                    await sb2.start(repo_path=str(ws.workspace), config=sandbox_cfg)
                    try:
                        await react_loop(
                            run_contracted,
                            _ScriptedEvalLLM(task, mode),
                            tools,
                            sb2,
                            verify=lambda ws=ws, sb=sb2: ws.verify(
                                sandbox_cfg,
                                sb,
                                sandbox_factory=lambda _c: SubprocessEvalSandbox(),
                            ),
                        )
                    finally:
                        await sb2.stop()
                contracted_results.append(
                    build_completed_result(
                        task.id, 1, run_contracted, cfg.model, None, [cfg.model], 0.1
                    )
                )

    profile = ModelProfileSettings(
        provider="deterministic", model="scripted-wrong", temperature=0.0
    )
    before = summarize_profile(
        "uncontracted",
        profile,
        uncontracted_results,
        temperature=0.0,
        max_iterations=10,
        max_tokens_per_run=40_000,
    )
    after = summarize_profile(
        "contracted",
        profile,
        contracted_results,
        temperature=0.0,
        max_iterations=10,
        max_tokens_per_run=40_000,
    )

    assert before.false_positive_rate == 1.0
    assert after.false_positive_rate == 0.0
    assert all(r.run_state == "verification_failed" and r.unmet_clauses for r in contracted_results)
