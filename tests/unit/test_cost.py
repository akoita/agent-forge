"""Tests for agent_forge.observability.cost — token/cost tracking & run summary."""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_forge.agent.models import AgentConfig, AgentRun, RunState, ToolInvocation
from agent_forge.llm.base import TokenUsage
from agent_forge.observability.cost import (
    COST_TABLE,
    CostEntry,
    CostTracker,
    _estimate_cost,
    print_run_summary,
    save_summary,
)
from agent_forge.tools.base import ToolResult

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_run(
    *,
    state: RunState = RunState.COMPLETED,
    iterations: int = 3,
    task: str = "Add input validation",
) -> AgentRun:
    """Create a minimal AgentRun for testing."""
    run = AgentRun(
        task=task,
        repo_path="/tmp/test-repo",
        config=AgentConfig(model="gemini-2.0-flash"),
    )
    run.state = state
    run.iterations = iterations
    run.created_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    run.completed_at = datetime(2026, 1, 1, 0, 0, 47, tzinfo=UTC)
    return run


# ===========================================================================
# CostEntry Tests
# ===========================================================================


class TestCostEntry:
    """Tests for CostEntry dataclass."""

    def test_construction(self) -> None:
        entry = CostEntry(
            model="gpt-4o",
            prompt_tokens=1000,
            completion_tokens=500,
            estimated_cost_usd=0.0075,
        )
        assert entry.model == "gpt-4o"
        assert entry.prompt_tokens == 1000
        assert entry.completion_tokens == 500
        assert entry.estimated_cost_usd == 0.0075
        assert entry.timestamp is not None

    def test_default_timestamp(self) -> None:
        entry = CostEntry(model="x", prompt_tokens=0, completion_tokens=0, estimated_cost_usd=0.0)
        assert entry.timestamp.tzinfo is not None


# ===========================================================================
# COST_TABLE Tests
# ===========================================================================


class TestCostTable:
    """Tests for the pricing table."""

    @pytest.mark.parametrize(
        "model",
        [
            "gemini-2.0-flash",
            "gemini-3.1-flash-lite-preview",
            "gpt-4o",
            "gpt-5.4",
            "claude-sonnet-4-20250514",
            "claude-sonnet-4-6",
            "claude-opus-4-6",
        ],
    )
    def test_model_present(self, model: str) -> None:
        assert model in COST_TABLE

    def test_all_prices_have_required_keys(self) -> None:
        for model in ["gemini-2.0-flash", "gpt-5.4", "claude-sonnet-4-6"]:
            prices = COST_TABLE[model]
            assert prices["input_cost_per_token"] > 0, f"{model} input price should be > 0"
            assert prices["output_cost_per_token"] > 0, f"{model} output price should be > 0"


# ===========================================================================
# Cost Estimation Tests
# ===========================================================================


class TestCostEstimation:
    """Tests for the cost calculation math."""

    def test_known_cost(self) -> None:
        usage = TokenUsage(
            prompt_tokens=1_000_000,
            completion_tokens=1_000_000,
            total_tokens=2_000_000,
        )
        cost = _estimate_cost(usage, "gpt-4o")
        # gpt-4o per-token: input=2.5e-6, output=1e-5
        # 1M * 2.5e-6 + 1M * 1e-5 = 2.50 + 10.00 = 12.50
        assert cost == pytest.approx(12.50)

    def test_gemini_cost(self) -> None:
        usage = TokenUsage(
            prompt_tokens=10_000,
            completion_tokens=3_000,
            total_tokens=13_000,
        )
        cost = _estimate_cost(usage, "gemini-2.0-flash")
        # gemini-2.0-flash per-token: input=1e-7, output=4e-7
        # 10k * 1e-7 + 3k * 4e-7 = 0.001 + 0.0012 = 0.0022
        assert cost == pytest.approx(0.0022)

    def test_unknown_model_returns_zero(self) -> None:
        usage = TokenUsage(prompt_tokens=5000, completion_tokens=2000, total_tokens=7000)
        cost = _estimate_cost(usage, "unknown-model-xyz")
        assert cost == 0.0

    def test_zero_tokens(self) -> None:
        usage = TokenUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0)
        cost = _estimate_cost(usage, "gpt-4o")
        assert cost == 0.0


# ===========================================================================
# CostTracker Tests
# ===========================================================================


