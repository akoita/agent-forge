"""Tests for bounded hierarchical subagent delegation (research & isolated workers)."""

from __future__ import annotations

import asyncio
import os
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest

from agent_forge.agent.core import react_loop
from agent_forge.agent.models import AgentConfig, AgentRun, RunState
from agent_forge.agent.runner import run_agent_task
from agent_forge.agent.subagent import (
    SubagentLimits,
    SubagentResult,
    SubagentRunner,
    build_read_only_registry,
    build_worker_registry,
)
from agent_forge.cli import _build_cli_overrides
from agent_forge.config import load_config
from agent_forge.llm.base import (
    LLMConfig,
    LLMProvider,
    LLMResponse,
    Message,
    TokenUsage,
    ToolCall,
    ToolDefinition,
)
from agent_forge.llm.capabilities import ProviderCapabilities
from agent_forge.orchestration.events import Event, EventBus, EventType
from agent_forge.sandbox.base import ExecResult, Sandbox, SandboxConfig
from agent_forge.tools import ReadFileTool, ToolRegistry, WriteFileTool, create_default_registry
from agent_forge.tools.subagent import SpawnSubagentTool

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


@dataclass
class _FakeResponse:
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tokens: tuple[int, int] = (10, 5)


class _ScriptedLLM(LLMProvider):
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls = 0

    @property
    def model_name(self) -> str:
        return "scripted-test"

    def capabilities(self, model: str | None = None) -> ProviderCapabilities:
        del model
        return ProviderCapabilities(parallel_tool_calls=True)

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: LLMConfig | None = None,
    ) -> LLMResponse:
        del messages, tools, config
        idx = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        item = self._responses[idx]
        prompt_t, comp_t = item.tokens
        return LLMResponse(
            content=item.content,
            tool_calls=list(item.tool_calls or []),
            usage=TokenUsage(
                prompt_tokens=prompt_t,
                completion_tokens=comp_t,
                total_tokens=prompt_t + comp_t,
            ),
            model=self.model_name,
            finish_reason="stop",
        )

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: LLMConfig | None = None,
    ) -> AsyncIterator[LLMResponse]:
        del messages, tools, config
        if False:  # pragma: no cover
            yield LLMResponse(content=None)


class _LocalWorkspaceSandbox(Sandbox):
    """Sandbox backed by the started workspace root on local disk."""

    def __init__(self, initial_files: dict[str, str] | None = None) -> None:
        self.repo_path: Path | None = None
        self.initial_files = dict(initial_files or {})
        self.started = False
        self.stopped = False

    def _resolve(self, path: str) -> Path:
        assert self.repo_path is not None
        rel = path.removeprefix("/workspace/").lstrip("/")
        if rel in {"", "."}:
            return self.repo_path
        return self.repo_path / rel

    async def start(self, repo_path: str, config: SandboxConfig) -> None:
        from pathlib import Path

        self.repo_path = Path(repo_path)
        for rel, content in self.initial_files.items():
            target = self._resolve(rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        self.started = True

    async def exec(self, command: str, timeout: int = 30) -> ExecResult:
        return ExecResult(exit_code=0, stdout="", stderr="")

    async def read_file(self, path: str) -> str:
        target = self._resolve(path)
        if not target.exists():
            msg = f"File not found: {path}"
            raise FileNotFoundError(msg)
        return target.read_text(encoding="utf-8")

    async def write_file(self, path: str, content: str) -> None:
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    async def list_files(self, path: str = "/workspace") -> list[str]:
        assert self.repo_path is not None
        return [
            p.relative_to(self.repo_path).as_posix()
            for p in self.repo_path.rglob("*")
            if p.is_file()
        ]

    async def stop(self) -> None:
        self.stopped = True

    async def is_alive(self) -> bool:
        return self.started and not self.stopped


def _run_git(cwd: Path, *args: str) -> str:
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "GIT_AUTHOR_NAME": "Test User",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test User",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    proc = subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def _init_git_repo(repo: Path) -> Path:
    repo.mkdir(parents=True, exist_ok=True)
    _run_git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("# Sample\n", encoding="utf-8")
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "init")
    return repo


def test_registry_filtering_for_research_and_worker() -> None:
    parent_tools = create_default_registry(load_plugins=False)
    ro = build_read_only_registry(parent_tools)
    ro_names = {td.name for td in ro.list_definitions()}
    assert ro_names == {"read_file", "list_directory", "search_codebase", "git_diff"}

    worker = build_worker_registry(parent_tools)
    worker_names = {td.name for td in worker.list_definitions()}
    assert "write_file" in worker_names
    assert "run_shell" in worker_names
    assert "git_push" not in worker_names
    assert "create_pr" not in worker_names
    assert "spawn_subagent" not in worker_names

    parent_tools.register(
        SpawnSubagentTool(
            SubagentRunner(
                ".",
                AgentConfig(),
                SandboxConfig(),
                lambda: _ScriptedLLM([]),
                lambda _cfg: _LocalWorkspaceSandbox(),
                parent_tools,
            )
        )
    )
    worker_no_nested = build_worker_registry(parent_tools, allow_nested_subagents=False)
    assert not worker_no_nested.has("spawn_subagent")
    worker_nested = build_worker_registry(parent_tools, allow_nested_subagents=True)
    assert worker_nested.has("spawn_subagent")
    assert not worker_nested.has("git_push")


