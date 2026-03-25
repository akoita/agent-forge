"""Unit tests for the CLI (mocked, no API key needed)."""

from __future__ import annotations

import json
import os
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

    def test_run_json_output_and_report_file(self, tmp_path: Path) -> None:
        """Run can emit a machine-readable result payload."""
        from datetime import UTC, datetime

        from agent_forge.agent.models import AgentConfig, AgentRun, RunState

        async def fake_run(
            task: str,
            repo: str,
            cfg: object,
            provider_name: str,
            api_key: str,
        ) -> AgentRun:
            run = AgentRun(task=task, repo_path=repo, config=AgentConfig())
            run.state = RunState.COMPLETED
            run.iterations = 2
            run.completed_at = datetime.now(UTC)
            return run

        runner = _runner()
        env = dict(os.environ)
        env["GEMINI_API_KEY"] = "test-key"
        report_file = tmp_path / "report.json"
        with patch("agent_forge.cli._run_agent", side_effect=fake_run), patch(
            "agent_forge.cli.USER_CONFIG_DIR", tmp_path
        ):
            result = runner.invoke(
                main,
                [
                    "run",
                    "--task", "Fix a bug",
                    "--repo", "/tmp/fake-repo",
                    "--output-format", "json",
                    "--report-file", str(report_file),
                ],
                env=env,
            )

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["schema_version"] == "agent-forge-run-result-v1"
        assert payload["state"] == "completed"
        assert payload["artifacts"]["summary_json"].endswith("/summary.json")

        file_payload = json.loads(report_file.read_text())
        assert file_payload["run_id"] == payload["run_id"]
        assert file_payload["task"] == "Fix a bug"

    def test_run_json_output_rejected_for_queue_mode(self) -> None:
        """Queue mode should fail clearly until machine output is supported there."""
        runner = _runner()
        env = dict(os.environ)
        env["GEMINI_API_KEY"] = "test-key"
        result = runner.invoke(
            main,
            [
                "run",
                "--task", "Fix a bug",
                "--repo", "/tmp/fake-repo",
                "--queue", "memory",
                "--output-format", "json",
            ],
            env=env,
        )
        assert result.exit_code != 0
        assert "not supported in queue mode yet" in result.output


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

    def test_status_existing_run_json(self, tmp_path: Path) -> None:
        """Status can emit a machine-readable result payload."""
        from datetime import UTC, datetime

        from agent_forge.agent.models import AgentConfig, AgentRun, RunState
        from agent_forge.agent.persistence import save_run

        run = AgentRun(task="Test task", repo_path="/tmp/repo", config=AgentConfig())
        run.state = RunState.COMPLETED
        run.iterations = 5
        run.completed_at = datetime.now(UTC)
        save_run(run, base_dir=tmp_path)

        runner = _runner()
        with patch("agent_forge.agent.persistence._default_runs_dir", return_value=tmp_path), patch(
            "agent_forge.cli.USER_CONFIG_DIR", tmp_path.parent
        ):
            result = runner.invoke(main, ["status", run.id, "--output-format", "json"])

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["schema_version"] == "agent-forge-run-result-v1"
        assert payload["run_id"] == run.id
        assert payload["state"] == "completed"


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
