"""Unit tests for M2 Context Engine: instructions, repo map, working set, compaction, caching."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from agent_forge.agent.context import (
    ContextEngine,
    ContextPolicy,
    WorkingSet,
    estimate_messages_tokens,
)
from agent_forge.agent.core import react_loop
from agent_forge.agent.instructions import discover_repo_instructions
from agent_forge.agent.journal import RunJournal, replay_journal
from agent_forge.agent.models import AgentConfig, AgentRun, RunState
from agent_forge.agent.persistence import load_run
from agent_forge.agent.prompts import build_system_prompt
from agent_forge.agent.repo_map import build_repo_map
from agent_forge.config import load_config
from agent_forge.eval.deterministic import SubprocessEvalSandbox
from agent_forge.llm.base import LLMResponse, Message, Role, TokenUsage, ToolCall
from agent_forge.tools import ToolRegistry, WriteFileTool, create_default_registry

if TYPE_CHECKING:
    from pathlib import Path

    from agent_forge.llm.base import LLMConfig, ToolDefinition


class _RecordingMockLLM:
    """Scripted mock LLM recording assembled messages and cache configs per call."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = responses
        self._cursor = 0
        self.recorded_messages: list[list[Message]] = []
        self.recorded_configs: list[LLMConfig | None] = []

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: LLMConfig | None = None,
    ) -> LLMResponse:
        del tools
        self.recorded_messages.append(list(messages))
        self.recorded_configs.append(config)
        resp = self._responses[min(self._cursor, len(self._responses) - 1)]
        self._cursor += 1
        return resp

    async def close(self) -> None:
        return None


