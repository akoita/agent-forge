"""Exercise verified CLI completion against a real local Git repository."""

from __future__ import annotations

import asyncio
import inspect
import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from agent_forge.agent import persistence
from agent_forge.cli import _run_agent, main
from agent_forge.config import ForgeConfig
from agent_forge.llm.base import LLMResponse
from agent_forge.sandbox.base import ExecResult


def _git(repository: Path, *args: str) -> str:
    return subprocess.check_output(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=repository,
        text=True,
        timeout=10,
    ).strip()


@pytest.fixture
def dirty_repository(tmp_path: Path) -> Path:
    """Return a committed repository with unrelated local edits."""
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "app.txt").write_text("committed\n")
    _git(repo, "add", "app.txt")
    _git(
        repo,
        "-c",
        "user.name=Tester",
        "-c",
        "user.email=test@example.com",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        "base",
    )
    (repo / "app.txt").write_text("user change\n")
    (repo / "untracked.txt").write_text("keep me\n")
    return repo


@pytest.mark.parametrize(
    ("edit", "check_code", "expected"),
    [(False, 0, "empty_patch"), (True, 1, "check_failed"), (True, 0, "verified")],
)
def test_cli_reports_verified_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dirty_repository: Path,
    *,
    edit: bool,
    check_code: int,
    expected: str,
) -> None:
    """The model stopping is insufficient for a successful verified fix."""
    repo = dirty_repository
    revision = _git(repo, "rev-parse", "HEAD")

    async def edit_candidate(*, repo_path: str, config: object) -> None:
        candidate = Path(repo_path)
        assert candidate != repo
        assert (candidate / "app.txt").read_text() == "committed\n"
        if edit:
            (candidate / "app.txt").write_text("agent change\n")
            (candidate / "added.txt").write_text("new file\n")

    async def start_verifier(repo_path: str, config: object) -> None:
        agent_sandbox.stop.assert_awaited()
        verifier = Path(repo_path)
        assert verifier != repo
        assert verifier != Path(agent_sandbox.start.call_args.kwargs["repo_path"])
        assert (verifier / "app.txt").read_text() == "agent change\n"
        assert (verifier / "added.txt").read_text() == "new file\n"
        assert not (verifier / "untracked.txt").exists()

    agent_sandbox = AsyncMock()
    agent_sandbox.start.side_effect = edit_candidate
    verifier_sandbox = AsyncMock()
    verifier_sandbox.start.side_effect = start_verifier
    verifier_sandbox.exec.return_value = ExecResult(check_code, "check output", "")
    model = AsyncMock()
    model.complete.return_value = LLMResponse(content="Done", finish_reason="stop")

    report_file = tmp_path / "report.json"
    runs_dir = tmp_path / "runs"
    monkeypatch.setattr(persistence, "_default_runs_dir", lambda: runs_dir)
    monkeypatch.setattr("agent_forge.cli.USER_CONFIG_DIR", tmp_path)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    runner_options = (
        {"mix_stderr": False} if "mix_stderr" in inspect.signature(CliRunner).parameters else {}
    )
    with (
        patch("agent_forge.sandbox.factory.create_sandbox", return_value=agent_sandbox),
        patch("agent_forge.agent.verification.create_sandbox", return_value=verifier_sandbox),
        patch("agent_forge.cli.create_provider", return_value=model),
        patch("agent_forge.agent.core.save_summary"),
    ):
        response = CliRunner(**runner_options).invoke(
            main,
            [
                "run",
                "--task",
                "Fix a fixture",
                "--repo",
                str(repo),
                "--verify-command",
                "check fixture",
                "--report-file",
                str(report_file),
                "--output-format",
                "json",
            ],
        )

    assert response.exit_code == (0 if expected == "verified" else 1), response.output
    payload = json.loads(response.stdout)
    assert payload == json.loads(report_file.read_text())
    assert "Run Summary" in response.stderr
    assert payload["verification"]["base_revision"] == revision
    assert payload["verification"]["reason"] == expected
    assert payload["state"] == ("completed" if expected == "verified" else "verification_failed")
    assert payload["completion_contract"]["passed"] is (expected == "verified")
    if expected != "verified":
        assert len(payload["completion_contract"]["unmet_clauses"]) >= 1
    assert (repo / "app.txt").read_text() == "user change\n"
    assert (repo / "untracked.txt").read_text() == "keep me\n"
    saved = persistence.load_run(payload["run_id"])
    assert saved.verification is not None
    assert saved.verification.reason == expected
    if edit:
        patch_file = Path(payload["artifacts"]["patch"])
        assert patch_file.read_text() == payload["verification"]["patch"]
        assert "+agent change" in patch_file.read_text()
        assert "+new file" in patch_file.read_text()
        verifier_sandbox.exec.assert_awaited_once_with("check fixture", timeout_seconds=300)
    else:
        verifier_sandbox.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_removes_candidate_checkout(dirty_repository: Path) -> None:
    """A cancelled verified run preserves the original and removes its temporary checkout."""
    model = AsyncMock()
    entered_model = asyncio.Event()

    async def wait_for_cancellation(*args: object, **kwargs: object) -> None:
        entered_model.set()
        await asyncio.Future()

    model.complete.side_effect = wait_for_cancellation
    sandbox = AsyncMock()
    with (
        patch("agent_forge.sandbox.factory.create_sandbox", return_value=sandbox),
        patch("agent_forge.cli.create_provider", return_value=model),
    ):
        task = asyncio.create_task(
            _run_agent(
                "Fix a fixture",
                str(dirty_repository),
                ForgeConfig(),
                "gemini",
                "test-key",
                verify_command="check fixture",
            )
        )
        await asyncio.wait_for(entered_model.wait(), timeout=5)
        candidate = Path(sandbox.start.call_args.kwargs["repo_path"])
        (candidate / "app.txt").write_text("agent change\n")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert not candidate.exists()
    sandbox.stop.assert_awaited()
    model.close.assert_awaited_once()
    assert (dirty_repository / "app.txt").read_text() == "user change\n"
    assert (dirty_repository / "untracked.txt").read_text() == "keep me\n"