@pytest.mark.asyncio
async def test_research_subagent_reuses_parent_sandbox_and_accumulates_tokens(
    tmp_path: Path,
) -> None:
    (tmp_path / "auth.py").write_text("TOKEN_TTL = 3600\n", encoding="utf-8")
    parent_tools = create_default_registry(load_plugins=False)
    bus = EventBus()
    events: list[Event] = []

    async def _collect(ev: Event) -> None:
        events.append(ev)

    await bus.subscribe(EventType.SUBAGENT_STARTED, _collect)
    await bus.subscribe(EventType.SUBAGENT_COMPLETED, _collect)

    parent_sandbox = _LocalWorkspaceSandbox()
    await parent_sandbox.start(str(tmp_path), SandboxConfig())

    accumulated = [TokenUsage(0, 0, 0)]

    def _add_tokens(usage: TokenUsage) -> None:
        accumulated[0] = accumulated[0] + usage

    child_llm = _ScriptedLLM(
        [
            _FakeResponse(
                tool_calls=[
                    ToolCall(id="c1", name="read_file", arguments={"path": "/workspace/auth.py"})
                ],
                tokens=(20, 10),
            ),
            _FakeResponse(
                content="auth.py sets TOKEN_TTL = 3600. " + ("x" * 600),
                tokens=(25, 15),
            ),
        ]
    )

    runner = SubagentRunner(
        tmp_path,
        AgentConfig(max_iterations=10, context_enabled=False),
        SandboxConfig(),
        lambda: child_llm,
        lambda _cfg: _LocalWorkspaceSandbox(),
        parent_tools,
        limits=SubagentLimits(max_result_chars=500),
        parent_run_id="run-parent",
        event_bus=bus,
        on_tokens=_add_tokens,
    )
    tool = SpawnSubagentTool(runner)
    parent_tools.register(tool)

    res = await tool.execute({"task": "Read auth.py TTL", "mode": "research"}, parent_sandbox)
    assert res.exit_code == 0
    assert not parent_sandbox.stopped
    assert accumulated[0].total_tokens == 70
    assert res.metadata["mode"] == "research"
    assert res.metadata["state"] == "completed"
    assert res.metadata["total_tokens"] == 70
    assert len(res.output) <= 500
    assert res.output.endswith("... [subagent output truncated]")
    assert [ev.type for ev in events] == [EventType.SUBAGENT_STARTED, EventType.SUBAGENT_COMPLETED]
    assert events[1].data["total_tokens"] == 70


@pytest.mark.asyncio
async def test_parallel_research_subagents_run_concurrently_in_react_loop(tmp_path: Path) -> None:
    parent_tools = ToolRegistry()
    parent_tools.register(ReadFileTool())

    active = 0
    peak = 0
    both_entered = asyncio.Event()

    async def _fake_child_loop(
        child_run: AgentRun,
        llm: Any,
        tools: ToolRegistry,
        sandbox: Sandbox,
        **kwargs: Any,
    ) -> AgentRun:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active >= 2:
            both_entered.set()
        await asyncio.wait_for(both_entered.wait(), timeout=2.0)
        child_run.state = RunState.COMPLETED
        child_run.iterations = 1
        child_run.total_tokens = TokenUsage(prompt_tokens=15, completion_tokens=5, total_tokens=20)
        from agent_forge.llm.base import Role

        child_run.messages.append(Message(role=Role.ASSISTANT, content=f"Done: {child_run.task}"))
        active -= 1
        return child_run

    runner = SubagentRunner(
        tmp_path,
        AgentConfig(context_enabled=False),
        SandboxConfig(),
        lambda: _ScriptedLLM([]),
        lambda _cfg: _LocalWorkspaceSandbox(),
        parent_tools,
        react_loop_fn=_fake_child_loop,
    )
    parent_tools.register(SpawnSubagentTool(runner))

    parent_llm = _ScriptedLLM(
        [
            _FakeResponse(
                tool_calls=[
                    ToolCall(
                        id="s1",
                        name="spawn_subagent",
                        arguments={"task": "Inspect module A", "mode": "research"},
                    ),
                    ToolCall(
                        id="s2",
                        name="spawn_subagent",
                        arguments={"task": "Inspect module B", "mode": "research"},
                    ),
                ],
                tokens=(30, 10),
            ),
            _FakeResponse(content="Synthesized findings from A and B.", tokens=(20, 10)),
        ]
    )

    parent_sb = _LocalWorkspaceSandbox()
    await parent_sb.start(str(tmp_path), SandboxConfig())
    run = AgentRun(
        task="Inspect modules A and B",
        repo_path=str(tmp_path),
        config=AgentConfig(context_enabled=False),
    )
    await react_loop(run, parent_llm, parent_tools, parent_sb, emit_lifecycle=False)

    assert peak == 2
    assert run.state == RunState.COMPLETED
    assert len(run.tool_invocations) == 2
    assert "Synthesized findings from A and B." in (run.messages[-1].content or "")


