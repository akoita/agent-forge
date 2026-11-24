"""Real Docker integration tests for Code Mode RPC bridge and worktree subagents (#223)."""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING, Any

import docker
import pytest
from docker.errors import DockerException

from agent_forge.agent.core import react_loop
from agent_forge.agent.journal import RunJournal, replay_journal
from agent_forge.agent.models import AgentConfig, AgentRun, RunState
from agent_forge.agent.subagent import SubagentLimits, SubagentRunner
from agent_forge.llm.base import (
    LLMConfig,
    LLMProvider,
    LLMResponse,
    Message,
    TokenUsage,
    ToolCall,
    ToolDefinition,
)
from agent_forge.orchestration.worktree import WorktreeManager
from agent_forge.sandbox.base import SandboxConfig
from agent_forge.sandbox.docker import DockerSandbox
from agent_forge.tools import ReadFileTool, RunShellTool, ToolRegistry, WriteFileTool

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

pytestmark = pytest.mark.integration


def _require_docker_sandbox_image() -> None:
    try:
        client = docker.from_env()
        client.ping()
        client.images.get("agent-forge-sandbox:latest")
    except DockerException:
        pytest.skip("Docker daemon or agent-forge-sandbox:latest image unavailable")


def _git(cwd: Path, *args: str) -> str:
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "GIT_AUTHOR_NAME": "Docker Test",
        "GIT_AUTHOR_EMAIL": "docker-test@example.com",
        "GIT_COMMITTER_NAME": "Docker Test",
        "GIT_COMMITTER_EMAIL": "docker-test@example.com",
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


class _DockerScriptedLLM(LLMProvider):
    def __init__(
        self,
        handler: Callable[[list[Message], list[ToolDefinition] | None, int], LLMResponse],
    ) -> None:
        self._handler = handler
        self._calls = 0

    @property
    def model_name(self) -> str:
        return "docker-scripted"

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: LLMConfig | None = None,
    ) -> LLMResponse:
        del config
        self._calls += 1
        return self._handler(messages, tools, self._calls)

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: LLMConfig | None = None,
    ) -> AsyncIterator[Any]:
        del messages, tools, config
        if False:  # pragma: no cover
            yield None


@pytest.mark.asyncio
async def test_code_mode_execute_code_in_real_offline_docker_sandbox(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify Code Mode runs Python 3 inside --network none Docker with gate & journal parity."""
    _require_docker_sandbox_image()
    monkeypatch.setenv("GEMINI_API_KEY", "host-only-secret-never-in-container")

    repo = tmp_path / "docker_code_mode_repo"
    repo.mkdir()
    (repo / "input.txt").write_text("value=41\n", encoding="utf-8")

    tools = ToolRegistry()
    tools.register(ReadFileTool())
    tools.register(WriteFileTool())
    tools.register(RunShellTool())

    script = (
        "import os\n"
        "assert os.environ.get('GEMINI_API_KEY') is None\n"
        "rd = call_tool('read_file', {'path': '/workspace/input.txt'})\n"
        "assert rd['exit_code'] == 0\n"
        "val = int(rd['output'].strip().split('=')[1]) + 1\n"
        "denied = call_tool('run_shell', {'command': 'rm -rf /tmp/nonexistent'})\n"
        "assert denied['exit_code'] == 1\n"
        "assert denied['metadata']['approval_denied'] is True\n"
        "wr = call_tool('write_file', {'path': '/workspace/output.txt', 'content': f'{val}\\n'})\n"
        "assert wr['exit_code'] == 0\n"
        "print(f'DOCKER_BRIDGE_OK={val}')\n"
    )
    seen_tools: list[list[str]] = []

    def _respond(_msgs: list[Message], defs: list[ToolDefinition] | None, step: int) -> LLMResponse:
        seen_tools.append([td.name for td in (defs or [])])
        if step == 1:
            return LLMResponse(
                content=None,
                tool_calls=[ToolCall(id="dc-1", name="execute_code", arguments={"code": script})],
                usage=TokenUsage(20, 10, 30),
                model="docker-scripted",
                finish_reason="tool_calls",
            )
        return LLMResponse(
            content="Docker Code Mode completed.",
            usage=TokenUsage(15, 5, 20),
            model="docker-scripted",
            finish_reason="stop",
        )

    journal_path = tmp_path / "docker_code_mode.jsonl"
    run = AgentRun(
        task="Compute value via Code Mode in Docker",
        repo_path=str(repo),
        config=AgentConfig(
            execution_mode="code_mode",
            approval_mode="deny",
            context_enabled=False,
        ),
    )

    sandbox = DockerSandbox()
    await sandbox.start(str(repo), SandboxConfig(network_enabled=False))
    try:
        await react_loop(
            run,
            _DockerScriptedLLM(_respond),
            tools,
            sandbox,
            emit_lifecycle=False,
            journal=RunJournal(run.id, path=journal_path, fsync=False),
        )
    finally:
        await sandbox.stop()

    assert run.state == RunState.COMPLETED
    assert seen_tools[0] == ["search_tools", "execute_code"]
    assert (repo / "output.txt").read_text(encoding="utf-8") == "42\n"
    assert list(repo.glob(".agent_forge_rpc_*")) == []
    replay = replay_journal(journal_path)
    assert replay.valid is True
    assert replay.anomalies == []
    assert replay.duplicate_side_effects == []


@pytest.mark.asyncio
async def test_worker_subagent_in_real_docker_sandbox_isolates_and_integrates(
    tmp_path: Path,
) -> None:
    """Verify worker subagent edits inside real Docker worktree and integrates cleanly onto main."""
    _require_docker_sandbox_image()
    repo = tmp_path / "docker_subagent_repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("# Docker Worker Test\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")

    parent_tools = ToolRegistry()
    parent_tools.register(ReadFileTool())
    parent_tools.register(WriteFileTool())

    def _worker_respond(
        _msgs: list[Message],
        _defs: list[ToolDefinition] | None,
        step: int,
    ) -> LLMResponse:
        if step == 1:
            return LLMResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="dw-1",
                        name="write_file",
                        arguments={"path": "/workspace/module.py", "content": "READY = True\n"},
                    )
                ],
                usage=TokenUsage(20, 10, 30),
                model="docker-scripted",
                finish_reason="tool_calls",
            )
        return LLMResponse(
            content="Added module.py inside isolated Docker worktree.",
            usage=TokenUsage(15, 5, 20),
            model="docker-scripted",
            finish_reason="stop",
        )

    runner = SubagentRunner(
        repo,
        AgentConfig(max_iterations=5, context_enabled=False),
        SandboxConfig(network_enabled=False),
        lambda: _DockerScriptedLLM(_worker_respond),
        lambda _cfg: DockerSandbox(),
        parent_tools,
        limits=SubagentLimits(branch_prefix="agent-forge/subagent"),
        worktree_root=tmp_path / "docker_wts",
    )
    res = await runner.delegate("Create module.py cleanly", mode="worker")

    assert res.state == "completed"
    assert res.branch is not None
    assert res.branch.startswith("agent-forge/subagent/")
    assert not (repo / "module.py").exists()

    worktree_entries = [
        line
        for line in _git(repo, "worktree", "list", "--porcelain").splitlines()
        if line.startswith("worktree ")
    ]
    assert len(worktree_entries) == 1

    manager = WorktreeManager(repo, branch_prefix="agent-forge/subagent")
    integrated = await manager.integrate_branch(res.branch, strategy="merge")
    assert integrated.status == "merged"
    assert (repo / "module.py").read_text(encoding="utf-8") == "READY = True\n"
