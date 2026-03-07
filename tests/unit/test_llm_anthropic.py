"""Unit tests for the Anthropic LLM provider adapter.

Uses respx to mock httpx requests to the Anthropic Messages API.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from agent_forge.llm.anthropic import AnthropicProvider
from agent_forge.llm.base import (
    LLMConfig,
    Message,
    Role,
    ToolCall,
    ToolDefinition,
)
from agent_forge.llm.errors import (
    LLMAuthError,
    LLMRateLimitError,
    LLMResponseError,
    LLMTimeoutError,
)
from agent_forge.llm.factory import create_provider

_ANTHROPIC_URL = "https://api.anthropic.com/v1"
_MODEL = "claude-sonnet-4-6"
_MESSAGES_URL = f"{_ANTHROPIC_URL}/messages"


def _text_response(text: str) -> dict[str, object]:
    """Build a minimal Anthropic text response."""
    return {
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


def _tool_use_response(
    name: str, args: dict[str, object]
) -> dict[str, object]:
    """Build an Anthropic tool_use response."""
    return {
        "content": [
            {
                "type": "tool_use",
                "id": "toolu_abc123",
                "name": name,
                "input": args,
            }
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 20, "output_tokens": 10},
    }


# ---------------------------------------------------------------------------
# Provider Construction
# ---------------------------------------------------------------------------


class TestAnthropicProvider:
    """Tests for AnthropicProvider construction and lifecycle."""

    def test_create_provider(self) -> None:
        provider = AnthropicProvider(api_key="test-key")
        assert isinstance(provider, AnthropicProvider)

    def test_factory(self) -> None:
        provider = create_provider("anthropic", api_key="test-key")
        assert isinstance(provider, AnthropicProvider)


# ---------------------------------------------------------------------------
# Complete
# ---------------------------------------------------------------------------


class TestAnthropicComplete:
    """Tests for AnthropicProvider.complete()."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_text_completion(self) -> None:
        respx.post(_MESSAGES_URL).respond(
            200, json=_text_response("Hello world!")
        )

        provider = AnthropicProvider(api_key="test-key")
        config = LLMConfig(model=_MODEL)
        messages = [Message(role=Role.USER, content="Say hello")]

        resp = await provider.complete(messages, config=config)

        assert resp.content == "Hello world!"
        assert resp.finish_reason == "stop"
        assert resp.usage.prompt_tokens == 10
        assert resp.usage.completion_tokens == 5
        assert resp.usage.total_tokens == 15
        assert resp.model == _MODEL
        assert resp.tool_calls == []

    @respx.mock
    @pytest.mark.asyncio
    async def test_tool_use_completion(self) -> None:
        respx.post(_MESSAGES_URL).respond(
            200,
            json=_tool_use_response("read_file", {"path": "main.py"}),
        )

        provider = AnthropicProvider(api_key="test-key")
        tools = [
            ToolDefinition(
                name="read_file",
                description="Read a file",
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
            )
        ]
        messages = [Message(role=Role.USER, content="Read main.py")]
        config = LLMConfig(model=_MODEL)

        resp = await provider.complete(
            messages, tools=tools, config=config
        )

        assert resp.content is None
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].name == "read_file"
        assert resp.tool_calls[0].arguments == {"path": "main.py"}
        assert resp.tool_calls[0].id == "toolu_abc123"
        assert resp.finish_reason == "tool_calls"

    @respx.mock
    @pytest.mark.asyncio
    async def test_system_message_handled(self) -> None:
        respx.post(_MESSAGES_URL).respond(
            200, json=_text_response("I understand.")
        )

        provider = AnthropicProvider(api_key="test-key")
        messages = [
            Message(
                role=Role.SYSTEM, content="You are a coding assistant."
            ),
            Message(role=Role.USER, content="Hello"),
        ]
        config = LLMConfig(model=_MODEL)

        resp = await provider.complete(messages, config=config)
        assert resp.content == "I understand."

        # Verify system as top-level field (not in messages)
        request = respx.calls[0].request
        body = json.loads(request.content)
        assert body["system"] == "You are a coding assistant."
        assert all(
            m["role"] != "system" for m in body["messages"]
        )

    @respx.mock
    @pytest.mark.asyncio
    async def test_tool_result_message(self) -> None:
        respx.post(_MESSAGES_URL).respond(
            200,
            json=_text_response("The file contains a function."),
        )

        provider = AnthropicProvider(api_key="test-key")
        messages = [
            Message(role=Role.USER, content="Read main.py"),
            Message(
                role=Role.TOOL,
                content="def main(): pass",
                tool_call_id="toolu_abc",
            ),
        ]
        config = LLMConfig(model=_MODEL)

        resp = await provider.complete(messages, config=config)
        assert resp.content == "The file contains a function."

        # Verify tool_result content block in request
        request = respx.calls[0].request
        body = json.loads(request.content)
        tool_msg = body["messages"][1]
        assert tool_msg["role"] == "user"
        assert tool_msg["content"][0]["type"] == "tool_result"
        assert tool_msg["content"][0]["tool_use_id"] == "toolu_abc"

    @respx.mock
    @pytest.mark.asyncio
    async def test_tools_in_request_body(self) -> None:
        respx.post(_MESSAGES_URL).respond(
            200, json=_text_response("ok")
        )

        provider = AnthropicProvider(api_key="test-key")
        tools = [
            ToolDefinition(
                name="read_file",
                description="Read a file",
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
            )
        ]
        messages = [Message(role=Role.USER, content="Hello")]
        config = LLMConfig(model=_MODEL)

        await provider.complete(messages, tools=tools, config=config)

        request = respx.calls[0].request
        body = json.loads(request.content)
        assert len(body["tools"]) == 1
        assert body["tools"][0]["name"] == "read_file"
        assert "input_schema" in body["tools"][0]

    @respx.mock
    @pytest.mark.asyncio
    async def test_assistant_tool_use_serialization(self) -> None:
        respx.post(_MESSAGES_URL).respond(
            200, json=_text_response("Done")
        )

        provider = AnthropicProvider(api_key="test-key")
        messages = [
            Message(role=Role.USER, content="Read file"),
            Message(
                role=Role.ASSISTANT,
                content=None,
                tool_calls=[
                    ToolCall(
                        id="toolu_1",
                        name="read_file",
                        arguments={"path": "foo.py"},
                    )
                ],
            ),
            Message(
                role=Role.TOOL,
                content="file contents",
                tool_call_id="toolu_1",
            ),
        ]
        config = LLMConfig(model=_MODEL)

        await provider.complete(messages, config=config)

        request = respx.calls[0].request
        body = json.loads(request.content)
        assistant_msg = body["messages"][1]
        assert assistant_msg["role"] == "assistant"
        tool_use_block = [
            b
            for b in assistant_msg["content"]
            if b["type"] == "tool_use"
        ]
        assert len(tool_use_block) == 1
        assert tool_use_block[0]["id"] == "toolu_1"

    @respx.mock
    @pytest.mark.asyncio
    async def test_anthropic_version_header(self) -> None:
        respx.post(_MESSAGES_URL).respond(
            200, json=_text_response("ok")
        )

        provider = AnthropicProvider(api_key="test-key")
        messages = [Message(role=Role.USER, content="Hello")]
        config = LLMConfig(model=_MODEL)

        await provider.complete(messages, config=config)

        request = respx.calls[0].request
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert request.headers["x-api-key"] == "test-key"