@pytest.mark.asyncio
async def test_worker_subagent_isolates_changes_in_worktree_and_cleans_up(tmp_path: Path) -> None:
    repo = _init_git_repo(tmp_path / "repo")
    parent_tools = ToolRegistry()
    parent_tools.register(ReadFileTool())
    parent_tools.register(WriteFileTool())

    child_llm = _ScriptedLLM(
        [
            _FakeResponse(
                tool_calls=[
                    ToolCall(
                        id="w1",
                        name="write_file",
                        arguments={
                            "path": "/workspace/feature.py",
                            "content": "def helper() -> int:\n    return 42\n",
                        },
                    )
                ],
                tokens=(30, 15),
            ),
            _FakeResponse(content="Added helper() returning 42.", tokens=(20, 10)),
        ]
    )

    runner = SubagentRunner(
        repo,
        AgentConfig(max_iterations=6, context_enabled=False),
        SandboxConfig(),
        lambda: child_llm,
        lambda _cfg: _LocalWorkspaceSandbox(),
        parent_tools,
        limits=SubagentLimits(branch_prefix="agent-forge/subagent"),
        worktree_root=tmp_path / "wts",
    )
    res = await runner.delegate("Implement helper in feature.py", mode="worker")

    assert res.state == "completed"
    assert res.branch is not None
    assert res.branch.startswith("agent-forge/subagent/")
    assert res.files_modified == ["/workspace/feature.py"]
    assert "def helper() -> int:" in res.patch
    assert not (repo / "feature.py").exists()

    worktree_lines = [
        line for line in _run_git(repo, "worktree", "list", "--porcelain").splitlines() if line
    ]
    worktree_entries = [line for line in worktree_lines if line.startswith("worktree ")]
    assert len(worktree_entries) == 1


@pytest.mark.asyncio
async def test_failed_worker_subagent_aborts_worktree_without_orphans(tmp_path: Path) -> None:
    repo = _init_git_repo(tmp_path / "repo")
    parent_tools = ToolRegistry()
    parent_tools.register(WriteFileTool())

    async def _failing_loop(
        child_run: AgentRun,
        llm: Any,
        tools: ToolRegistry,
        sandbox: Sandbox,
        **kwargs: Any,
    ) -> AgentRun:
        await sandbox.write_file("/workspace/temp_dirty.py", "dirty = True\n")
        child_run.state = RunState.FAILED
        child_run.error = "simulated worker crash"
        return child_run

    runner = SubagentRunner(
        repo,
        AgentConfig(context_enabled=False),
        SandboxConfig(),
        lambda: _ScriptedLLM([]),
        lambda _cfg: _LocalWorkspaceSandbox(),
        parent_tools,
        react_loop_fn=_failing_loop,
        worktree_root=tmp_path / "wts",
    )
    res: SubagentResult = await runner.delegate("Modify something and fail", mode="worker")

    assert res.state == "failed"
    assert res.branch is None
    assert res.patch == ""
    assert not (repo / "temp_dirty.py").exists()
    worktree_entries = [
        line
        for line in _run_git(repo, "worktree", "list", "--porcelain").splitlines()
        if line.startswith("worktree ")
    ]
    assert len(worktree_entries) == 1
    branches = _run_git(repo, "branch", "--list", "agent-forge/subagent/*").strip()
    assert branches == ""


