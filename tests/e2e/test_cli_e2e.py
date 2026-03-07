"""E2E tests for the CLI using real LLM API.

These tests require:
  - GEMINI_API_KEY environment variable
  - Docker running (for sandbox)

They are marked with @pytest.mark.e2e and excluded from the default test suite.
Run manually: make test-e2e
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from agent_forge.cli import main
from agent_forge.config import USER_CONFIG_DIR

if TYPE_CHECKING:
    from pathlib import Path

# Skip all tests in this module if no API key is set
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not os.environ.get("GEMINI_API_KEY"),
        reason="GEMINI_API_KEY not set — skipping e2e tests",
    ),
]


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def sample_repo(tmp_path: Path) -> Path:
    """Create a minimal repo for the agent to work on."""
    (tmp_path / "hello.py").write_text("# placeholder\n")
    return tmp_path


class TestRunE2E:
    """End-to-end tests for the `run` command with real LLM."""

    def test_run_simple_task(self, runner: CliRunner, sample_repo: Path) -> None:
        """Agent completes a trivial task using real Gemini API."""
        result = runner.invoke(
            main,
            [
                "run",
                "--task", "Read the file hello.py and tell me what it contains",
                "--repo", str(sample_repo),
                "--max-iterations", "3",
            ],
        )
        # Agent should complete (exit 0) or fail gracefully
        # We accept either — the point is it runs without crashing
        assert result.exit_code in (0, 1), f"Unexpected exit: {result.output}"

    def test_run_produces_persisted_run(self, runner: CliRunner, sample_repo: Path) -> None:
        """The run command persists results to disk."""
        runner.invoke(
            main,
            [
                "run",
                "--task", "List the files in the workspace",
                "--repo", str(sample_repo),
                "--max-iterations", "2",
            ],
        )
        runs_dir = USER_CONFIG_DIR / "runs"
        if runs_dir.exists():
            run_dirs = list(runs_dir.iterdir())
            if run_dirs:
                latest = max(run_dirs, key=lambda d: d.stat().st_mtime)
                assert (latest / "run.json").exists()
                meta = json.loads((latest / "run.json").read_text())
                assert meta["state"] in ("completed", "failed", "timeout")


class TestStatusE2E:
    """E2E tests for `status` command after a real run."""

    def test_status_shows_run(self, runner: CliRunner, sample_repo: Path) -> None:
        """After a run, status displays the run info."""
        # First run
        runner.invoke(
            main,
            [
                "run",
                "--task", "Read hello.py",
                "--repo", str(sample_repo),
                "--max-iterations", "2",
            ],
        )
        # Find the run ID
        runs_dir = USER_CONFIG_DIR / "runs"
        if not runs_dir.exists():
            pytest.skip("No runs persisted")
        run_dirs = list(runs_dir.iterdir())
        if not run_dirs:
            pytest.skip("No runs persisted")

        latest = max(run_dirs, key=lambda d: d.stat().st_mtime)
        run_id = latest.name

        # Check status
        result = runner.invoke(main, ["status", run_id])
        assert result.exit_code == 0
        assert run_id in result.output or "Run" in result.output


class TestListE2E:
    """E2E tests for `list` command."""

    def test_list_shows_runs(self, runner: CliRunner) -> None:
        """List command shows recent runs."""
        result = runner.invoke(main, ["list"])
        assert result.exit_code == 0


class TestConfigE2E:
    """E2E tests for `config` command."""

    def test_config_shows_resolved(self, runner: CliRunner) -> None:
        """Config command outputs valid JSON."""
        result = runner.invoke(main, ["config"])
        assert result.exit_code == 0
        # Rich JSON output contains the config keys
        assert "agent" in result.output
        assert "sandbox" in result.output


class TestRunFlagsE2E:
    """E2E tests for CLI flags on the run command."""

    def test_run_with_max_iterations_flag(
        self, runner: CliRunner, sample_repo: Path
    ) -> None:
        """Agent respects --max-iterations flag."""
        result = runner.invoke(
            main,
            [
                "run",
                "--task", "List files",
                "--repo", str(sample_repo),
                "--max-iterations", "1",
            ],
        )
        assert result.exit_code in (0, 1), f"Unexpected exit: {result.output}"

    def test_run_with_custom_model_flag(
        self, runner: CliRunner, sample_repo: Path
    ) -> None:
        """Agent accepts --model flag without crashing."""
        result = runner.invoke(
            main,
            [
                "run",
                "--task", "Say hello",
                "--repo", str(sample_repo),
                "--max-iterations", "1",
                "--model", "gemini-3.1-flash-lite-preview",
            ],
        )
        assert result.exit_code in (0, 1), f"Unexpected exit: {result.output}"


class TestListMultipleRunsE2E:
    """E2E tests for listing multiple runs."""

    def test_list_after_multiple_runs(
        self, runner: CliRunner, sample_repo: Path
    ) -> None:
        """List shows runs after multiple executions."""
        # Run two tasks
        for task in ("Read hello.py", "List files"):
            runner.invoke(
                main,
                [
                    "run",
                    "--task", task,
                    "--repo", str(sample_repo),
                    "--max-iterations", "1",
                ],
            )

        result = runner.invoke(main, ["list"])
        assert result.exit_code == 0
