"""Unit tests for the run state machine and persistence."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from agent_forge.agent.models import AgentConfig, AgentRun, RunState, ToolInvocation
from agent_forge.agent.persistence import load_run, save_run
from agent_forge.agent.state import (
    VALID_TRANSITIONS,
    InvalidStateTransitionError,
    transition,
)
from agent_forge.llm.base import Message, Role, TokenUsage, ToolCall
from agent_forge.tools.base import ToolResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_run(**overrides: object) -> AgentRun:
    kwargs: dict[str, object] = {
        "task": "Fix the login bug",
        "repo_path": "/tmp/test-repo",
        "config": AgentConfig(),
    }
    kwargs.update(overrides)
    return AgentRun(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# State Machine
# ---------------------------------------------------------------------------


class TestValidTransitions:
    """All valid transitions should succeed."""

    def test_pending_to_running(self) -> None:
        run = _make_run()
        assert run.state == RunState.PENDING
        transition(run, RunState.RUNNING)
        assert run.state == RunState.RUNNING

    def test_running_to_completed(self) -> None:
        run = _make_run()
        transition(run, RunState.RUNNING)
        transition(run, RunState.COMPLETED)
        assert run.state == RunState.COMPLETED

    def test_running_to_failed(self) -> None:
        run = _make_run()
        transition(run, RunState.RUNNING)
        transition(run, RunState.FAILED)
        assert run.state == RunState.FAILED

    def test_running_to_timeout(self) -> None:
        run = _make_run()
        transition(run, RunState.RUNNING)
        transition(run, RunState.TIMEOUT)
        assert run.state == RunState.TIMEOUT

    def test_running_to_cancelled(self) -> None:
        run = _make_run()
        transition(run, RunState.RUNNING)
        transition(run, RunState.CANCELLED)
        assert run.state == RunState.CANCELLED


class TestInvalidTransitions:
    """Invalid transitions should raise InvalidStateTransitionError."""

    def test_pending_to_completed(self) -> None:
        run = _make_run()
        with pytest.raises(InvalidStateTransitionError, match="pending → completed"):
            transition(run, RunState.COMPLETED)

    def test_pending_to_failed(self) -> None:
        run = _make_run()
        with pytest.raises(InvalidStateTransitionError):
            transition(run, RunState.FAILED)

    def test_completed_to_running(self) -> None:
        run = _make_run()
        transition(run, RunState.RUNNING)
        transition(run, RunState.COMPLETED)
        with pytest.raises(InvalidStateTransitionError, match="completed → running"):
            transition(run, RunState.RUNNING)

    def test_failed_to_running(self) -> None:
        run = _make_run()
        transition(run, RunState.RUNNING)
        transition(run, RunState.FAILED)
        with pytest.raises(InvalidStateTransitionError):
            transition(run, RunState.RUNNING)

    def test_timeout_to_completed(self) -> None:
        run = _make_run()
        transition(run, RunState.RUNNING)
        transition(run, RunState.TIMEOUT)
        with pytest.raises(InvalidStateTransitionError):
            transition(run, RunState.COMPLETED)

    def test_cancelled_is_terminal(self) -> None:
        run = _make_run()
        transition(run, RunState.RUNNING)
        transition(run, RunState.CANCELLED)
        with pytest.raises(InvalidStateTransitionError):
            transition(run, RunState.RUNNING)


class TestTransitionMap:
    """The VALID_TRANSITIONS map covers all RunState values."""

    def test_all_states_covered(self) -> None:
        for state in RunState:
            assert state in VALID_TRANSITIONS

    def test_terminal_states_have_no_transitions(self) -> None:
        for state in [RunState.COMPLETED, RunState.FAILED, RunState.TIMEOUT, RunState.CANCELLED]:
            assert VALID_TRANSITIONS[state] == set()


class TestInvalidStateTransitionError:
    """Error carries current and target state."""

    def test_attributes(self) -> None:
        err = InvalidStateTransitionError(RunState.COMPLETED, RunState.RUNNING)
        assert err.current == RunState.COMPLETED
        assert err.target == RunState.RUNNING
        assert "completed → running" in str(err)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class TestSaveAndLoadRun:
    """Round-trip: save_run → load_run produces equivalent AgentRun."""

    def test_round_trip(self, tmp_path: Path) -> None:
        run = _make_run()
        run.state = RunState.COMPLETED  # direct set for test setup
        run.iterations = 3
        run.total_tokens = TokenUsage(100, 50, 150)
        run.completed_at = datetime.now(UTC)
        run.error = None
        run.messages = [
            Message(role=Role.SYSTEM, content="You are an agent."),
            Message(role=Role.USER, content="Fix the bug"),
            Message(
                role=Role.ASSISTANT,
                content="Reading file.",
                tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "x.py"})],
            ),
            Message(role=Role.TOOL, content="file content", tool_call_id="c1"),
        ]
        run.tool_invocations = [
            ToolInvocation(
                tool_name="read_file",
                arguments={"path": "x.py"},
                result=ToolResult(output="file content", exit_code=0),
                iteration=1,
                timestamp=datetime.now(UTC),
                duration_ms=42,
            ),
        ]

        run_dir = save_run(run, base_dir=tmp_path)
        assert (run_dir / "run.json").exists()
        assert (run_dir / "messages.jsonl").exists()
        assert (run_dir / "events.jsonl").exists()

        loaded = load_run(run.id, base_dir=tmp_path)
        assert loaded.id == run.id
        assert loaded.task == run.task
        assert loaded.state == RunState.COMPLETED
        assert loaded.iterations == 3
        assert loaded.total_tokens.total_tokens == 150
        assert len(loaded.messages) == 4
        assert loaded.messages[0].role == Role.SYSTEM
        assert loaded.messages[2].tool_calls is not None
        assert loaded.messages[2].tool_calls[0].name == "read_file"
        assert len(loaded.tool_invocations) == 1
        assert loaded.tool_invocations[0].tool_name == "read_file"
        assert loaded.tool_invocations[0].duration_ms == 42

    def test_load_nonexistent_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="Run not found"):
            load_run("nonexistent-id", base_dir=tmp_path)


class TestPersistenceFiles:
    """File content matches expected format."""

    def test_run_json_structure(self, tmp_path: Path) -> None:
        run = _make_run()
        run.state = RunState.COMPLETED
        save_run(run, base_dir=tmp_path)

        meta = json.loads((tmp_path / run.id / "run.json").read_text())
        assert meta["id"] == run.id
        assert meta["state"] == "completed"
        assert "total_tokens" in meta
        assert "config" in meta

    def test_messages_jsonl_format(self, tmp_path: Path) -> None:
        run = _make_run()
        run.state = RunState.COMPLETED
        run.messages = [
            Message(role=Role.USER, content="hello"),
            Message(role=Role.ASSISTANT, content="hi"),
        ]
        save_run(run, base_dir=tmp_path)

        lines = (tmp_path / run.id / "messages.jsonl").read_text().strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["role"] == "user"
        assert json.loads(lines[1])["content"] == "hi"

    def test_events_jsonl_format(self, tmp_path: Path) -> None:
        run = _make_run()
        run.state = RunState.COMPLETED
        run.tool_invocations = [
            ToolInvocation(
                tool_name="run_shell",
                arguments={"command": "ls"},
                result=ToolResult(output="file.py", exit_code=0),
                iteration=1,
                timestamp=datetime.now(UTC),
                duration_ms=10,
            ),
        ]
        save_run(run, base_dir=tmp_path)

        lines = (tmp_path / run.id / "events.jsonl").read_text().strip().splitlines()
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["tool_name"] == "run_shell"
        assert data["result"]["exit_code"] == 0

    def test_empty_messages_and_events(self, tmp_path: Path) -> None:
        run = _make_run()
        run.state = RunState.COMPLETED
        save_run(run, base_dir=tmp_path)

        assert (tmp_path / run.id / "messages.jsonl").read_text() == ""
        assert (tmp_path / run.id / "events.jsonl").read_text() == ""
