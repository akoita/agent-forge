"""Unit tests for core budget, auto-pause, and cancellation policies (#29)."""

from __future__ import annotations

import inspect
import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from agent_forge.agent.budget import BudgetPolicy, CancellationToken
from agent_forge.agent.checkpoint import load_checkpoint, save_checkpoint
from agent_forge.agent.core import react_loop
from agent_forge.agent.models import AgentConfig, AgentRun, RunState
from agent_forge.agent.persistence import load_run, save_run
from agent_forge.cli import main as cli_main
from agent_forge.llm.base import LLMResponse, TokenUsage, ToolCall
from agent_forge.orchestration.events import Event, EventBus, EventType
from agent_forge.sandbox.base import ExecResult, Sandbox, SandboxConfig
from agent_forge.tools.base import ToolDefinition, ToolResult

if TYPE_CHECKING:
    from pathlib import Path

    from agent_forge.tools import ToolRegistry


class _NoopSandbox(Sandbox):
    async def start(self, repo_path: str, config: SandboxConfig | None = None) -> None:
        del repo_path, config

    async def stop(self) -> None:
        return None

    async def is_alive(self) -> bool:
        return True

    async def read_file(self, path: str) -> str:
        raise FileNotFoundError(path)

    async def write_file(self, path: str, content: str) -> None:
        del path, content

    async def exec(self, command: str, *, timeout_seconds: int = 30) -> ExecResult:
        del command, timeout_seconds
        return ExecResult(exit_code=0, stdout="", stderr="")


def _tool_registry() -> ToolRegistry:
    registry = AsyncMock()
    registry.list_definitions = lambda: [
        ToolDefinition(
            name="read_file",
            description="Read file",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}},
        )
    ]
    tool = AsyncMock()
    tool.execute.return_value = ToolResult(output="ok", exit_code=0)
    registry.get = lambda _name: tool
    return registry  # type: ignore[return-value]


class TestBudgetPolicyUnit:
    """Verify BudgetPolicy thresholds and error validation."""

    def test_token_cost_and_time_limits(self) -> None:
        policy = BudgetPolicy(
            max_tokens=100,
            max_cost_usd=0.05,
            max_duration_seconds=30.0,
            on_exceed="pause",
        )
        ok = policy.evaluate(total_tokens=50, total_cost_usd=0.01, elapsed_seconds=5.0)
        assert ok.exceeded is False
        assert ok.action == "continue"

        tok = policy.evaluate(total_tokens=101, total_cost_usd=0.01, elapsed_seconds=5.0)
        assert tok.exceeded is True
        assert tok.dimension == "tokens"
        assert tok.action == "pause"

        cost = policy.evaluate(total_tokens=50, total_cost_usd=0.06, elapsed_seconds=5.0)
        assert cost.exceeded is True
        assert cost.dimension == "cost"
        assert cost.action == "pause"

        timed = policy.evaluate(total_tokens=50, total_cost_usd=0.01, elapsed_seconds=31.0)
        assert timed.exceeded is True
        assert timed.dimension == "time"
        assert timed.action == "pause"

    def test_invalid_action_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid budget action"):
            BudgetPolicy(max_tokens=100, on_exceed="halt")


