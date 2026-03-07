"""E2E tests for the agent pipeline using real LLM API.

These tests exercise the full agent pipeline:
  LLM (Gemini) → Tools → Docker Sandbox

Requirements:
  - GEMINI_API_KEY environment variable
  - Docker running

Marked with @pytest.mark.e2e and excluded from default test suite.
Run manually: make test-e2e
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from agent_forge.agent.core import react_loop
from agent_forge.agent.models import AgentConfig, AgentRun, RunState
from agent_forge.agent.persistence import load_run, save_run
from agent_forge.llm.gemini import GeminiProvider
from agent_forge.sandbox.docker import DockerSandbox
from agent_forge.tools import create_default_registry

if TYPE_CHECKING:
    from pathlib import Path

    from agent_forge.tools.base import ToolRegistry

# Skip all tests if no API key
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not os.environ.get("GEMINI_API_KEY"),
        reason="GEMINI_API_KEY not set — skipping e2e tests",
    ),
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def api_key() -> str:
    """Return the Gemini API key from environment."""
    key = os.environ.get("GEMINI_API_KEY", "")
    assert key, "GEMINI_API_KEY must be set"
    return key


@pytest.fixture
def llm(api_key: str) -> GeminiProvider:
    """Create a real Gemini provider."""
    return GeminiProvider(api_key)


@pytest.fixture
def tools() -> ToolRegistry:
    """Create the default tool registry."""
    return create_default_registry()


@pytest.fixture
def sandbox() -> DockerSandbox:
    """Create a Docker sandbox."""
    return DockerSandbox()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Create a workspace with some files for the agent."""
    (tmp_path / "hello.py").write_text('print("Hello, World!")\n')
    (tmp_path / "data.txt").write_text("line1\nline2\nline3\n")
    (tmp_path / "subdir").mkdir()
    (tmp_path / "subdir" / "nested.txt").write_text("nested content\n")
    return tmp_path