# ---------------------------------------------------------------------------
# Error Handling
# ---------------------------------------------------------------------------


class TestAnthropicErrors:
    """Tests for error handling and retry logic."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_auth_error(self) -> None:
        respx.post(_MESSAGES_URL).respond(
            401,
            json={"error": {"message": "unauthorized"}},
        )

        provider = AnthropicProvider(api_key="bad-key")
        messages = [Message(role=Role.USER, content="Hello")]
        config = LLMConfig(model=_MODEL)

        with pytest.raises(LLMAuthError, match="authentication failed"):
            await provider.complete(messages, config=config)

    @respx.mock
    @pytest.mark.asyncio
    async def test_rate_limit_exhausts_retries(self) -> None:
        respx.post(_MESSAGES_URL).respond(
            429,
            json={"error": {"message": "rate limited"}},
        )

        provider = AnthropicProvider(api_key="test-key")
        messages = [Message(role=Role.USER, content="Hello")]
        config = LLMConfig(model=_MODEL)

        with pytest.raises(LLMRateLimitError, match="rate limit"):
            await provider.complete(messages, config=config)

        assert len(respx.calls) == 6

    @respx.mock
    @pytest.mark.asyncio
    async def test_rate_limit_retries_then_succeeds(self) -> None:
        route = respx.post(_MESSAGES_URL)
        route.side_effect = [
            httpx.Response(
                429, json={"error": {"message": "rate limited"}}
            ),
            httpx.Response(
                429, json={"error": {"message": "rate limited"}}
            ),
            httpx.Response(200, json=_text_response("Success!")),
        ]

        provider = AnthropicProvider(api_key="test-key")
        messages = [Message(role=Role.USER, content="Hello")]
        config = LLMConfig(model=_MODEL)

        resp = await provider.complete(messages, config=config)
        assert resp.content == "Success!"
        assert len(respx.calls) == 3

    @respx.mock
    @pytest.mark.asyncio
    async def test_timeout_error(self) -> None:
        respx.post(_MESSAGES_URL).mock(
            side_effect=httpx.ReadTimeout("timeout")
        )

        provider = AnthropicProvider(api_key="test-key")
        messages = [Message(role=Role.USER, content="Hello")]
        config = LLMConfig(model=_MODEL, timeout_seconds=1)

        with pytest.raises(LLMTimeoutError, match="timed out"):
            await provider.complete(messages, config=config)

    @respx.mock
    @pytest.mark.asyncio
    async def test_server_error_retries(self) -> None:
        route = respx.post(_MESSAGES_URL)
        route.side_effect = [
            httpx.Response(500, text="Internal Server Error"),
            httpx.Response(200, json=_text_response("Recovered")),
        ]

        provider = AnthropicProvider(api_key="test-key")
        messages = [Message(role=Role.USER, content="Hello")]
        config = LLMConfig(model=_MODEL)

        resp = await provider.complete(messages, config=config)
        assert resp.content == "Recovered"

    @respx.mock
    @pytest.mark.asyncio
    async def test_overloaded_retries(self) -> None:
        """HTTP 529 (overloaded) is retryable for Anthropic."""
        route = respx.post(_MESSAGES_URL)
        route.side_effect = [
            httpx.Response(529, text="Overloaded"),
            httpx.Response(200, json=_text_response("Back")),
        ]

        provider = AnthropicProvider(api_key="test-key")
        messages = [Message(role=Role.USER, content="Hello")]
        config = LLMConfig(model=_MODEL)

        resp = await provider.complete(messages, config=config)
        assert resp.content == "Back"

    @respx.mock
    @pytest.mark.asyncio
    async def test_malformed_json_retries(self) -> None:
        route = respx.post(_MESSAGES_URL)
        route.side_effect = [
            httpx.Response(200, text="not json"),
            httpx.Response(200, json=_text_response("Fixed")),
        ]

        provider = AnthropicProvider(api_key="test-key")
        messages = [Message(role=Role.USER, content="Hello")]
        config = LLMConfig(model=_MODEL)

        resp = await provider.complete(messages, config=config)
        assert resp.content == "Fixed"

    @respx.mock
    @pytest.mark.asyncio
    async def test_malformed_json_exhausts_retries(self) -> None:
        respx.post(_MESSAGES_URL).respond(200, text="not json at all")

        provider = AnthropicProvider(api_key="test-key")
        messages = [Message(role=Role.USER, content="Hello")]
        config = LLMConfig(model=_MODEL)

        with pytest.raises(LLMResponseError, match="Malformed JSON"):
            await provider.complete(messages, config=config)

    @respx.mock
    @pytest.mark.asyncio
    async def test_empty_content(self) -> None:
        respx.post(_MESSAGES_URL).respond(
            200,
            json={
                "content": [],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        )

        provider = AnthropicProvider(api_key="test-key")
        messages = [Message(role=Role.USER, content="Hello")]
        config = LLMConfig(model=_MODEL)

        resp = await provider.complete(messages, config=config)
        assert resp.content is None
        assert resp.finish_reason == "stop"