class TestRepoInstructionDiscovery:
    """Tests for hierarchical repo instruction discovery and symlink deduplication."""

    def test_parent_root_child_precedence_and_git_boundary(self, tmp_path: Path) -> None:
        outer = tmp_path / "outer"
        outer.mkdir()
        # Above .git boundary — MUST NOT be loaded.
        (outer / "AGENTS.md").write_text("OUTER OUTSIDE GIT", encoding="utf-8")

        git_root = outer / "monorepo"
        git_root.mkdir()
        (git_root / ".git").mkdir()
        (git_root / "AGENTS.md").write_text("MONOREPO PARENT RULE", encoding="utf-8")

        pkg_root = git_root / "packages" / "core"
        (pkg_root / "src" / "sub").mkdir(parents=True)
        (pkg_root / "AGENTS.md").write_text("PACKAGE ROOT RULE", encoding="utf-8")
        (pkg_root / "src" / "AGENTS.md").write_text("SRC CHILD RULE", encoding="utf-8")
        (pkg_root / "src" / "sub" / "AGENTS.md").write_text("SUB CHILD RULE", encoding="utf-8")
        target = pkg_root / "src" / "sub" / "mod.py"
        target.write_text("x = 1\n", encoding="utf-8")

        discovered = discover_repo_instructions(
            pkg_root,
            task="Inspect src/sub/mod.py and update tests",
        )
        scopes = [(item.rel_path, item.scope) for item in discovered.files]
        assert scopes == [
            ("../../AGENTS.md", "parent"),
            ("AGENTS.md", "root"),
            ("src/AGENTS.md", "child"),
            ("src/sub/AGENTS.md", "child"),
        ]
        assert "OUTER OUTSIDE GIT" not in discovered.rendered
        assert discovered.rendered.index("MONOREPO PARENT RULE") < discovered.rendered.index(
            "PACKAGE ROOT RULE"
        )
        assert discovered.rendered.index("PACKAGE ROOT RULE") < discovered.rendered.index(
            "SRC CHILD RULE"
        )
        assert discovered.rendered.index("SRC CHILD RULE") < discovered.rendered.index(
            "SUB CHILD RULE"
        )

    def test_symlink_deduplication_and_budget_truncation(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        agents_md = repo / "AGENTS.md"
        agents_md.write_text("Root rule:\n" + ("Keep changes minimal.\n" * 60), encoding="utf-8")
        claude_md = repo / "CLAUDE.md"
        os.symlink("AGENTS.md", claude_md)
        extra_dir = repo / ".agent-forge"
        extra_dir.mkdir()
        (extra_dir / "instructions.md").write_text("Custom harness notes.", encoding="utf-8")

        discovered = discover_repo_instructions(repo, max_tokens=180)
        rel_paths = [item.rel_path for item in discovered.files]
        assert "AGENTS.md" in rel_paths
        assert "CLAUDE.md" not in rel_paths  # deduplicated symlink target
        assert discovered.truncated is True
        assert "truncated to fit instruction budget" in discovered.rendered


class TestRepoMap:
    """Tests for AST/regex symbol extraction and dependency-aware PageRank mapping."""

    def test_python_ast_and_pagerank_prioritizes_core_and_focus(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        (repo / "src").mkdir(parents=True)
        (repo / "tests").mkdir()
        (repo / "node_modules").mkdir()
        (repo / ".gitignore").write_text("ignored_mod.py\n", encoding="utf-8")

        (repo / "src" / "engine.py").write_text(
            "MAX_RETRIES = 3\n\n"
            "class ContextRouter:\n"
            "    def route(self, name: str) -> int:\n"
            "        return len(name)\n\n"
            "def run_engine(cfg: dict[str, str]) -> bool:\n"
            "    return bool(cfg)\n",
            encoding="utf-8",
        )
        (repo / "src" / "caller_a.py").write_text(
            "from src.engine import ContextRouter, run_engine\n"
            "def go() -> bool:\n"
            "    ContextRouter().route('a')\n"
            "    return run_engine({})\n",
            encoding="utf-8",
        )
        (repo / "src" / "caller_b.py").write_text(
            "from src.engine import run_engine\n"
            "def cli_entry() -> bool:\n"
            "    return run_engine({'x': 'y'})\n",
            encoding="utf-8",
        )
        (repo / "src" / "isolated_leaf.py").write_text(
            "def unused_helper() -> None:\n    return None\n",
            encoding="utf-8",
        )
        (repo / "tests" / "test_engine.py").write_text(
            "from src.engine import run_engine\n"
            "def test_ok() -> None:\n    assert run_engine({})\n",
            encoding="utf-8",
        )
        (repo / "ignored_mod.py").write_text("def should_skip() -> None: pass\n", encoding="utf-8")
        (repo / "node_modules" / "dep.py").write_text(
            "def skip_nm() -> None: pass\n", encoding="utf-8"
        )

        # Broken syntax file should still be handled gracefully via regex fallback
        (repo / "src" / "broken.py").write_text("def broken_fn(:\n    pass\n", encoding="utf-8")

        rmap = build_repo_map(repo)
        assert rmap.total_files_scanned == 6
        assert "ignored_mod.py" not in rmap.rendered
        assert "node_modules" not in rmap.rendered
        assert rmap.ranked_files[0] == "src/engine.py"
        assert "class ContextRouter" in rmap.rendered
        assert "def route(name: str) -> int" in rmap.rendered
        assert "MAX_RETRIES" in rmap.rendered

        # Task/focus personalization boosts targeted file
        focused = build_repo_map(
            repo,
            task="Update unused_helper",
            focus_files=["/workspace/src/isolated_leaf.py"],
        )
        assert focused.ranked_files[0] == "src/isolated_leaf.py"

        # Tight token budget enforces truncation
        tight = build_repo_map(repo, max_tokens=45)
        assert tight.truncated is True
        assert len(tight.ranked_files) < 6

    def test_multilanguage_regex_extraction(self, tmp_path: Path) -> None:
        repo = tmp_path / "poly"
        repo.mkdir()
        (repo / "api.ts").write_text(
            "export interface HealthReply { ok: boolean }\n"
            "export async function getHealth(id: string) { return { ok: true }; }\n",
            encoding="utf-8",
        )
        (repo / "worker.go").write_text(
            'package main\nimport "fmt"\ntype Job struct{}\n'
            "func RunJob(id string) { fmt.Println(id) }\n",
            encoding="utf-8",
        )
        (repo / "lib.rs").write_text(
            "pub struct Engine {}\npub fn tick_once(step: u32) -> u32 { step }\n",
            encoding="utf-8",
        )

        rmap = build_repo_map(repo)
        assert rmap.total_files_scanned == 3
        assert "type HealthReply" in rmap.rendered
        assert "function getHealth(id: string)" in rmap.rendered
        assert "type Job" in rmap.rendered
        assert "function RunJob(id string)" in rmap.rendered
        assert "type Engine" in rmap.rendered
        assert "function tick_once(step: u32)" in rmap.rendered


class TestWorkingSetAndCompaction:
    """Tests for working-set eviction, two-stage compaction, atomic groups, and cache stability."""

    def test_working_set_pins_modified_files_and_tracks_failures(self) -> None:
        ws = WorkingSet(max_files=3)
        ws.touch_file("/workspace/a.py", action="write", iteration=1)
        ws.touch_file("/workspace/b.py", action="read", iteration=2)
        ws.touch_file("/workspace/c.py", action="read", iteration=3)
        ws.touch_file("/workspace/d.py", action="read", iteration=4)

        active = ws.active_file_paths()
        assert "a.py" in active  # pinned modified file never evicted
        assert "b.py" not in active  # oldest unpinned file evicted
        assert len(active) == 3

        ws.record_failure(iteration=4, tool_name="run_shell", target="pytest", summary="1 failed")
        ws.record_verification(iteration=4, command="pytest", exit_code=1, summary="1 failed")
        assert len(ws.active_failures) == 1
        ws.clear_failure_for("pytest")
        assert ws.active_failures == []

    def test_two_stage_compaction_preserves_tool_pairing_and_prefix_stability(
        self, tmp_path: Path
    ) -> None:
        # Stage 1: superseded read_file + oversized tool outputs are pruned first.
        stage1_policy = ContextPolicy(
            context_window_tokens=1_400,
            compaction_trigger_ratio=0.70,  # ~980 tokens
            compaction_target_ratio=0.45,  # ~630 tokens
            keep_recent_turns=1,
            stale_tool_result_max_chars=180,
        )
        stage1_engine = ContextEngine(
            tmp_path, task="Fix module bug and pass tests", policy=stage1_policy
        )
        stage1_run = AgentRun(
            task="Fix module bug and pass tests",
            repo_path=str(tmp_path),
            config=AgentConfig(),
        )
        stage1_run.messages = [
            Message(role=Role.SYSTEM, content="System instructions for repo."),
            Message(role=Role.USER, content=stage1_run.task),
            Message(
                role=Role.ASSISTANT,
                content="",
                tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "src/a.py"})],
            ),
            Message(role=Role.TOOL, tool_call_id="c1", content="OLD_LINE\n" * 160),
            Message(
                role=Role.ASSISTANT,
                content="Writing initial patch to src/a.py",
                tool_calls=[
                    ToolCall(
                        id="c2",
                        name="write_file",
                        arguments={"path": "src/a.py", "content": "x=1"},
                    )
                ],
            ),
            Message(role=Role.TOOL, tool_call_id="c2", content="Wrote src/a.py"),
            Message(
                role=Role.ASSISTANT,
                content="",
                tool_calls=[ToolCall(id="c3", name="run_shell", arguments={"command": "pytest"})],
            ),
            Message(
                role=Role.TOOL,
                tool_call_id="c3",
                content=(
                    "STDOUT " + ("detail line\n" * 180) + "\n[ERROR] AssertionError: expected 200"
                ),
            ),
            Message(
                role=Role.ASSISTANT,
                content="",
                tool_calls=[ToolCall(id="c4", name="read_file", arguments={"path": "src/a.py"})],
            ),
            Message(role=Role.TOOL, tool_call_id="c4", content="def updated(): return 200\n"),
        ]
        stage1_assembly = stage1_engine.assemble(stage1_run)
        assert stage1_assembly.compacted_this_turn is True
        assert stage1_assembly.compaction_event is not None
        assert stage1_assembly.compaction_event.pruned_tool_results >= 2
        c1_msg = next(m for m in stage1_assembly.messages if m.tool_call_id == "c1")
        c3_msg = next(m for m in stage1_assembly.messages if m.tool_call_id == "c3")
        c4_msg = next(m for m in stage1_assembly.messages if m.tool_call_id == "c4")
        assert c1_msg.content.startswith("[Compacted tool output: read_file('src/a.py') superseded")
        assert "AssertionError: expected 200" in c3_msg.content
        assert c4_msg.content == "def updated(): return 200\n"

        # Stage 2: when Stage 1 alone remains above target_tokens, fold older atomic groups.
        stage2_policy = ContextPolicy(
            context_window_tokens=500,
            compaction_trigger_ratio=0.70,  # ~350 tokens
            compaction_target_ratio=0.45,  # ~225 tokens
            keep_recent_turns=1,
            stale_tool_result_max_chars=180,
        )
        engine = ContextEngine(tmp_path, task="Fix module bug and pass tests", policy=stage2_policy)
        run = AgentRun(
            task="Fix module bug and pass tests",
            repo_path=str(tmp_path),
            config=AgentConfig(),
        )
        analysis = "Investigating root cause in module state transitions. " * 12
        run.messages = [
            Message(role=Role.SYSTEM, content="System instructions for repo."),
            Message(role=Role.USER, content=run.task),
            Message(role=Role.ASSISTANT, content=f"Turn 1: {analysis}", tool_calls=[]),
            Message(role=Role.ASSISTANT, content=f"Turn 2: {analysis}", tool_calls=[]),
            Message(
                role=Role.ASSISTANT,
                content=f"Turn 3: {analysis}",
                tool_calls=[ToolCall(id="c3", name="run_shell", arguments={"command": "pytest"})],
            ),
            Message(role=Role.TOOL, tool_call_id="c3", content="FAILED tests/test_a.py"),
            Message(
                role=Role.ASSISTANT,
                content="Reading latest file contents.",
                tool_calls=[ToolCall(id="c4", name="read_file", arguments={"path": "src/a.py"})],
            ),
            Message(role=Role.TOOL, tool_call_id="c4", content="def updated(): return 200\n"),
        ]
        engine.working_set.touch_file("src/a.py", action="write", iteration=2)
        engine.working_set.record_failure(
            iteration=3, tool_name="run_shell", target="pytest", summary="AssertionError"
        )
        engine.working_set.record_verification(
            iteration=3, command="pytest", exit_code=1, summary="AssertionError"
        )

        raw_msg_count = len(run.messages)
        assembly1 = engine.assemble(run)
        assert len(run.messages) == raw_msg_count  # never mutates run.messages in place
        assert assembly1.compacted_this_turn is True
        assert assembly1.compaction_event is not None
        assert assembly1.compaction_event.summarized_turns >= 1
        assert assembly1.assembled_prompt_tokens < assembly1.raw_prompt_tokens

        # Summary must preserve Goal, Working Set, Failures, Verification State
        summary_msgs = [m for m in assembly1.messages if m.content.startswith("[CONTEXT SUMMARY")]
        assert len(summary_msgs) == 1
        summary_text = summary_msgs[0].content
        assert "Fix module bug" in summary_text
        assert "src/a.py" in summary_text
        assert "AssertionError" in summary_text
        assert "pytest" in summary_text

        # Every TOOL message must stay paired with its preceding ASSISTANT tool_calls group
        pending_ids: set[str] = set()
        for msg in assembly1.messages:
            if msg.role == Role.ASSISTANT and msg.tool_calls:
                pending_ids = {tc.id for tc in msg.tool_calls}
            elif msg.role == Role.TOOL:
                assert msg.tool_call_id in pending_ids

        # Next turn below trigger keeps the exact prefix byte-for-byte for KV prompt caching
        run.messages.append(Message(role=Role.ASSISTANT, content="Next small step", tool_calls=[]))
        assembly2 = engine.assemble(run)
        assert assembly2.compacted_this_turn is False
        assert assembly2.messages[:-1] == assembly1.messages
        assert assembly2.cache_breakpoint_index == assembly1.cache_breakpoint_index == 2

    def test_static_prefix_before_task_matches_across_different_tasks(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        (repo / "AGENTS.md").write_text("Global project standard.", encoding="utf-8")
        (repo / "mod.py").write_text("def alpha() -> int:\n    return 1\n", encoding="utf-8")

        engine1 = ContextEngine(repo, task="Task A: inspect alpha")
        ins1, rmap1 = engine1.prepare_initial_context()
        prompt1 = build_system_prompt(
            "Task A: inspect alpha",
            [],
            repo_instructions=ins1.rendered,
            repo_map=rmap1.rendered,
        )

        engine2 = ContextEngine(repo, task="Task B: write docs")
        ins2, rmap2 = engine2.prepare_initial_context()
        prompt2 = build_system_prompt(
            "Task B: write docs",
            [],
            repo_instructions=ins2.rendered,
            repo_map=rmap2.rendered,
        )

        prefix1 = prompt1[: prompt1.index("\n## Task\n")]
        prefix2 = prompt2[: prompt2.index("\n## Task\n")]
        assert prefix1 == prefix2
        assert "Global project standard." in prefix1
        assert "def alpha() -> int" in prefix1


class TestContextLoopIntegrationAndConfig:
    """End-to-end integration of ContextEngine inside react_loop, journal, persistence, config."""

    @pytest.mark.asyncio
    async def test_scoped_instruction_injection_compaction_journal_and_cache_config(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runs_dir = tmp_path / "runs"
        monkeypatch.setattr("agent_forge.agent.persistence.USER_CONFIG_DIR", tmp_path)
        monkeypatch.setattr("agent_forge.agent.core.save_summary", lambda *_a: tmp_path / "s.json")

        repo = tmp_path / "repo"
        (repo / "pkg").mkdir(parents=True)
        (repo / ".git").mkdir()
        (repo / "AGENTS.md").write_text("Root instruction: check style.", encoding="utf-8")
        (repo / "pkg" / "AGENTS.md").write_text(
            "PKG CHILD RULE: always return status ok.", encoding="utf-8"
        )
        big_notes = ("diagnostic detail line\n" * 90).strip()
        (repo / "pkg" / "service.py").write_text(
            f'"""{big_notes}"""\ndef status() -> str:\n    return "bad"\n',
            encoding="utf-8",
        )

        policy = ContextPolicy(
            context_window_tokens=1_000,
            compaction_trigger_ratio=0.70,
            compaction_target_ratio=0.45,
            keep_recent_turns=1,
            stale_tool_result_max_chars=160,
        )
        engine = ContextEngine(repo, task="Fix pkg service", policy=policy)
        llm = _RecordingMockLLM(
            [
                LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="c1",
                            name="read_file",
                            arguments={"path": "/workspace/pkg/service.py"},
                        )
                    ],
                    usage=TokenUsage(100, 20, 120),
                    finish_reason="tool_calls",
                ),
                LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="c2",
                            name="write_file",
                            arguments={
                                "path": "/workspace/pkg/service.py",
                                "content": "def status() -> str:\n    return 'ok'\n",
                            },
                        )
                    ],
                    usage=TokenUsage(100, 20, 120),
                    finish_reason="tool_calls",
                ),
                LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="c3",
                            name="read_file",
                            arguments={"path": "/workspace/pkg/service.py"},
                        )
                    ],
                    usage=TokenUsage(80, 15, 95),
                    finish_reason="tool_calls",
                ),
                LLMResponse(
                    content="Updated pkg/service.py cleanly.",
                    tool_calls=[],
                    usage=TokenUsage(60, 10, 70),
                    finish_reason="stop",
                ),
            ]
        )

        registry = create_default_registry(load_plugins=False)
        sb = SubprocessEvalSandbox()
        await sb.start(repo_path=str(repo))
        run = AgentRun(
            task="Fix pkg service", repo_path=str(repo), config=AgentConfig(max_iterations=8)
        )
        journal = RunJournal(run.id, base_dir=runs_dir)
        try:
            finished = await react_loop(
                run,
                llm,
                registry,
                sb,
                journal=journal,
                context_engine=engine,
            )
        finally:
            await sb.stop()

        assert finished.state == RunState.COMPLETED
        # Scoped pkg/AGENTS.md injected once when pkg/service.py was first read
        scoped_msgs = [
            m for m in finished.messages if "PKG CHILD RULE: always return status ok." in m.content
        ]
        assert len(scoped_msgs) == 1
        assert "Root instruction: check style." in finished.messages[0].content
        assert "def status() -> str" in finished.messages[0].content

        # Cache metadata propagated on LLMConfig
        assert llm.recorded_configs[0] is not None
        assert llm.recorded_configs[0].cache_key is not None
        cache_keys = {cfg.cache_key for cfg in llm.recorded_configs if cfg is not None}
        assert len(cache_keys) == 1

        # Persistence + replay record context telemetry cleanly
        loaded = load_run(finished.id, base_dir=runs_dir)
        assert loaded.context_stats["discovered_instructions"] == 2
        assert "pkg/service.py" in loaded.context_stats["modified_files"]

        replayed = replay_journal(finished.id, base_dir=runs_dir)
        assert replayed.valid is True
        assert replayed.compactions >= 1
        assert replayed.context_tokens_saved > 0

    def test_context_env_overrides_load_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENT_FORGE_CONTEXT_ENABLED", "false")
        monkeypatch.setenv("AGENT_FORGE_CONTEXT_MAX_INSTRUCTION_TOKENS", "2048")
        monkeypatch.setenv("AGENT_FORGE_CONTEXT_COMPACTION_TRIGGER_RATIO", "0.65")
        monkeypatch.setenv("AGENT_FORGE_CONTEXT_KEEP_RECENT_TURNS", "6")

        cfg = load_config()
        assert cfg.context.enabled is False
        assert cfg.context.max_instruction_tokens == 2048
        assert cfg.context.compaction_trigger_ratio == 0.65
        assert cfg.context.keep_recent_turns == 6

    @pytest.mark.asyncio
    async def test_estimate_messages_tokens_counts_tool_calls(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("agent_forge.agent.persistence.USER_CONFIG_DIR", tmp_path)
        monkeypatch.setattr("agent_forge.agent.core.save_summary", lambda *_a: tmp_path / "s.json")

        msgs = [
            Message(role=Role.SYSTEM, content="abcd" * 10),
            Message(
                role=Role.ASSISTANT,
                content="",
                tool_calls=[ToolCall(id="1", name="write_file", arguments={"path": "a.py"})],
            ),
        ]
        assert estimate_messages_tokens(msgs) > 15

        # Ensure WriteFileTool works when context_enabled=False
        reg = ToolRegistry()
        reg.register(WriteFileTool())
        sb = SubprocessEvalSandbox()
        await sb.start(repo_path=str(tmp_path))
        llm = AsyncMock()
        llm.complete.return_value = LLMResponse(
            content="Done.", tool_calls=[], finish_reason="stop"
        )
        run = AgentRun(
            task="No context",
            repo_path=str(tmp_path),
            config=AgentConfig(max_iterations=2, context_enabled=False),
        )
        try:
            res = await react_loop(run, llm, reg, sb)
        finally:
            await sb.stop()
        assert res.state == RunState.COMPLETED
        assert "## Repository Instructions" not in res.messages[0].content
