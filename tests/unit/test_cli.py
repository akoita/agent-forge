"""Unit tests for the CLI (mocked, no API key needed)."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from click.testing import CliRunner

from agent_forge.cli import main

if TYPE_CHECKING:
    from pathlib import Path


def _runner() -> CliRunner:
    return CliRunner()


class TestRunCommand:
    """Tests for the `run` command."""

    def test_run_missing_api_key(self) -> None:
        """Run exits with error when API key is not set."""
        runner = _runner()
        env = {k: v for k, v in __import__("os").environ.items() if not k.endswith("_API_KEY")}
        result = runner.invoke(
            main,
            ["run", "--task", "Fix a bug", "--repo", "/tmp/fake-repo"],
            env=env,
        )
        assert result.exit_code != 0
        assert "Missing API key" in result.output

    def test_run_unknown_provider(self) -> None:
        """Run exits with error for unknown provider."""
        runner = _runner()
        result = runner.invoke(
            main,
            [
                "run",
                "--task", "Fix a bug",
                "--repo", "/tmp/fake-repo",
                "--provider", "nonexistent",
            ],
        )
        assert result.exit_code != 0
        assert "Unknown provider" in result.output


class TestStatusCommand:
    """Tests for the `status` command."""

    def test_status_nonexistent_run(self) -> None:
        """Status exits with error for unknown run ID."""
        runner = _runner()
        result = runner.invoke(main, ["status", "nonexistent-run-id"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_status_existing_run(self, tmp_path: Path) -> None:
        """Status displays info for a persisted run."""
        from datetime import UTC, datetime

        from agent_forge.agent.models import AgentConfig, AgentRun, RunState
        from agent_forge.agent.persistence import save_run

        run = AgentRun(task="Test task", repo_path="/tmp/repo", config=AgentConfig())
        run.state = RunState.COMPLETED
        run.iterations = 5
        run.completed_at = datetime.now(UTC)
        save_run(run, base_dir=tmp_path)

        runner = _runner()
        with patch("agent_forge.agent.persistence._default_runs_dir", return_value=tmp_path):
            result = runner.invoke(main, ["status", run.id])

        assert result.exit_code == 0


class TestListCommand:
    """Tests for the `list` command."""

    def test_list_empty(self, tmp_path: Path) -> None:
        """List with no runs shows empty message."""
        runner = _runner()
        with patch("agent_forge.cli.USER_CONFIG_DIR", tmp_path):
            result = runner.invoke(main, ["list"])
        assert result.exit_code == 0
        assert "No runs" in result.output

    def test_list_with_runs(self, tmp_path: Path) -> None:
        """List shows runs from the runs directory."""
        from datetime import UTC, datetime

        from agent_forge.agent.models import AgentConfig, AgentRun, RunState
        from agent_forge.agent.persistence import save_run

        run = AgentRun(task="Fix the login bug", repo_path="/tmp/repo", config=AgentConfig())
        run.state = RunState.COMPLETED
        run.iterations = 3
        run.completed_at = datetime.now(UTC)
        save_run(run, base_dir=tmp_path / "runs")

        runner = _runner()
        with patch("agent_forge.cli.USER_CONFIG_DIR", tmp_path):
            result = runner.invoke(main, ["list"])

        assert result.exit_code == 0
        assert "login" in result.output


class TestConfigCommand:
    """Tests for the `config` command."""

    def test_config_shows_json(self) -> None:
        """Config outputs resolved configuration."""
        runner = _runner()
        result = runner.invoke(main, ["config"])
        assert result.exit_code == 0
        assert "agent" in result.output

    def test_config_valid_structure(self) -> None:
        """Config output contains expected sections."""
        runner = _runner()
        result = runner.invoke(main, ["config"])
        assert result.exit_code == 0
        assert "max_iterations" in result.output


class TestMainGroup:
    """Tests for the CLI group itself."""

    def test_help(self) -> None:
        runner = _runner()
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "Agent Forge" in result.output

    def test_version(self) -> None:
        runner = _runner()
        result = runner.invoke(main, ["--version"])
        assert result.exit_code == 0
