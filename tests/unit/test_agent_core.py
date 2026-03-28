"""Unit tests for the agent core ReAct loop.

All tests use mocked LLM, ToolRegistry, and Sandbox to isolate
the loop logic from external dependencies.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_forge.agent.core import react_loop
from agent_forge.agent.models import AgentConfig, AgentRun, RunState
from agent_forge.agent.prompts import build_system_prompt
from agent_forge.llm.base import (
    LLMResponse,
    Role,
    TokenUsage,
    ToolCall,
    ToolDefinition,
)
from agent_forge.tools.base import ToolResult

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_run(**overrides: object) -> AgentRun:
    """Create an AgentRun with sensible defaults."""
    kwargs: dict[str, object] = {
        "task": "Add input validation",
        "repo_path": "/tmp/test-repo",
        "config": AgentConfig(max_iterations=5, max_tokens_per_run=10_000),
    }
    kwargs.update(overrides)
    return AgentRun(**kwargs)  # type: ignore[arg-type]


def _mock_tools() -> MagicMock:
    """Create a mock ToolRegistry with a single 'read_file' tool."""
    registry = MagicMock()
    registry.list_definitions.return_value = [
        ToolDefinition(
            name="read_file",
            description="Read a file",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}},
        ),
    ]
    tool = AsyncMock()
    tool.execute.return_value = ToolResult(output="file content", exit_code=0)
    registry.get.return_value = tool
    return registry


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestReactLoopCompletion:
    """LLM returns stop with no tool calls → COMPLETED."""

    @pytest.mark.asyncio
    async def test_completes_in_one_iteration(self) -> None:
        run = _make_run()
        llm = AsyncMock()
        llm.complete.return_value = LLMResponse(
            content="Done! I've completed the task.",
            tool_calls=[],
            usage=TokenUsage(100, 50, 150),
            finish_reason="stop",
        )
        tools = _mock_tools()
        sandbox = AsyncMock()

        result = await react_loop(run, llm, tools, sandbox)

        assert result.state == RunState.COMPLETED
        assert result.iterations == 1
        assert result.total_tokens.total_tokens == 150
        assert result.completed_at is not None
        assert result.error is None


class TestReactLoopToolExecution:
    """LLM requests tools → tools execute → results fed back."""

    @pytest.mark.asyncio
    async def test_tool_round_trip(self) -> None:
        run = _make_run()
        llm = AsyncMock()

        # Iteration 1: LLM requests read_file
        # Iteration 2: LLM completes
        llm.complete.side_effect = [
            LLMResponse(
                content="Let me read the file first.",
                tool_calls=[
                    ToolCall(id="call_1", name="read_file", arguments={"path": "main.py"}),
                ],
                usage=TokenUsage(100, 50, 150),
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content="Task complete.",
                tool_calls=[],
                usage=TokenUsage(200, 100, 300),
                finish_reason="stop",
            ),
        ]
        tools = _mock_tools()
        sandbox = AsyncMock()

        result = await react_loop(run, llm, tools, sandbox)

        assert result.state == RunState.COMPLETED
        assert result.iterations == 2
        assert len(result.tool_invocations) == 1
        assert result.tool_invocations[0].tool_name == "read_file"
        assert result.total_tokens.total_tokens == 450

        # Verify tool result was appended as a TOOL message
        tool_messages = [m for m in result.messages if m.role == Role.TOOL]
        assert len(tool_messages) == 1
        assert "file content" in tool_messages[0].content

    @pytest.mark.asyncio
    async def test_multiple_tool_calls_in_one_iteration(self) -> None:
        run = _make_run()
        llm = AsyncMock()
        llm.complete.side_effect = [
            LLMResponse(
                content="Reading two files.",
                tool_calls=[
                    ToolCall(id="call_1", name="read_file", arguments={"path": "a.py"}),
                    ToolCall(id="call_2", name="read_file", arguments={"path": "b.py"}),
                ],
                usage=TokenUsage(100, 50, 150),
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content="Done.",
                tool_calls=[],
                usage=TokenUsage(200, 100, 300),
                finish_reason="stop",
            ),
        ]
        tools = _mock_tools()
        sandbox = AsyncMock()

        result = await react_loop(run, llm, tools, sandbox)

        assert result.state == RunState.COMPLETED
        assert len(result.tool_invocations) == 2


class TestReactLoopTermination:
    """Various termination conditions."""

    @pytest.mark.asyncio
    async def test_max_iterations_timeout(self) -> None:
        run = _make_run(config=AgentConfig(max_iterations=2, max_tokens_per_run=100_000))
        llm = AsyncMock()
        # LLM always requests a tool — never stops
        llm.complete.return_value = LLMResponse(
            content="Working...",
            tool_calls=[
                ToolCall(id="call_x", name="read_file", arguments={"path": "f.py"}),
            ],
            usage=TokenUsage(10, 10, 20),
            finish_reason="tool_calls",
        )
        tools = _mock_tools()
        sandbox = AsyncMock()

        result = await react_loop(run, llm, tools, sandbox)

        assert result.state == RunState.TIMEOUT
        assert result.iterations == 2
        assert "Max iterations" in (result.error or "")

    @pytest.mark.asyncio
    async def test_token_budget_exceeded(self) -> None:
        run = _make_run(config=AgentConfig(max_iterations=10, max_tokens_per_run=100))
        llm = AsyncMock()
        llm.complete.return_value = LLMResponse(
            content="Working...",
            tool_calls=[
                ToolCall(id="call_x", name="read_file", arguments={"path": "f.py"}),
            ],
            usage=TokenUsage(50, 60, 110),  # Exceeds budget of 100
            finish_reason="tool_calls",
        )
        tools = _mock_tools()
        sandbox = AsyncMock()

        result = await react_loop(run, llm, tools, sandbox)

        assert result.state == RunState.TIMEOUT
        assert "budget" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_unrecoverable_error(self) -> None:
        run = _make_run()
        llm = AsyncMock()
        llm.complete.side_effect = RuntimeError("API connection lost")
        tools = _mock_tools()
        sandbox = AsyncMock()

        result = await react_loop(run, llm, tools, sandbox)

        assert result.state == RunState.FAILED
        assert "API connection lost" in (result.error or "")
        assert result.completed_at is not None


class TestReactLoopMalformedCalls:
    """Unknown tool → error injected, LLM self-corrects."""

    @pytest.mark.asyncio
    async def test_unknown_tool_self_corrects(self) -> None:

        run = _make_run()
        llm = AsyncMock()

        # Iteration 1: LLM requests unknown tool
        # Iteration 2: LLM self-corrects and completes
        llm.complete.side_effect = [
            LLMResponse(
                content="Trying to use a tool.",
                tool_calls=[
                    ToolCall(id="call_bad", name="nonexistent_tool", arguments={}),
                ],
                usage=TokenUsage(100, 50, 150),
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content="I see, let me use the correct tool.",
                tool_calls=[],
                usage=TokenUsage(100, 50, 150),
                finish_reason="stop",
            ),
        ]

        tools = _mock_tools()
        tools.get.side_effect = [
            KeyError("Unknown tool: nonexistent_tool"),  # First call fails
        ]
        sandbox = AsyncMock()

        result = await react_loop(run, llm, tools, sandbox)

        assert result.state == RunState.COMPLETED
        assert result.iterations == 2

        # Error message was injected as a TOOL message
        tool_messages = [m for m in result.messages if m.role == Role.TOOL]
        assert len(tool_messages) == 1
        assert "Unknown tool" in tool_messages[0].content

        # Tool invocation was still recorded
        assert len(result.tool_invocations) == 1
        assert result.tool_invocations[0].tool_name == "nonexistent_tool"


class TestReactLoopTokenAccumulation:
    """Tokens accumulate correctly across iterations."""

    @pytest.mark.asyncio
    async def test_tokens_accumulate(self) -> None:
        run = _make_run(config=AgentConfig(max_iterations=5, max_tokens_per_run=100_000))
        llm = AsyncMock()

        llm.complete.side_effect = [
            LLMResponse(
                content="Step 1.",
                tool_calls=[
                    ToolCall(id="c1", name="read_file", arguments={"path": "a.py"}),
                ],
                usage=TokenUsage(100, 50, 150),
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content="Step 2.",
                tool_calls=[
                    ToolCall(id="c2", name="read_file", arguments={"path": "b.py"}),
                ],
                usage=TokenUsage(200, 100, 300),
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content="Done.",
                tool_calls=[],
                usage=TokenUsage(150, 75, 225),
                finish_reason="stop",
            ),
        ]
        tools = _mock_tools()
        sandbox = AsyncMock()

        result = await react_loop(run, llm, tools, sandbox)

        assert result.state == RunState.COMPLETED
        assert result.total_tokens.prompt_tokens == 450
        assert result.total_tokens.completion_tokens == 225
        assert result.total_tokens.total_tokens == 675


class TestBuildSystemPrompt:
    """System prompt renders correctly."""

    def test_includes_tool_descriptions(self) -> None:
        prompt = build_system_prompt(
            task="Fix the bug",
            tool_definitions=[
                ToolDefinition(
                    name="read_file",
                    description="Read a file",
                    parameters={
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                    },
                ),
            ],
        )

        assert "Agent Forge" in prompt
        assert "read_file" in prompt
        assert "Fix the bug" in prompt
        assert "/workspace" in prompt
        assert "Backend: docker" in prompt
        assert "Runtime image: agent-forge-sandbox:latest" in prompt
        assert "Network: disabled" in prompt

    def test_empty_tools(self) -> None:
        prompt = build_system_prompt(task="Do something", tool_definitions=[])
        assert "Agent Forge" in prompt
        assert "Do something" in prompt

    def test_network_enabled_prompt(self) -> None:
        prompt = build_system_prompt(
            task="Install dependencies",
            tool_definitions=[],
            sandbox_backend="bwrap",
            sandbox_image="agent-forge-sandbox:node",
            network_enabled=True,
            command_timeout_seconds=480,
        )
        assert "Backend: bwrap" in prompt
        assert "Network: enabled" in prompt
        assert "installing dependencies" in prompt
        assert "agent-forge-sandbox:node" in prompt
        assert "480s" in prompt


class TestTokenUsageAdd:
    """TokenUsage addition."""

    def test_add(self) -> None:
        a = TokenUsage(10, 20, 30)
        b = TokenUsage(5, 15, 20)
        c = a + b
        assert c.prompt_tokens == 15
        assert c.completion_tokens == 35
        assert c.total_tokens == 50

    def test_iadd(self) -> None:
        a = TokenUsage(10, 20, 30)
        a = a + TokenUsage(5, 15, 20)
        assert a.total_tokens == 50
