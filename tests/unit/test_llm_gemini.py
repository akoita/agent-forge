"""Unit tests for the Gemini LLM provider adapter.

Uses respx to mock httpx requests to the Gemini REST API.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from agent_forge.llm.base import (
    LLMConfig,
    Message,
    Role,
    ToolDefinition,
)
from agent_forge.llm.errors import (
    LLMAuthError,
    LLMRateLimitError,
    LLMResponseError,
    LLMTimeoutError,
)
from agent_forge.llm.factory import create_provider
from agent_forge.llm.gemini import GeminiProvider

_GEMINI_URL = "https://generativelanguage.googleapis.com"
_MODEL = "gemini-3.1-flash-lite-preview"
_GENERATE_URL = f"{_GEMINI_URL}/v1beta/models/{_MODEL}:generateContent"
_STREAM_URL = f"{_GEMINI_URL}/v1beta/models/{_MODEL}:streamGenerateContent"


def _text_response(text: str) -> dict[str, object]:
    """Build a minimal Gemini text response payload."""
    return {
        "candidates": [
            {
                "content": {"parts": [{"text": text}]},
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 10,
            "candidatesTokenCount": 5,
            "totalTokenCount": 15,
        },
    }


def _tool_call_response(name: str, args: dict[str, object]) -> dict[str, object]:
    """Build a Gemini function call response payload."""
    return {
        "candidates": [
            {
                "content": {"parts": [{"functionCall": {"name": name, "args": args}}]},
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 20,
            "candidatesTokenCount": 10,
            "totalTokenCount": 30,
        },
    }


# ---------------------------------------------------------------------------
# Provider Construction
# ---------------------------------------------------------------------------


class TestGeminiProvider:
    """Tests for GeminiProvider construction and lifecycle."""

    def test_create_provider(self) -> None:
        provider = GeminiProvider(api_key="test-key")
        assert isinstance(provider, GeminiProvider)

    def test_factory(self) -> None:
        provider = create_provider("gemini", api_key="test-key")
        assert isinstance(provider, GeminiProvider)

    def test_factory_unknown(self) -> None:
        with pytest.raises(ValueError, match="Unknown LLM provider"):
            create_provider("unknown", api_key="k")


# ---------------------------------------------------------------------------
# Complete
# ---------------------------------------------------------------------------


class TestGeminiComplete:
    """Tests for GeminiProvider.complete()."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_text_completion(self) -> None:
        respx.post(_GENERATE_URL).respond(200, json=_text_response("Hello world!"))

        provider = GeminiProvider(api_key="test-key")
        config = LLMConfig(model=_MODEL)
        messages = [Message(role=Role.USER, content="Say hello")]

        resp = await provider.complete(messages, config=config)

        assert resp.content == "Hello world!"
        assert resp.finish_reason == "stop"
        assert resp.usage.total_tokens == 15
        assert resp.model == _MODEL
        assert resp.tool_calls == []

    @respx.mock
    @pytest.mark.asyncio
    async def test_tool_call_completion(self) -> None:
        respx.post(_GENERATE_URL).respond(
            200, json=_tool_call_response("read_file", {"path": "main.py"})
        )

        provider = GeminiProvider(api_key="test-key")
        tools = [
            ToolDefinition(
                name="read_file",
                description="Read a file",
                parameters={"type": "object", "properties": {"path": {"type": "string"}}},
            )
        ]
        messages = [Message(role=Role.USER, content="Read main.py")]
        config = LLMConfig(model=_MODEL)

        resp = await provider.complete(messages, tools=tools, config=config)

        assert resp.content is None
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].name == "read_file"
        assert resp.tool_calls[0].arguments == {"path": "main.py"}
        assert resp.finish_reason == "tool_calls"

    @respx.mock
    @pytest.mark.asyncio
    async def test_system_message_handled(self) -> None:
        respx.post(_GENERATE_URL).respond(200, json=_text_response("I understand."))

        provider = GeminiProvider(api_key="test-key")
        messages = [
            Message(role=Role.SYSTEM, content="You are a coding assistant."),
            Message(role=Role.USER, content="Hello"),
        ]
        config = LLMConfig(model=_MODEL)

        resp = await provider.complete(messages, config=config)
        assert resp.content == "I understand."

        # Verify the request body contains systemInstruction
        request = respx.calls[0].request
        body = json.loads(request.content)
        assert "systemInstruction" in body
        assert body["systemInstruction"]["parts"][0]["text"] == "You are a coding assistant."

    @respx.mock
    @pytest.mark.asyncio
    async def test_tool_result_message(self) -> None:
        respx.post(_GENERATE_URL).respond(200, json=_text_response("The file contains a function."))

        provider = GeminiProvider(api_key="test-key")
        messages = [
            Message(role=Role.USER, content="Read main.py"),
            Message(role=Role.TOOL, content="def main(): pass", tool_call_id="read_file"),
        ]
        config = LLMConfig(model=_MODEL)

        resp = await provider.complete(messages, config=config)
        assert resp.content == "The file contains a function."

        # Verify functionResponse in request body
        request = respx.calls[0].request
        body = json.loads(request.content)
        tool_content = body["contents"][1]
        assert "functionResponse" in tool_content["parts"][0]