@pytest.mark.asyncio
async def test_subagent_limits_depth_validation_and_non_git_worker(tmp_path: Path) -> None:
    parent_tools = create_default_registry(load_plugins=False)
    runner_deep = SubagentRunner(
        tmp_path,
        AgentConfig(context_enabled=False),
        SandboxConfig(),
        lambda: _ScriptedLLM([]),
        lambda _cfg: _LocalWorkspaceSandbox(),
        parent_tools,
        limits=SubagentLimits(max_depth=1),
        current_depth=1,
    )
    deep_res = await runner_deep.delegate("Nested task", mode="research")
    assert deep_res.state == "failed"
    assert "Maximum subagent recursion depth (1) reached" in (deep_res.error or "")

    runner_capped = SubagentRunner(
        tmp_path,
        AgentConfig(context_enabled=False),
        SandboxConfig(),
        lambda: _ScriptedLLM([_FakeResponse(content="ok")]),
        lambda _cfg: _LocalWorkspaceSandbox(),
        parent_tools,
        limits=SubagentLimits(max_subagents_per_run=1),
    )
    first = await runner_capped.delegate("First", mode="research")
    second = await runner_capped.delegate("Second", mode="research")
    assert first.state == "completed"
    assert runner_capped.spawned_count == 1
    assert second.state == "failed"
    assert "Maximum subagents per run (1) reached" in (second.error or "")

    plain_runner = SubagentRunner(
        tmp_path,
        AgentConfig(context_enabled=False),
        SandboxConfig(),
        lambda: _ScriptedLLM([]),
        lambda _cfg: _LocalWorkspaceSandbox(),
        parent_tools,
    )
    worker_plain = await plain_runner.delegate("Worker on plain dir", mode="worker")
    assert worker_plain.state == "failed"
    assert "requires a Git repository" in (worker_plain.error or "")

    tool = SpawnSubagentTool(plain_runner)
    invalid_mode = await tool.execute(
        {"task": "Do work", "mode": "bogus"},
        _LocalWorkspaceSandbox(),
    )
    assert invalid_mode.exit_code == 1
    assert "Invalid mode" in (invalid_mode.error or "")


def test_subagent_config_env_overrides_and_cli_flags(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AGENT_FORGE_SUBAGENTS_ENABLED", "true")
    monkeypatch.setenv("AGENT_FORGE_SUBAGENTS_MAX_DEPTH", "2")
    monkeypatch.setenv("AGENT_FORGE_SUBAGENTS_MAX_ITERATIONS", "6")
    cfg = load_config(project_path=tmp_path)
    assert cfg.subagents.enabled is True
    assert cfg.subagents.max_depth == 2
    assert cfg.subagents.max_iterations == 6

    overrides = _build_cli_overrides(
        provider="gemini",
        model="gemini-3.1-flash-lite-preview",
        max_iterations=10,
        subagents=True,
    )
    assert overrides is not None
    assert overrides["subagents.enabled"] is True


@pytest.mark.asyncio
async def test_run_agent_task_attaches_subagent_tool_when_enabled(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("hello subagent\n", encoding="utf-8")
    cfg = load_config(project_path=tmp_path)
    cfg.context.enabled = False
    cfg.subagents.enabled = True

    class _RunLLM(LLMProvider):
        def __init__(self, role: str) -> None:
            self.role = role
            self.calls = 0

        @property
        def model_name(self) -> str:
            return "test-runner"

        async def complete(
            self,
            messages: list[Message],
            tools: list[ToolDefinition] | None = None,
            config: LLMConfig | None = None,
        ) -> LLMResponse:
            del messages, config
            self.calls += 1
            names = {td.name for td in (tools or [])}
            if self.role == "parent":
                assert "spawn_subagent" in names
                if self.calls == 1:
                    return LLMResponse(
                        content=None,
                        tool_calls=[
                            ToolCall(
                                id="call-sub",
                                name="spawn_subagent",
                                arguments={"task": "Read note.txt", "mode": "research"},
                            )
                        ],
                        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
                        model=self.model_name,
                        finish_reason="stop",
                    )
                return LLMResponse(
                    content="Subagent finished.",
                    usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
                    model=self.model_name,
                    finish_reason="stop",
                )
            assert "spawn_subagent" not in names
            return LLMResponse(
                content="note.txt contains hello subagent",
                usage=TokenUsage(prompt_tokens=40, completion_tokens=10, total_tokens=50),
                model=self.model_name,
                finish_reason="stop",
            )

        async def stream(
            self,
            messages: list[Message],
            tools: list[ToolDefinition] | None = None,
            config: LLMConfig | None = None,
        ) -> AsyncIterator[LLMResponse]:
            del messages, tools, config
            if False:  # pragma: no cover
                yield LLMResponse(content=None)

    created = 0

    def _llm_factory() -> LLMProvider:
        nonlocal created
        created += 1
        return _RunLLM("parent" if created == 1 else "child")

    run = await run_agent_task(
        "Use a research subagent to inspect note.txt",
        str(tmp_path),
        cfg,
        AgentConfig(context_enabled=False),
        "gemini",
        "test-key",
        provider_factory=lambda _p, _k: _llm_factory(),
        sandbox_factory=lambda _c: _LocalWorkspaceSandbox(),
        tools_factory=lambda: create_default_registry(load_plugins=False),
    )
    assert run.state == RunState.COMPLETED
    # 15 + 15 parent tokens + 50 child tokens
    assert run.total_tokens.total_tokens == 80