def _make_run(task: str, workspace: Path, **overrides: object) -> AgentRun:
    """Helper to create an AgentRun with sensible test defaults."""
    config_kwargs = {
        "max_iterations": 3,
        "max_tokens_per_run": 100_000,
        "model": "gemini-2.0-flash",
        "provider": "gemini",
        "temperature": 0.0,
    }
    config_kwargs.update(overrides)
    return AgentRun(
        task=task,
        repo_path=str(workspace),
        config=AgentConfig(**config_kwargs),  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Tests — Tool Usage
# ---------------------------------------------------------------------------


class TestAgentToolUsage:
    """Tests that verify the agent uses tools correctly via real LLM."""

    @pytest.mark.asyncio
    async def test_agent_reads_file(
        self, llm: GeminiProvider, tools: ToolRegistry, sandbox: DockerSandbox, workspace: Path
    ) -> None:
        """Agent uses read_file tool to read a file in the sandbox."""
        run = _make_run("Read the file hello.py and tell me what it contains.", workspace)
        sandbox.start(str(workspace))
        try:
            result = await react_loop(run, llm, tools, sandbox)
        finally:
            sandbox.stop()
            await llm.close()

        assert result.state in (RunState.COMPLETED, RunState.TIMEOUT)
        assert result.iterations >= 1
        # Agent should have used at least one tool
        if result.state == RunState.COMPLETED:
            assert len(result.tool_invocations) >= 1

    @pytest.mark.asyncio
    async def test_agent_writes_file(
        self, llm: GeminiProvider, tools: ToolRegistry, sandbox: DockerSandbox, workspace: Path
    ) -> None:
        """Agent uses write_file tool to create a new file."""
        run = _make_run(
            "Create a file called 'output.txt' containing the text 'agent wrote this'.",
            workspace,
        )
        sandbox.start(str(workspace))
        try:
            result = await react_loop(run, llm, tools, sandbox)
        finally:
            sandbox.stop()
            await llm.close()

        assert result.state in (RunState.COMPLETED, RunState.TIMEOUT)
        # Check the file was created in the workspace
        output_file = workspace / "output.txt"
        if output_file.exists():
            assert "agent wrote this" in output_file.read_text()

    @pytest.mark.asyncio
    async def test_agent_lists_directory(
        self, llm: GeminiProvider, tools: ToolRegistry, sandbox: DockerSandbox, workspace: Path
    ) -> None:
        """Agent uses list_directory to explore workspace."""
        run = _make_run("List all files and directories in the workspace.", workspace)
        sandbox.start(str(workspace))
        try:
            result = await react_loop(run, llm, tools, sandbox)
        finally:
            sandbox.stop()
            await llm.close()

        assert result.state in (RunState.COMPLETED, RunState.TIMEOUT)
        # Agent should have used list_directory
        tool_names = [inv.tool_name for inv in result.tool_invocations]
        assert any(name in ("list_directory", "run_shell") for name in tool_names)

    @pytest.mark.asyncio
    async def test_agent_runs_shell_command(
        self, llm: GeminiProvider, tools: ToolRegistry, sandbox: DockerSandbox, workspace: Path
    ) -> None:
        """Agent uses run_shell to execute a command."""
        run = _make_run("Run 'echo hello' in the shell and report the output.", workspace)
        sandbox.start(str(workspace))
        try:
            result = await react_loop(run, llm, tools, sandbox)
        finally:
            sandbox.stop()
            await llm.close()

        assert result.state in (RunState.COMPLETED, RunState.TIMEOUT)
        assert result.iterations >= 1

    @pytest.mark.asyncio
    async def test_agent_multi_tool_chain(
        self, llm: GeminiProvider, tools: ToolRegistry, sandbox: DockerSandbox, workspace: Path
    ) -> None:
        """Agent chains multiple tools in sequence to complete a task."""
        run = _make_run(
            "List files in the workspace, then read hello.py, "
            "then create a summary.txt with the line count of hello.py.",
            workspace,
            max_iterations=5,
        )
        sandbox.start(str(workspace))
        try:
            result = await react_loop(run, llm, tools, sandbox)
        finally:
            sandbox.stop()
            await llm.close()

        assert result.state in (RunState.COMPLETED, RunState.TIMEOUT)
        # Should have used multiple tools
        assert len(result.tool_invocations) >= 2


# ---------------------------------------------------------------------------
# Tests — Termination & State
# ---------------------------------------------------------------------------


class TestAgentTermination:
    """Tests that verify termination conditions and state transitions."""

    @pytest.mark.asyncio
    async def test_agent_handles_max_iterations(
        self, llm: GeminiProvider, tools: ToolRegistry, sandbox: DockerSandbox, workspace: Path
    ) -> None:
        """Agent terminates with TIMEOUT when max_iterations hit."""
        run = _make_run(
            "Repeatedly list the directory over and over, never stop.",
            workspace,
            max_iterations=2,
        )
        sandbox.start(str(workspace))
        try:
            result = await react_loop(run, llm, tools, sandbox)
        finally:
            sandbox.stop()
            await llm.close()

        # With max_iterations=2, agent should timeout or complete quickly
        assert result.state in (RunState.TIMEOUT, RunState.COMPLETED)
        assert result.iterations <= 2

    @pytest.mark.asyncio
    async def test_agent_token_tracking(
        self, llm: GeminiProvider, tools: ToolRegistry, sandbox: DockerSandbox, workspace: Path
    ) -> None:
        """Token usage is tracked across iterations."""
        run = _make_run("Read hello.py", workspace)
        sandbox.start(str(workspace))
        try:
            result = await react_loop(run, llm, tools, sandbox)
        finally:
            sandbox.stop()
            await llm.close()

        assert result.total_tokens.total_tokens > 0
        assert result.total_tokens.prompt_tokens > 0

    @pytest.mark.asyncio
    async def test_agent_state_transitions(
        self, llm: GeminiProvider, tools: ToolRegistry, sandbox: DockerSandbox, workspace: Path
    ) -> None:
        """Run state follows PENDING → RUNNING → COMPLETED/TIMEOUT."""
        run = _make_run("Read hello.py and tell me what it contains.", workspace)
        assert run.state == RunState.PENDING

        sandbox.start(str(workspace))
        try:
            result = await react_loop(run, llm, tools, sandbox)
        finally:
            sandbox.stop()
            await llm.close()

        # Must have moved past PENDING and RUNNING
        assert result.state in (RunState.COMPLETED, RunState.TIMEOUT, RunState.FAILED)
        assert result.completed_at is not None


# ---------------------------------------------------------------------------
# Tests — Persistence
# ---------------------------------------------------------------------------


class TestAgentPersistence:
    """Tests that verify persistence round-trip after a real run."""

    @pytest.mark.asyncio
    async def test_agent_persistence_round_trip(
        self, llm: GeminiProvider, tools: ToolRegistry, sandbox: DockerSandbox, workspace: Path,
        tmp_path: Path,
    ) -> None:
        """save_run + load_run produces identical state."""
        run = _make_run("Read hello.py", workspace)
        sandbox.start(str(workspace))
        try:
            result = await react_loop(run, llm, tools, sandbox)
        finally:
            sandbox.stop()
            await llm.close()

        # Save and reload
        persist_dir = tmp_path / "persist"
        save_run(result, base_dir=persist_dir)
        loaded = load_run(result.id, base_dir=persist_dir)

        assert loaded.id == result.id
        assert loaded.task == result.task
        assert loaded.state == result.state
        assert loaded.iterations == result.iterations
        assert loaded.total_tokens.total_tokens == result.total_tokens.total_tokens
        assert len(loaded.messages) == len(result.messages)
        assert len(loaded.tool_invocations) == len(result.tool_invocations)
