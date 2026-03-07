"""Unit tests for LLM base data classes."""

from __future__ import annotations

from agent_forge.llm.base import (
    LLMConfig,
    LLMResponse,
    Message,
    Role,
    TokenUsage,
    ToolCall,
    ToolDefinition,
)


class TestRole:
    """Tests for the Role enum."""

    def test_values(self) -> None:
        assert Role.SYSTEM.value == "system"
        assert Role.USER.value == "user"
        assert Role.ASSISTANT.value == "assistant"
        assert Role.TOOL.value == "tool"


class TestMessage:
    """Tests for Message data class."""

    def test_basic_message(self) -> None:
        msg = Message(role=Role.USER, content="Hello")
        assert msg.role == Role.USER
        assert msg.content == "Hello"
        assert msg.tool_call_id is None
        assert msg.tool_calls is None

    def test_tool_result_message(self) -> None:
        msg = Message(role=Role.TOOL, content="file contents", tool_call_id="read_file")
        assert msg.role == Role.TOOL
        assert msg.tool_call_id == "read_file"

    def test_assistant_with_tool_calls(self) -> None:
        tc = ToolCall(id="tc1", name="read_file", arguments={"path": "main.py"})
        msg = Message(role=Role.ASSISTANT, content="", tool_calls=[tc])
        assert msg.tool_calls is not None
        assert len(msg.tool_calls) == 1
        assert msg.tool_calls[0].name == "read_file"


class TestToolDefinition:
    """Tests for ToolDefinition."""

    def test_construction(self) -> None:
        td = ToolDefinition(
            name="read_file",
            description="Read a file",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}},
        )
        assert td.name == "read_file"
        assert "path" in td.parameters["properties"]  # type: ignore[operator]


class TestLLMConfig:
    """Tests for LLMConfig defaults."""

    def test_defaults(self) -> None:
        cfg = LLMConfig()
        assert cfg.model == "gemini-3.1-flash-lite-preview"
        assert cfg.temperature == 0.0
        assert cfg.max_tokens == 4096
        assert cfg.top_p == 1.0
        assert cfg.timeout_seconds == 120

    def test_custom(self) -> None:
        cfg = LLMConfig(model="gpt-4o", temperature=0.7, max_tokens=8192)
        assert cfg.model == "gpt-4o"
        assert cfg.temperature == 0.7
        assert cfg.max_tokens == 8192


class TestLLMResponse:
    """Tests for LLMResponse."""

    def test_text_response(self) -> None:
        resp = LLMResponse(
            content="Hello!",
            usage=TokenUsage(10, 5, 15),
            model="gemini-3.1-flash-lite-preview",
            finish_reason="stop",
        )
        assert resp.content == "Hello!"
        assert resp.tool_calls == []
        assert resp.usage.total_tokens == 15

    def test_tool_call_response(self) -> None:
        tc = ToolCall(id="tc1", name="write_file", arguments={"path": "x.py", "content": "pass"})
        resp = LLMResponse(
            content=None,
            tool_calls=[tc],
            model="gemini-3.1-flash-lite-preview",
            finish_reason="tool_calls",
        )
        assert resp.content is None
        assert len(resp.tool_calls) == 1
        assert resp.finish_reason == "tool_calls"

    def test_defaults(self) -> None:
        resp = LLMResponse(content="hi")
        assert resp.tool_calls == []
        assert resp.usage.total_tokens == 0
        assert resp.model == ""
        assert resp.finish_reason == ""
