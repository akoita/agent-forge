"""Unit tests for durable DevSession persistence and full ContextEngine serialization."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest

from agent_forge.agent import (
    AgentConfig,
    AgentRun,
    ContextEngine,
    ContextPolicy,
    DevSession,
    RunState,
    SessionStore,
    ToolInvocation,
    WorkingSet,
)
from agent_forge.agent.core import react_loop
from agent_forge.llm.base import LLMResponse, Message, Role, TokenUsage, ToolCall
from agent_forge.tools.base import Tool, ToolRegistry, ToolResult

if TYPE_CHECKING:
    from pathlib import Path


class _SimpleEditTool(Tool):
    def __init__(self, target_file: str = "app.py") -> None:
        self._target_file = target_file

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return "Write workspace file"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        }

    async def execute(self, arguments: dict[str, Any], sandbox: Any) -> ToolResult:
        del sandbox
        path = str(arguments.get("path", self._target_file))
        return ToolResult(output=f"wrote {path} " + ("x" * 120))


class _SimpleReadTool(Tool):
    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return "Read workspace file"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"path": {"type": "string"}}}

    @property
    def side_effecting(self) -> bool:
        return False

    async def execute(self, arguments: dict[str, Any], sandbox: Any) -> ToolResult:
        del sandbox
        path = str(arguments.get("path", "app.py"))
        return ToolResult(output=f"contents of {path}:\n" + ("y" * 240))


def _init_sample_repo(repo: Path) -> Path:
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "AGENTS.md").write_text(
        "# Repo Rules\nAlways keep functions small.\n", encoding="utf-8"
    )
    (repo / "app.py").write_text(
        "def greet(name: str) -> str:\n    return name\n", encoding="utf-8"
    )
    return repo


class TestWorkingSetAndContextEngineSerialization:
    """Roundtrip fidelity and skip-rediscovery guarantees for WorkingSet & ContextEngine."""

    def test_working_set_export_and_from_state_roundtrip(self) -> None:
        ws = WorkingSet(max_files=8)
        ws.touch_file("/workspace/src/main.py", action="read", iteration=1)
        ws.touch_file("/workspace/src/main.py", action="write", iteration=2)
        ws.touch_file("/workspace/README.md", action="read", iteration=2)
        ws.loaded_instruction_paths.append("AGENTS.md")
        ws.record_failure(
            iteration=2,
            tool_name="run_shell",
            target="pytest -q",
            summary="1 failed",
        )
        ws.record_verification(
            iteration=3,
            command="pytest -q",
            exit_code=0,
            summary="3 passed",
        )
        ws.evicted_tool_results = 2
        ws.evicted_tokens = 350

        snapshot = ws.export_state()
        restored = WorkingSet.from_state(snapshot)

        assert restored.max_files == 8
        assert restored._seq == ws._seq
        assert restored.active_file_paths() == ws.active_file_paths()
        assert restored.modified_files == ws.modified_files
        assert restored.loaded_instruction_paths == ["AGENTS.md"]
        assert restored.active_failures == ws.active_failures
        assert restored.last_verification == ws.last_verification
        assert restored.evicted_tool_results == 2
        assert restored.evicted_tokens == 350

    def test_context_engine_roundtrip_and_zero_rediscovery(self, tmp_path: Path) -> None:
        repo = _init_sample_repo(tmp_path / "repo")
        policy = ContextPolicy(
            context_window_tokens=1000,
            compaction_trigger_ratio=0.35,
            compaction_target_ratio=0.20,
            keep_recent_turns=1,
            stale_tool_result_max_chars=120,
        )
        engine = ContextEngine(repo, task="Update greet in app.py", policy=policy)
        base_instr, repo_map = engine.prepare_initial_context()
        assert "Always keep functions small." in base_instr.rendered
        assert "app.py" in repo_map.ranked_files

        run = AgentRun(
            task="Update greet in app.py",
            repo_path=str(repo),
            config=AgentConfig(),
            iterations=3,
            messages=[
                Message(role=Role.SYSTEM, content="System prompt"),
                Message(role=Role.USER, content="Update greet in app.py"),
                Message(
                    role=Role.ASSISTANT,
                    content="Reading app.py first",
                    tool_calls=[
                        ToolCall(
                            id="tc1", name="read_file", arguments={"path": "/workspace/app.py"}
                        )
                    ],
                ),
                Message(role=Role.TOOL, content="line1\n" * 220, tool_call_id="tc1"),
                Message(
                    role=Role.ASSISTANT,
                    content="Editing app.py now",
                    tool_calls=[
                        ToolCall(
                            id="tc2", name="write_file", arguments={"path": "/workspace/app.py"}
                        )
                    ],
                ),
                Message(role=Role.TOOL, content="ok", tool_call_id="tc2"),
                Message(role=Role.ASSISTANT, content="Done updating"),
                Message(role=Role.USER, content="Now add docstring"),
            ],
            tool_invocations=[
                ToolInvocation(
                    tool_name="read_file",
                    arguments={"path": "/workspace/app.py"},
                    result=ToolResult(output="ok"),
                    iteration=1,
                    timestamp=datetime.now(UTC),
                    duration_ms=5,
                ),
                ToolInvocation(
                    tool_name="write_file",
                    arguments={"path": "/workspace/app.py"},
                    result=ToolResult(output="ok"),
                    iteration=2,
                    timestamp=datetime.now(UTC),
                    duration_ms=5,
                ),
            ],
        )

        before_view = engine.assemble(run)
        assert before_view.compacted_this_turn is True
        assert engine._compaction_epoch >= 1

        exported = engine.export_state()
        assert exported["version"] == 1

        with (
            patch("agent_forge.agent.context.discover_repo_instructions") as mock_discover,
            patch("agent_forge.agent.context.build_repo_map") as mock_map,
        ):
            restored = ContextEngine.from_state(repo, exported)
            r_instr, r_map = restored.prepare_initial_context()
            after_view = restored.assemble(run)

        mock_discover.assert_not_called()
        mock_map.assert_not_called()
        assert r_instr.rendered == base_instr.rendered
        assert r_map.rendered == repo_map.rendered
        assert restored.working_set.modified_files == engine.working_set.modified_files
        assert restored._compaction_epoch == engine._compaction_epoch
        assert restored._compacted_group_cutoff == engine._compacted_group_cutoff
        assert restored._folded_notes == engine._folded_notes
        assert restored._summary_message_text == engine._summary_message_text
        assert restored._pruned_tool_stubs == engine._pruned_tool_stubs
        assert [(m.role, m.content) for m in after_view.messages] == [
            (m.role, m.content) for m in before_view.messages
        ]

        # Unsupported version rejected
        with pytest.raises(ValueError, match="Unsupported"):
            ContextEngine.from_state(repo, {"version": 99})

    def test_force_compact_folds_turns_even_under_trigger_threshold(self, tmp_path: Path) -> None:
        repo = _init_sample_repo(tmp_path / "repo")
        engine = ContextEngine(
            repo,
            task="Multi-turn session",
            policy=ContextPolicy(context_window_tokens=200_000, keep_recent_turns=1),
        )
        run = AgentRun(
            task="Multi-turn session",
            repo_path=str(repo),
            config=AgentConfig(),
            messages=[
                Message(role=Role.SYSTEM, content="System"),
                Message(role=Role.USER, content="Turn 1"),
                Message(role=Role.ASSISTANT, content="Done turn 1"),
                Message(role=Role.USER, content="Turn 2"),
                Message(role=Role.ASSISTANT, content="Done turn 2"),
                Message(role=Role.USER, content="Turn 3"),
            ],
        )

        # Threshold is huge -> assemble does not compact
        no_compact = engine.assemble(run)
        assert no_compact.compacted_this_turn is False

        # force_compact folds earlier turns deterministically
        compacted = engine.force_compact(run, keep_recent_turns=1)
        assert compacted.compacted_this_turn is True
        assert engine._compacted_group_cutoff == 3
        assert engine._compaction_epoch == 1
        assert "[CONTEXT SUMMARY" in compacted.messages[2].content


class TestDevSessionAndSessionStore:
    """Multi-turn DevSession continuation, atomic SessionStore persistence, and lookups."""

    @pytest.mark.asyncio
    async def test_multi_turn_dev_session_persists_and_resumes_across_restarts(
        self, tmp_path: Path
    ) -> None:
        repo = _init_sample_repo(tmp_path / "project")
        store_dir = tmp_path / "sessions"
        store1 = SessionStore(base_dir=store_dir)

        registry = ToolRegistry()
        registry.register(_SimpleReadTool())
        registry.register(_SimpleEditTool())
        sandbox = AsyncMock()

        policy = ContextPolicy(
            context_window_tokens=900,
            compaction_trigger_ratio=0.25,
            compaction_target_ratio=0.15,
            keep_recent_turns=1,
            stale_tool_result_max_chars=80,
        )
        session = DevSession(repo_path=str(repo), id="sess-m9-001", model="gemini-test")
        engine1 = session.build_context_engine(task="Inspect app.py", policy=policy)

        llm_turn1 = AsyncMock()
        llm_turn1.complete.side_effect = [
            LLMResponse(
                content="Reading file",
                tool_calls=[
                    ToolCall(
                        id="t1_call", name="read_file", arguments={"path": "/workspace/app.py"}
                    )
                ],
                usage=TokenUsage(100, 20, 120),
                finish_reason="tool_calls",
                reasoning_blocks=[{"type": "thinking", "text": "inspecting"}],
                response_id="resp-1",
            ),
            LLMResponse(
                content="Looked at app.py",
                tool_calls=[],
                usage=TokenUsage(120, 15, 135),
                finish_reason="stop",
            ),
        ]

        run1 = session.prepare_next_run("Inspect app.py", config=AgentConfig(model="gemini-test"))
        await react_loop(run1, llm_turn1, registry, sandbox, context_engine=engine1)
        assert run1.state == RunState.COMPLETED
        turn1 = session.record_turn(prompt="Inspect app.py", run=run1, context_engine=engine1)
        assert turn1.turn_index == 0
        assert turn1.response == "Looked at app.py"
        store1.save(session)

        # Simulate full process restart using a new SessionStore instance
        store2 = SessionStore(base_dir=store_dir)
        loaded = store2.load("sess-m9-001")
        assert loaded.id == "sess-m9-001"
        assert len(loaded.turns) == 1
        assert loaded.total_tokens.total_tokens == 255
        assert loaded.messages[2].response_id == "resp-1"
        assert loaded.messages[2].reasoning_blocks == [{"type": "thinking", "text": "inspecting"}]

        engine2 = loaded.build_context_engine()
        llm_turn2 = AsyncMock()
        llm_turn2.complete.side_effect = [
            LLMResponse(
                content="Editing now",
                tool_calls=[
                    ToolCall(
                        id="t2_call",
                        name="write_file",
                        arguments={"path": "/workspace/app.py", "content": "updated"},
                    )
                ],
                usage=TokenUsage(90, 20, 110),
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content="Updated app.py",
                tool_calls=[],
                usage=TokenUsage(95, 15, 110),
                finish_reason="stop",
            ),
        ]

        run2 = loaded.prepare_next_run(
            "Modify app.py",
            config=AgentConfig(model="gemini-test"),
        )
        assert run2.messages[-1].role == Role.USER
        assert run2.messages[-1].content == "Modify app.py"

        with (
            patch("agent_forge.agent.context.discover_repo_instructions") as mock_discover,
            patch("agent_forge.agent.context.build_repo_map") as mock_map,
        ):
            await react_loop(run2, llm_turn2, registry, sandbox, context_engine=engine2)

        mock_discover.assert_not_called()
        mock_map.assert_not_called()
        turn2 = loaded.record_turn(prompt="Modify app.py", run=run2, context_engine=engine2)
        assert turn2.turn_index == 1
        assert turn2.response == "Updated app.py"
        store2.save(loaded)

        # Verify restart after second turn preserves working set, invocations, and turns
        reloaded = SessionStore(base_dir=store_dir).load("sess-m9-001")
        assert len(reloaded.turns) == 2
        assert len(reloaded.tool_invocations) == 2
        assert reloaded.total_tokens.total_tokens == 475
        final_engine = reloaded.build_context_engine()
        assert "app.py" in final_engine.working_set.modified_files

    def test_session_store_list_latest_delete_and_validation(self, tmp_path: Path) -> None:
        store = SessionStore(base_dir=tmp_path / "sessions")
        repo_a = _init_sample_repo(tmp_path / "repo_a")
        repo_b = _init_sample_repo(tmp_path / "repo_b")

        t0 = datetime(2026, 11, 24, 1, 0, tzinfo=UTC)
        s1 = DevSession(
            repo_path=str(repo_a),
            id="s1",
            title="First",
            created_at=t0,
            updated_at=t0,
        )
        s2 = DevSession(
            repo_path=str(repo_a),
            id="s2",
            title="Second",
            created_at=t0,
            updated_at=t0 + timedelta(minutes=5),
        )
        s3 = DevSession(
            repo_path=str(repo_b),
            id="s3",
            title="Other repo",
            created_at=t0,
            updated_at=t0 + timedelta(minutes=10),
        )
        for s in (s1, s2, s3):
            store.save(s)

        assert store.exists("s1") is True
        all_list = store.list_sessions()
        assert [item.id for item in all_list] == ["s3", "s2", "s1"]

        repo_a_list = store.list_sessions(repo_path=repo_a)
        assert [item.id for item in repo_a_list] == ["s2", "s1"]

        latest_a = store.latest_for_repo(repo_a)
        assert latest_a is not None and latest_a.id == "s2"

        assert store.delete("s2") is True
        assert store.exists("s2") is False
        assert store.delete("s2") is False

        with pytest.raises(FileNotFoundError):
            store.load("does-not-exist")

        for bad_id in ("", "../escape", "a/b", ".hidden", "bad\\id"):
            with pytest.raises(ValueError, match="Invalid session ID"):
                store.load(bad_id)
