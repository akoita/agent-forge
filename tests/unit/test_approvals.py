"""Unit tests for human-in-the-loop approval gates (#43)."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner

from agent_forge.agent.approvals import (
    ApprovalGate,
    ApprovalRequest,
    classify_destructive_tool_call,
)
from agent_forge.agent.core import react_loop
from agent_forge.agent.models import AgentConfig, AgentRun, RunState
from agent_forge.agent.persistence import save_run
from agent_forge.cli import main as cli_main
from agent_forge.config import load_config
from agent_forge.llm.base import LLMResponse, TokenUsage, ToolCall
from agent_forge.orchestration.events import Event, EventBus, EventType
from agent_forge.tools.base import ToolDefinition, ToolResult

if TYPE_CHECKING:
    from pathlib import Path


class TestDestructiveClassifier:
    """Verify destructive vs safe tool call classification."""

    @pytest.mark.parametrize(
        ("tool_name", "arguments", "expected_category"),
        [
            (
                "apply_patch",
                {"operations": [{"path": "old.py", "action": "delete"}]},
                "file_deletion",
            ),
            ("run_shell", {"command": "rm -f tmp.txt"}, "file_deletion"),
            ("run_shell", {"command": "git push origin feat --force"}, "git_force_push"),
            ("run_shell", {"command": "git push -f origin feat"}, "git_force_push"),
            ("run_shell", {"command": "git push --force-with-lease"}, "git_force_push"),
            ("run_shell", {"command": "git reset --hard HEAD~1"}, "git_reset_hard"),
            ("run_shell", {"command": "git clean -fd"}, "git_clean"),
            ("run_shell", {"command": "git branch -D old-branch"}, "git_branch_delete"),
            ("run_shell", {"command": "git checkout -- app.py"}, "git_discard_changes"),
            ("run_shell", {"command": "git restore app.py"}, "git_discard_changes"),
        ],
    )
    def test_flags_destructive_operations(
        self,
        tool_name: str,
        arguments: dict[str, object],
        expected_category: str,
    ) -> None:
        request = classify_destructive_tool_call(tool_name, arguments)
        assert request is not None
        assert request.category == expected_category
        assert request.tool_name == tool_name

    @pytest.mark.parametrize(
        ("tool_name", "arguments"),
        [
            ("run_shell", {"command": "pytest -q"}),
            ("run_shell", {"command": "git status"}),
            ("run_shell", {"command": "git push origin feat/43"}),
            ("apply_patch", {"operations": [{"path": "app.py", "action": "modify", "hunks": []}]}),
            (
                "apply_patch",
                {"operations": [{"path": "new.py", "action": "create", "content": ""}]},
            ),
            ("read_file", {"path": "README.md"}),
        ],
    )
    def test_ignores_non_destructive_operations(
        self,
        tool_name: str,
        arguments: dict[str, object],
    ) -> None:
        assert classify_destructive_tool_call(tool_name, arguments) is None


class TestApprovalGatePolicies:
    """Verify ApprovalGate modes and fail-closed prompt behavior."""

    @pytest.mark.asyncio
    async def test_auto_mode_approves_destructive_operation(self) -> None:
        gate = ApprovalGate(mode="auto")
        req, decision = await gate.evaluate("run_shell", {"command": "rm -rf build"})
        assert req is not None
        assert decision is not None
        assert decision.approved is True
        assert decision.decided_by == "policy:auto"

    @pytest.mark.asyncio
    async def test_deny_mode_blocks_destructive_operation(self) -> None:
        gate = ApprovalGate(mode="deny")
        req, decision = await gate.evaluate("run_shell", {"command": "git push -f"})
        assert req is not None
        assert decision is not None
        assert decision.approved is False
        assert decision.decided_by == "policy:deny"

    @pytest.mark.asyncio
    async def test_prompt_mode_fails_closed_without_callback(self) -> None:
        gate = ApprovalGate(mode="prompt")
        req, decision = await gate.evaluate("run_shell", {"command": "rm foo.txt"})
        assert req is not None
        assert decision is not None
        assert decision.approved is False
        assert decision.decided_by == "policy:no_approver"

    @pytest.mark.asyncio
    async def test_prompt_mode_invokes_callback_for_decision(self) -> None:
        seen: list[ApprovalRequest] = []

        async def _approve(request: ApprovalRequest) -> bool:
            seen.append(request)
            return True

        gate = ApprovalGate(mode="prompt", callback=_approve)
        req, decision = await gate.evaluate("run_shell", {"command": "git reset --hard"})
        assert req is not None
        assert decision is not None
        assert decision.approved is True
        assert decision.decided_by == "human"
        assert len(seen) == 1

        async def _reject(_request: ApprovalRequest) -> bool:
            return False

        deny_gate = ApprovalGate(mode="prompt", callback=_reject)
        _, denied = await deny_gate.evaluate("run_shell", {"command": "git reset --hard"})
        assert denied is not None
        assert denied.approved is False
        assert denied.decided_by == "human"

    def test_invalid_mode_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid approval mode"):
            ApprovalGate(mode="unknown")


def _build_shell_registry(tool_mock: AsyncMock) -> AsyncMock:
    registry = AsyncMock()
    registry.list_definitions = lambda: [
        ToolDefinition(
            name="run_shell",
            description="Run shell command",
            parameters={"type": "object", "properties": {"command": {"type": "string"}}},
        )
    ]
    registry.get = lambda _name: tool_mock
    return registry


class TestApprovalLoopIntegration:
    """Verify approval gates in react_loop emit events and control execution."""

    @pytest.mark.asyncio
    async def test_denied_destructive_tool_does_not_execute_and_emits_events(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "agent_forge.agent.core.save_run", lambda run: save_run(run, base_dir=tmp_path)
        )
        monkeypatch.setattr(
            "agent_forge.observability.cost.save_summary",
            lambda *_args: tmp_path / "summary.json",
        )

        shell_tool = AsyncMock()
        shell_tool.execute.return_value = ToolResult(output="ok", exit_code=0)
        registry = _build_shell_registry(shell_tool)
        sandbox = AsyncMock()
        sandbox.is_alive.return_value = True

        bus = EventBus()
        captured: list[Event] = []

        async def _record(event: Event) -> None:
            captured.append(event)

        await bus.subscribe(EventType.APPROVAL_REQUESTED, _record)
        await bus.subscribe(EventType.APPROVAL_DECIDED, _record)

        llm = AsyncMock()
        llm.complete.side_effect = [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(id="c1", name="run_shell", arguments={"command": "rm -f app.py"})
                ],
                usage=TokenUsage(10, 10, 20),
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content="Understood; skipped destructive removal.",
                tool_calls=[],
                usage=TokenUsage(20, 10, 30),
                finish_reason="stop",
            ),
        ]

        run = AgentRun(
            task="Clean up",
            repo_path=str(tmp_path),
            config=AgentConfig(max_iterations=4, approval_mode="deny"),
        )
        completed = await react_loop(run, llm, registry, sandbox, event_bus=bus)

        assert completed.state == RunState.COMPLETED
        shell_tool.execute.assert_not_called()
        assert len(captured) == 2
        assert captured[0].type == EventType.APPROVAL_REQUESTED
        assert captured[0].data["category"] == "file_deletion"
        assert captured[1].type == EventType.APPROVAL_DECIDED
        assert captured[1].data["approved"] is False
        assert completed.tool_invocations[0].result.metadata["approval_denied"] is True

    @pytest.mark.asyncio
    async def test_approved_destructive_tool_executes_and_emits_approval(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "agent_forge.agent.core.save_run", lambda run: save_run(run, base_dir=tmp_path)
        )
        monkeypatch.setattr(
            "agent_forge.observability.cost.save_summary",
            lambda *_args: tmp_path / "summary.json",
        )

        shell_tool = AsyncMock()
        shell_tool.execute.return_value = ToolResult(output="deleted", exit_code=0)
        registry = _build_shell_registry(shell_tool)
        sandbox = AsyncMock()
        sandbox.is_alive.return_value = True

        bus = EventBus()
        decided: list[Event] = []

        async def _on_decided(event: Event) -> None:
            decided.append(event)

        await bus.subscribe(EventType.APPROVAL_DECIDED, _on_decided)

        async def _allow(_req: ApprovalRequest) -> bool:
            return True

        llm = AsyncMock()
        llm.complete.side_effect = [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(id="c1", name="run_shell", arguments={"command": "git branch -D tmp"})
                ],
                usage=TokenUsage(10, 10, 20),
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content="Branch deleted.",
                tool_calls=[],
                usage=TokenUsage(10, 10, 20),
                finish_reason="stop",
            ),
        ]

        run = AgentRun(
            task="Remove temp branch",
            repo_path=str(tmp_path),
            config=AgentConfig(max_iterations=4),
        )
        completed = await react_loop(
            run,
            llm,
            registry,
            sandbox,
            event_bus=bus,
            approval_gate=ApprovalGate(mode="prompt", callback=_allow),
        )

        assert completed.state == RunState.COMPLETED
        shell_tool.execute.assert_awaited_once()
        assert len(decided) == 1
        assert decided[0].data["approved"] is True
        assert decided[0].data["decided_by"] == "human"


def test_config_env_and_cli_approval_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_FORGE_AGENT_APPROVAL_MODE", "deny")
    cfg = load_config(
        project_path=tmp_path / "agent-forge.toml",
        user_path=tmp_path / "user.toml",
    )
    assert cfg.agent.approval_mode == "deny"

    captured_mode: list[str] = []

    async def _fake_run_agent(*_args: object, **_kwargs: object) -> AgentRun:
        cfg_arg = _args[2]
        captured_mode.append(cfg_arg.agent.approval_mode)  # type: ignore[attr-defined]
        return AgentRun(
            task="t",
            repo_path=str(tmp_path),
            config=AgentConfig(),
            state=RunState.COMPLETED,
        )

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr("agent_forge.cli._run_agent", _fake_run_agent)
    runner = CliRunner()
    res = runner.invoke(
        cli_main,
        ["run", "--task", "t", "--repo", str(tmp_path), "--approval-mode", "prompt"],
    )
    assert res.exit_code == 0, res.output
    assert captured_mode == ["prompt"]