# ---------------------------------------------------------------------------
# Error Handling
# ---------------------------------------------------------------------------


class TestGeminiErrors:
    """Tests for error handling and retry logic."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_auth_error(self) -> None:
        respx.post(_GENERATE_URL).respond(401, json={"error": "unauthorized"})

        provider = GeminiProvider(api_key="bad-key")
        messages = [Message(role=Role.USER, content="Hello")]
        config = LLMConfig(model=_MODEL)

        with pytest.raises(LLMAuthError, match="authentication failed"):
            await provider.complete(messages, config=config)

    @respx.mock
    @pytest.mark.asyncio
    async def test_rate_limit_exhausts_retries(self) -> None:
        # All 6 attempts (1 initial + 5 retries) return 429
        respx.post(_GENERATE_URL).respond(429, json={"error": "rate limited"})

        provider = GeminiProvider(api_key="test-key")
        messages = [Message(role=Role.USER, content="Hello")]
        config = LLMConfig(model=_MODEL)

        with pytest.raises(LLMRateLimitError, match="rate limit"):
            await provider.complete(messages, config=config)

        # Should have made 6 attempts (1 initial + 5 retries)
        assert len(respx.calls) == 6

    @respx.mock
    @pytest.mark.asyncio
    async def test_rate_limit_retries_then_succeeds(self) -> None:
        route = respx.post(_GENERATE_URL)
        # First two return 429, third succeeds
        route.side_effect = [
            httpx.Response(429, json={"error": "rate limited"}),
            httpx.Response(429, json={"error": "rate limited"}),
            httpx.Response(200, json=_text_response("Success!")),
        ]

        provider = GeminiProvider(api_key="test-key")
        messages = [Message(role=Role.USER, content="Hello")]
        config = LLMConfig(model=_MODEL)

        resp = await provider.complete(messages, config=config)
        assert resp.content == "Success!"
        assert len(respx.calls) == 3

    @respx.mock
    @pytest.mark.asyncio
    async def test_timeout_error(self) -> None:
        respx.post(_GENERATE_URL).mock(side_effect=httpx.ReadTimeout("timeout"))

        provider = GeminiProvider(api_key="test-key")
        messages = [Message(role=Role.USER, content="Hello")]
        config = LLMConfig(model=_MODEL, timeout_seconds=1)

        with pytest.raises(LLMTimeoutError, match="timed out"):
            await provider.complete(messages, config=config)

    @respx.mock
    @pytest.mark.asyncio
    async def test_server_error_retries(self) -> None:
        route = respx.post(_GENERATE_URL)
        route.side_effect = [
            httpx.Response(500, text="Internal Server Error"),
            httpx.Response(200, json=_text_response("Recovered")),
        ]

        provider = GeminiProvider(api_key="test-key")
        messages = [Message(role=Role.USER, content="Hello")]
        config = LLMConfig(model=_MODEL)

        resp = await provider.complete(messages, config=config)
        assert resp.content == "Recovered"

    @respx.mock
    @pytest.mark.asyncio
    async def test_malformed_json_retries(self) -> None:
        route = respx.post(_GENERATE_URL)
        route.side_effect = [
            httpx.Response(200, text="not json"),
            httpx.Response(200, json=_text_response("Fixed")),
        ]

        provider = GeminiProvider(api_key="test-key")
        messages = [Message(role=Role.USER, content="Hello")]
        config = LLMConfig(model=_MODEL)

        resp = await provider.complete(messages, config=config)
        assert resp.content == "Fixed"

    @respx.mock
    @pytest.mark.asyncio
    async def test_malformed_json_exhausts_retries(self) -> None:
        respx.post(_GENERATE_URL).respond(200, text="not json at all")

        provider = GeminiProvider(api_key="test-key")
        messages = [Message(role=Role.USER, content="Hello")]
        config = LLMConfig(model=_MODEL)

        with pytest.raises(LLMResponseError, match="Malformed JSON"):
            await provider.complete(messages, config=config)

    @respx.mock
    @pytest.mark.asyncio
    async def test_empty_candidates(self) -> None:
        respx.post(_GENERATE_URL).respond(200, json={"candidates": []})

        provider = GeminiProvider(api_key="test-key")
        messages = [Message(role=Role.USER, content="Hello")]
        config = LLMConfig(model=_MODEL)

        resp = await provider.complete(messages, config=config)
        assert resp.content is None
        assert resp.finish_reason == "error"
