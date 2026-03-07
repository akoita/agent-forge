"""Gemini LLM provider adapter.

Uses the Gemini REST API via httpx. Handles tool mapping, streaming,
and retry logic per spec § 4.1 and § 7.2.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from agent_forge.llm.base import ToolDefinition

import httpx

from agent_forge.llm.base import (
    LLMConfig,
    LLMProvider,
    LLMResponse,
    Message,
    Role,
    TokenUsage,
    ToolCall,
)
from agent_forge.llm.errors import (
    LLMAuthError,
    LLMRateLimitError,
    LLMResponseError,
    LLMTimeoutError,
)

logger = logging.getLogger(__name__)

_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com"
_DEFAULT_MODEL = "gemini-2.0-flash"

# Retry config per spec § 7.2
_MAX_RETRIES = 3
_BACKOFF_BASE = 1.0  # seconds
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503}


class GeminiProvider(LLMProvider):
    """Gemini adapter using the REST API with httpx."""

    def __init__(self, api_key: str, *, base_url: str | None = None) -> None:
        self._api_key = api_key
        self._base_url = base_url or _GEMINI_BASE_URL
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(120.0),
        )

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: LLMConfig | None = None,
    ) -> LLMResponse:
        """Send a completion request and return the full response."""
        cfg = config or LLMConfig(model=_DEFAULT_MODEL)
        body = self._build_request_body(messages, tools, cfg)
        url = f"/v1beta/models/{cfg.model}:generateContent"

        data = await self._post_with_retry(url, body, cfg)
        return self._parse_response(data, cfg.model)

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: LLMConfig | None = None,
    ) -> AsyncIterator[LLMResponse]:
        """Stream partial responses as they arrive via SSE."""
        cfg = config or LLMConfig(model=_DEFAULT_MODEL)
        body = self._build_request_body(messages, tools, cfg)
        url = f"/v1beta/models/{cfg.model}:streamGenerateContent"

        params = {"alt": "sse", "key": self._api_key}
        try:
            async with self._client.stream(
                "POST",
                url,
                json=body,
                params=params,
                timeout=httpx.Timeout(cfg.timeout_seconds),
            ) as response:
                self._check_status(response.status_code)
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    raw = line[6:]
                    if raw.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    yield self._parse_response(chunk, cfg.model)
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(
                f"Gemini streaming request timed out after {cfg.timeout_seconds}s"
            ) from exc

    # ------------------------------------------------------------------
    # Request Building
    # ------------------------------------------------------------------

    def _build_request_body(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None,
        config: LLMConfig,
    ) -> dict[str, Any]:
        """Build the Gemini API request body from messages and tools."""
        contents = self._messages_to_contents(messages)
        body: dict[str, Any] = {"contents": contents}

        # Generation config
        body["generationConfig"] = {
            "temperature": config.temperature,
            "maxOutputTokens": config.max_tokens,
            "topP": config.top_p,
        }

        # System instruction (extract from messages)
        system_parts = [{"text": m.content} for m in messages if m.role == Role.SYSTEM]
        if system_parts:
            body["systemInstruction"] = {"parts": system_parts}

        # Tool declarations
        if tools:
            body["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": t.name,
                            "description": t.description,
                            "parameters": t.parameters,
                        }
                        for t in tools
                    ]
                }
            ]

        return body

    def _messages_to_contents(self, messages: list[Message]) -> list[dict[str, object]]:
        """Convert messages to Gemini 'contents' format.

        System messages are handled separately via systemInstruction.
        """
        contents: list[dict[str, Any]] = []

        for msg in messages:
            if msg.role == Role.SYSTEM:
                continue  # handled in systemInstruction

            role = "user" if msg.role in (Role.USER, Role.TOOL) else "model"
            parts: list[dict[str, Any]] = []

            if msg.role == Role.TOOL and msg.tool_call_id:
                # Tool result → functionResponse
                parts.append(
                    {
                        "functionResponse": {
                            "name": msg.tool_call_id,
                            "response": {"result": msg.content},
                        }
                    }
                )
            elif msg.tool_calls:
                # Assistant with tool calls → functionCall parts
                for tc in msg.tool_calls:
                    parts.append(
                        {
                            "functionCall": {
                                "name": tc.name,
                                "args": tc.arguments,
                            }
                        }
                    )
                if msg.content:
                    parts.append({"text": msg.content})
            elif msg.content:
                parts.append({"text": msg.content})

            if parts:
                contents.append({"role": role, "parts": parts})

        return contents

    # ------------------------------------------------------------------
    # Response Parsing
    # ------------------------------------------------------------------

    def _parse_response(self, data: dict[str, Any], model: str) -> LLMResponse:
        """Parse a Gemini API response into an LLMResponse."""
        candidates = data.get("candidates", [])
        if not candidates:
            return LLMResponse(
                content=None,
                model=model,
                finish_reason="error",
            )

        candidate = candidates[0]
        content_obj = candidate.get("content", {})
        parts = content_obj.get("parts", [])
        finish_reason = candidate.get("finishReason", "STOP")

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []

        for part in parts:
            if "text" in part:
                text_parts.append(part["text"])
            elif "functionCall" in part:
                fc = part["functionCall"]
                tool_calls.append(
                    ToolCall(
                        id=str(uuid.uuid4()),
                        name=fc.get("name", ""),
                        arguments=fc.get("args", {}),
                    )
                )

        # Parse token usage
        usage_data = data.get("usageMetadata", {})
        usage = TokenUsage(
            prompt_tokens=usage_data.get("promptTokenCount", 0),
            completion_tokens=usage_data.get("candidatesTokenCount", 0),
            total_tokens=usage_data.get("totalTokenCount", 0),
        )

        # Map Gemini finish reasons to our standard
        mapped_reason = self._map_finish_reason(str(finish_reason), tool_calls)

        return LLMResponse(
            content="\n".join(text_parts) if text_parts else None,
            tool_calls=tool_calls,
            usage=usage,
            model=model,
            finish_reason=mapped_reason,
        )

    @staticmethod
    def _map_finish_reason(gemini_reason: str, tool_calls: list[ToolCall]) -> str:
        """Map Gemini finish reason to our standard reasons."""
        if tool_calls:
            return "tool_calls"
        mapping = {
            "STOP": "stop",
            "MAX_TOKENS": "length",
            "SAFETY": "error",
            "RECITATION": "error",
            "OTHER": "error",
        }
        return mapping.get(gemini_reason, "stop")

    # ------------------------------------------------------------------
    # HTTP + Retry
    # ------------------------------------------------------------------

    async def _post_with_retry(
        self,
        url: str,
        body: dict[str, Any],
        config: LLMConfig,
    ) -> dict[str, Any]:
        """POST with exponential backoff retry on retryable errors."""
        params = {"key": self._api_key}
        last_exc: Exception | None = None

        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = await self._client.post(
                    url,
                    json=body,
                    params=params,
                    timeout=httpx.Timeout(config.timeout_seconds),
                )
                if resp.status_code in _RETRYABLE_STATUS_CODES:
                    if attempt < _MAX_RETRIES:
                        delay = _BACKOFF_BASE * (2**attempt)
                        logger.warning(
                            "Gemini API returned %d, retrying in %.1fs (attempt %d/%d)",
                            resp.status_code,
                            delay,
                            attempt + 1,
                            _MAX_RETRIES,
                        )
                        await asyncio.sleep(delay)
                        continue
                    # Out of retries — raise appropriate error
                    if resp.status_code == 429:
                        raise LLMRateLimitError(
                            f"Gemini rate limit exceeded after {_MAX_RETRIES} retries"
                        )
                    raise LLMResponseError(
                        f"Gemini API returned {resp.status_code} after {_MAX_RETRIES} retries"
                    )

                self._check_status(resp.status_code)

                try:
                    return resp.json()  # type: ignore[no-any-return]
                except (json.JSONDecodeError, ValueError) as exc:
                    if attempt < _MAX_RETRIES:
                        logger.warning(
                            "Malformed response from Gemini, retrying (attempt %d/%d)",
                            attempt + 1,
                            _MAX_RETRIES,
                        )
                        await asyncio.sleep(_BACKOFF_BASE)
                        continue
                    raise LLMResponseError("Malformed JSON response from Gemini API") from exc

            except httpx.TimeoutException as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    logger.warning(
                        "Gemini request timed out, retrying (attempt %d/%d)",
                        attempt + 1,
                        _MAX_RETRIES,
                    )
                    await asyncio.sleep(_BACKOFF_BASE)
                    continue
                raise LLMTimeoutError(
                    f"Gemini request timed out after {config.timeout_seconds}s "
                    f"({_MAX_RETRIES} retries exhausted)"
                ) from last_exc

        # Should not reach here, but satisfy type checker
        msg = "Unexpected retry loop exit"
        raise LLMResponseError(msg)  # pragma: no cover

    @staticmethod
    def _check_status(status_code: int) -> None:
        """Raise specific errors for non-retryable status codes."""
        if status_code == 401 or status_code == 403:  # noqa: PLR1714
            raise LLMAuthError(
                f"Gemini authentication failed (HTTP {status_code}). Check your GEMINI_API_KEY."
            )
        if status_code >= 400:
            raise LLMResponseError(f"Gemini API error (HTTP {status_code})")