class TestBudgetAndCancelLoopIntegration:
    """Verify auto-pause, resume-to-completion, cost/time budgets, and cancellation."""

    @pytest.mark.asyncio
    async def test_token_budget_auto_pauses_then_resumes_to_completion(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runs_dir = tmp_path / "runs"
        monkeypatch.setattr(
            "agent_forge.agent.core.save_run", lambda r: save_run(r, base_dir=runs_dir)
        )
        monkeypatch.setattr(
            "agent_forge.observability.cost.save_summary",
            lambda *_args: tmp_path / "summary.json",
        )

        bus = EventBus()
        captured: list[Event] = []

        async def _capture(event: Event) -> None:
            captured.append(event)

        for ev in (EventType.BUDGET_EXCEEDED, EventType.CHECKPOINT_SAVED, EventType.RUN_PAUSED):
            await bus.subscribe(ev, _capture)

        llm = AsyncMock()
        llm.complete.side_effect = [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "a.py"})],
                usage=TokenUsage(60, 60, 120),
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content="Done after raising budget.",
                tool_calls=[],
                usage=TokenUsage(20, 10, 30),
                finish_reason="stop",
            ),
        ]

        run = AgentRun(
            task="Solve task",
            repo_path=str(tmp_path),
            config=AgentConfig(max_iterations=5, max_tokens_per_run=100, budget_action="pause"),
        )

        async def _on_cp(r: AgentRun) -> None:
            save_checkpoint(r, base_revision="rev1", base_dir=runs_dir)

        paused = await react_loop(
            run,
            llm,
            _tool_registry(),
            _NoopSandbox(),
            event_bus=bus,
            on_checkpoint=_on_cp,
        )

        assert paused.state == RunState.PAUSED
        assert paused.completed_at is None
        assert "Token budget exceeded" in (paused.error or "")
        assert [e.type for e in captured] == [
            EventType.BUDGET_EXCEEDED,
            EventType.CHECKPOINT_SAVED,
            EventType.RUN_PAUSED,
        ]
        assert captured[0].data["dimension"] == "tokens"

        reloaded, _cp = load_checkpoint(run.id, base_dir=runs_dir)
        reloaded.config.max_tokens_per_run = 500
        completed = await react_loop(
            reloaded,
            llm,
            _tool_registry(),
            _NoopSandbox(),
            event_bus=bus,
            on_checkpoint=_on_cp,
        )
        assert completed.state == RunState.COMPLETED
        assert completed.completed_at is not None
        assert completed.error is None

    @pytest.mark.asyncio
    async def test_cost_and_time_budget_auto_pause(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runs_dir = tmp_path / "runs"
        monkeypatch.setattr(
            "agent_forge.agent.core.save_run", lambda r: save_run(r, base_dir=runs_dir)
        )
        monkeypatch.setattr(
            "agent_forge.observability.cost.save_summary",
            lambda *_args: tmp_path / "summary.json",
        )

        # Cost budget exceeded on gpt-4o tokens.
        llm_cost = AsyncMock()
        llm_cost.complete.return_value = LLMResponse(
            content="",
            tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "a.py"})],
            usage=TokenUsage(10_000, 1_000, 11_000),
            model="gpt-4o",
            finish_reason="tool_calls",
        )
        run_cost = AgentRun(
            task="Cost test",
            repo_path=str(tmp_path),
            config=AgentConfig(
                max_tokens_per_run=500_000,
                max_cost_usd=0.001,
                budget_action="pause",
            ),
        )
        paused_cost = await react_loop(run_cost, llm_cost, _tool_registry(), _NoopSandbox())
        assert paused_cost.state == RunState.PAUSED
        assert "Cost budget exceeded" in (paused_cost.error or "")

        # Time budget exceeded using monotonic clock advance.
        ticks = iter([100.0, 115.0])
        with monkeypatch.context() as m:
            m.setattr("agent_forge.agent.core.time.monotonic", lambda: next(ticks, 115.0))
            llm_time = AsyncMock()
            llm_time.complete.return_value = LLMResponse(
                content="",
                tool_calls=[ToolCall(id="c2", name="read_file", arguments={"path": "a.py"})],
                usage=TokenUsage(10, 10, 20),
                finish_reason="tool_calls",
            )
            run_time = AgentRun(
                task="Time test",
                repo_path=str(tmp_path),
                config=AgentConfig(
                    max_tokens_per_run=500_000,
                    max_duration_seconds=5.0,
                    budget_action="pause",
                ),
            )
            paused_time = await react_loop(run_time, llm_time, _tool_registry(), _NoopSandbox())
        assert paused_time.state == RunState.PAUSED
        assert "Time budget exceeded" in (paused_time.error or "")

    @pytest.mark.asyncio
    async def test_cooperative_cancellation_token_stops_before_llm(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runs_dir = tmp_path / "runs"
        monkeypatch.setattr(
            "agent_forge.agent.core.save_run", lambda r: save_run(r, base_dir=runs_dir)
        )
        monkeypatch.setattr(
            "agent_forge.observability.cost.save_summary",
            lambda *_args: tmp_path / "summary.json",
        )

        token = CancellationToken()
        token.cancel("Operator stopped run")
        llm = AsyncMock()
        bus = EventBus()
        cancelled_events: list[Event] = []

        async def _capture(event: Event) -> None:
            cancelled_events.append(event)

        await bus.subscribe(EventType.RUN_CANCELLED, _capture)
        run = AgentRun(task="Cancel test", repo_path=str(tmp_path), config=AgentConfig())

        res = await react_loop(
            run,
            llm,
            _tool_registry(),
            _NoopSandbox(),
            event_bus=bus,
            cancellation_token=token,
        )

        llm.complete.assert_not_called()
        assert res.state == RunState.CANCELLED
        assert res.error == "Operator stopped run"
        assert len(cancelled_events) == 1
        assert cancelled_events[0].data["reason"] == "Operator stopped run"


def test_cli_budget_flags_resume_and_cancel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs_dir = tmp_path / "runs"
    monkeypatch.setattr("agent_forge.cli.USER_CONFIG_DIR", tmp_path)
    monkeypatch.setattr("agent_forge.agent.persistence.USER_CONFIG_DIR", tmp_path)
    monkeypatch.setattr("agent_forge.config.USER_CONFIG_DIR", tmp_path)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    seen_budget: dict[str, object] = {}

    async def _fake_run_agent(*_args: object, **_kwargs: object) -> AgentRun:
        cfg_arg = _args[2]
        seen_budget["max_cost"] = cfg_arg.agent.max_cost_usd  # type: ignore[attr-defined]
        seen_budget["max_duration"] = cfg_arg.agent.max_duration_seconds  # type: ignore[attr-defined]
        seen_budget["budget_action"] = cfg_arg.agent.budget_action  # type: ignore[attr-defined]
        return AgentRun(
            task="t",
            repo_path=str(tmp_path),
            config=AgentConfig(),
            state=RunState.COMPLETED,
        )

    runner_options = (
        {"mix_stderr": False} if "mix_stderr" in inspect.signature(CliRunner).parameters else {}
    )
    runner = CliRunner(**runner_options)
    with patch("agent_forge.cli._run_agent", _fake_run_agent):
        run_res = runner.invoke(
            cli_main,
            [
                "run",
                "--task",
                "t",
                "--repo",
                str(tmp_path),
                "--max-cost",
                "0.25",
                "--max-duration",
                "45",
                "--budget-action",
                "pause",
            ],
        )
    assert run_res.exit_code == 0, run_res.output
    assert seen_budget == {"max_cost": 0.25, "max_duration": 45.0, "budget_action": "pause"}

    # Test resume --max-tokens raises budget & completes, and cancel command behavior.
    paused = AgentRun(
        task="Paused",
        repo_path=str(tmp_path),
        config=AgentConfig(max_tokens_per_run=50, budget_action="pause"),
        state=RunState.PAUSED,
        iterations=1,
    )
    save_checkpoint(paused, base_revision="HEAD", base_dir=runs_dir)

    cancel_res = runner.invoke(cli_main, ["cancel", paused.id, "--reason", "User aborted"])
    assert cancel_res.exit_code == 0, cancel_res.stderr
    assert load_run(paused.id, base_dir=runs_dir).state == RunState.CANCELLED
    assert load_run(paused.id, base_dir=runs_dir).error == "User aborted"

    # Cancelling an already-cancelled run fails cleanly.
    reject_res = runner.invoke(cli_main, ["cancel", paused.id])
    assert reject_res.exit_code == 1
    assert "cannot cancel" in " ".join(reject_res.stderr.split())

    # Test resume --max-tokens updates run config budget.
    paused2 = AgentRun(
        task="Raise budget",
        repo_path=str(tmp_path),
        config=AgentConfig(max_tokens_per_run=50, budget_action="pause"),
        state=RunState.PAUSED,
        iterations=1,
    )
    save_checkpoint(paused2, base_revision="HEAD", base_dir=runs_dir)

    resumed_tokens: list[int] = []

    async def _fake_exec_ws(agent_run: AgentRun, *_args: object, **_kwargs: object) -> AgentRun:
        resumed_tokens.append(agent_run.config.max_tokens_per_run)
        agent_run.state = RunState.COMPLETED
        return agent_run

    with patch("agent_forge.cli._execute_with_workspace", _fake_exec_ws):
        resume_res = runner.invoke(
            cli_main,
            ["resume", paused2.id, "--max-tokens", "5000", "--output-format", "json"],
        )
    assert resume_res.exit_code == 0, resume_res.stderr
    assert resumed_tokens == [5000]
    assert json.loads(resume_res.stdout)["state"] == "completed"