class TestCostTracker:
    """Tests for CostTracker record/aggregate operations."""

    def test_record_and_total(self) -> None:
        tracker = CostTracker(run_id="test-run")
        tracker.record(
            TokenUsage(prompt_tokens=1000, completion_tokens=500, total_tokens=1500),
            "gpt-4o",
        )
        tracker.record(
            TokenUsage(prompt_tokens=2000, completion_tokens=1000, total_tokens=3000),
            "gpt-4o",
        )
        assert len(tracker.entries) == 2
        assert tracker.total_cost() > 0

    def test_summary(self) -> None:
        tracker = CostTracker(run_id="sum-run")
        tracker.record(
            TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
            "gemini-2.0-flash",
        )
        s = tracker.summary()
        assert s["run_id"] == "sum-run"
        assert s["total_prompt_tokens"] == 100
        assert s["total_completion_tokens"] == 50
        assert s["total_tokens"] == 150
        assert s["llm_calls"] == 1
        assert s["total_cost_usd"] >= 0

    def test_empty_tracker(self) -> None:
        tracker = CostTracker(run_id="empty")
        assert tracker.total_cost() == 0.0
        s = tracker.summary()
        assert s["total_tokens"] == 0
        assert s["llm_calls"] == 0


# ===========================================================================
# Print Run Summary Tests
# ===========================================================================


class TestPrintRunSummary:
    """Tests for the box-drawn console summary."""

    def test_output_contains_key_fields(self) -> None:
        import io

        run = _make_run()
        tracker = CostTracker(run_id=run.id)
        tracker.record(
            TokenUsage(prompt_tokens=12000, completion_tokens=3000, total_tokens=15000),
            "gemini-2.0-flash",
        )

        buf = io.StringIO()
        result = print_run_summary(run, tracker, file=buf)

        assert run.id in result
        assert "COMPLETED" in result
        assert "Add input validation" in result
        assert "47.0s" in result
        assert "gemini-2.0-flash" in result
        assert "12,000" in result
        assert "3,000" in result

    def test_with_tool_invocations(self) -> None:
        import io

        run = _make_run()
        run.tool_invocations.append(
            ToolInvocation(
                tool_name="read_file",
                arguments={"path": "foo.py"},
                result=ToolResult(output="ok", exit_code=0),
                iteration=1,
                timestamp=datetime.now(UTC),
                duration_ms=42,
            )
        )
        tracker = CostTracker(run_id=run.id)
        buf = io.StringIO()
        result = print_run_summary(run, tracker, file=buf)
        assert "read_file" in result
        assert "1 call" in result

    def test_with_modified_files(self) -> None:
        import io

        run = _make_run()
        run.tool_invocations.append(
            ToolInvocation(
                tool_name="write_file",
                arguments={"path": "src/api.py"},
                result=ToolResult(output="ok", exit_code=0),
                iteration=1,
                timestamp=datetime.now(UTC),
                duration_ms=30,
            )
        )
        tracker = CostTracker(run_id=run.id)
        buf = io.StringIO()
        result = print_run_summary(run, tracker, file=buf)
        assert "src/api.py" in result

    def test_failed_state(self) -> None:
        import io

        run = _make_run(state=RunState.FAILED)
        tracker = CostTracker(run_id=run.id)
        buf = io.StringIO()
        result = print_run_summary(run, tracker, file=buf)
        assert "FAILED" in result


# ===========================================================================
# Save Summary Tests
# ===========================================================================


class TestSaveSummary:
    """Tests for summary.json persistence."""

    def test_writes_valid_json(self) -> None:
        run = _make_run()
        tracker = CostTracker(run_id=run.id)
        tracker.record(
            TokenUsage(prompt_tokens=500, completion_tokens=200, total_tokens=700),
            "gemini-2.0-flash",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = save_summary(run, tracker, base_dir=tmpdir)
            assert path.exists()
            data = json.loads(path.read_text())

            assert data["run_id"] == run.id
            assert data["state"] == "completed"
            assert data["task"] == "Add input validation"
            assert data["model"] == "gemini-2.0-flash"
            assert data["duration_seconds"] == 47.0
            assert data["iterations"] == 3
            assert data["tokens"]["prompt"] == 500
            assert data["tokens"]["completion"] == 200
            assert data["tokens"]["total"] == 700
            assert data["estimated_cost_usd"] >= 0
            assert data["llm_calls"] == 1

    def test_creates_directory(self) -> None:
        run = _make_run()
        tracker = CostTracker(run_id=run.id)

        with tempfile.TemporaryDirectory() as tmpdir:
            nested = Path(tmpdir) / "nested" / "runs"
            path = save_summary(run, tracker, base_dir=nested)
            assert path.exists()
