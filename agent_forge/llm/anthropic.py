"""Anthropic LLM provider adapter.

Uses the Anthropic Messages REST API via httpx. Handles tool mapping
(``tool_use`` / ``tool_result`` content blocks), streaming, and retry
logic per spec § 4.1 and § 7.2.
"""

from __future__ import annotations

import asyncio
import json
import random
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
from agent_forge.observability import get_logger

logger = get_logger("anthropic")

_ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1"
_DEFAULT_MODEL = "claude-sonnet-4-6"
_ANTHROPIC_VERSION = "2023-06-01"

# Retry config per spec § 7.2
_MAX_RETRIES = 5
_BACKOFF_BASE = 2.0  # seconds
_BACKOFF_MAX = 30.0  # cap delay at 30s
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 529}  # 529 = overloaded


class AnthropicProvider(LLMProvider):
    """Anthropic adapter using the Messages REST API with httpx."""

    def __init__(self, api_key: str, *, base_url: str | None = None) -> None:
        self._api_key = api_key
        self._base_url = base_url or _ANTHROPIC_BASE_URL
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(120.0),
            headers={
                "x-api-key": api_key,
                "anthropic-version": _ANTHROPIC_VERSION,
                "Content-Type": "application/json",
            },
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

        data = await self._post_with_retry("/messages", body, cfg)
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
        body["stream"] = True

        try:
            async with self._client.stream(
                "POST",
                "/messages",
                json=body,
                timeout=httpx.Timeout(cfg.timeout_seconds),
            ) as response:
                self._check_status(response)
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    raw = line[6:]
                    if raw.strip() == "[DONE]":
                        break
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    parsed = self._parse_stream_event(event, cfg.model)
                    if parsed is not None:
                        yield parsed
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(
                f"Anthropic streaming request timed out after "
                f"{cfg.timeout_seconds}s"
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
        """Build the Anthropic Messages API request body."""
        system_text, api_messages = self._messages_to_anthropic(messages)

        body: dict[str, Any] = {
            "model": config.model,
            "messages": api_messages,
            "max_tokens": config.max_tokens,
            "temperature": config.temperature,
            "top_p": config.top_p,
        }

        if system_text:
            body["system"] = system_text

        if tools:
            body["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.parameters,
                }
                for t in tools
            ]

        return body

    @staticmethod
    def _messages_to_anthropic(
        messages: list[Message],
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Convert internal messages to Anthropic format.

        Returns:
            A tuple of (system_text, api_messages).
        """
        system_text: str | None = None
        api_messages: list[dict[str, Any]] = []

        for msg in messages:
            if msg.role == Role.SYSTEM:
                system_text = msg.content
            elif msg.role == Role.USER:
                api_messages.append({
                    "role": "user",
                    "content": msg.content,
                })
            elif msg.role == Role.ASSISTANT:
                content: list[dict[str, Any]] = []
                if msg.content:
                    content.append({"type": "text", "text": msg.content})
                if msg.tool_calls:
                    for tc in msg.tool_calls:
                        content.append({
                            "type": "tool_use",
                            "id": tc.id,
                            "name": tc.name,
                            "input": tc.arguments,
                        })
                api_messages.append({
                    "role": "assistant",
                    "content": content or msg.content,
                })
            elif msg.role == Role.TOOL:
                api_messages.append({
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": msg.tool_call_id or "",
                            "content": msg.content,
                        }
                    ],
                })

        return system_text, api_messages

    # ------------------------------------------------------------------
    # Response Parsing
    # ------------------------------------------------------------------

    def _parse_response(
        self, data: dict[str, Any], model: str
    ) -> LLMResponse:
        """Parse an Anthropic Messages API response."""
        content_blocks = data.get("content", [])
        stop_reason = data.get("stop_reason", "end_turn")

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []

        for block in content_blocks:
            block_type = block.get("type")
            if block_type == "text":
                text_parts.append(block.get("text", ""))
            elif block_type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.get("id", ""),
                        name=block.get("name", ""),
                        arguments=block.get("input", {}),
                    )
                )

        usage_data = data.get("usage", {})
        input_tokens = usage_data.get("input_tokens", 0)
        output_tokens = usage_data.get("output_tokens", 0)
        usage = TokenUsage(
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
        )

        mapped_reason = self._map_finish_reason(stop_reason, tool_calls)

        return LLMResponse(
            content="\n".join(text_parts) if text_parts else None,
            tool_calls=tool_calls,
            usage=usage,
            model=model,
            finish_reason=mapped_reason,
        )

    def _parse_stream_event(
        self, event: dict[str, Any], model: str
    ) -> LLMResponse | None:
        """Parse a single SSE event from a streaming response."""
        event_type = event.get("type")

        if event_type == "content_block_delta":
            delta = event.get("delta", {})
            delta_type = delta.get("type")
            if delta_type == "text_delta":
                return LLMResponse(
                    content=delta.get("text"),
                    model=model,
                    finish_reason="",
                )
        elif event_type == "message_delta":
            stop_reason = event.get("delta", {}).get("stop_reason", "")
            return LLMResponse(
                content=None,
                model=model,
                finish_reason=self._map_finish_reason(stop_reason, []),
            )
        elif event_type == "message_stop":
            return LLMResponse(
                content=None,
                model=model,
                finish_reason="stop",
            )

        return None

    @staticmethod
    def _map_finish_reason(
        anthropic_reason: str, tool_calls: list[ToolCall]
    ) -> str:
        """Map Anthropic stop reason to our standard reasons."""
        if tool_calls:
            return "tool_calls"
        mapping = {
            "end_turn": "stop",
            "stop_sequence": "stop",
            "max_tokens": "length",
        }
        return mapping.get(anthropic_reason, "stop")

    # ------------------------------------------------------------------
    # HTTP + Retry
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_delay(
        attempt: int, resp: httpx.Response | None = None
    ) -> float:
        """Compute the retry delay, respecting Retry-After if present."""
        if resp is not None:
            retry_after = resp.headers.get(
                "Retry-After"
            ) or resp.headers.get("retry-after")
            if retry_after:
                try:
                    parsed: float = float(retry_after)
                    return min(parsed, _BACKOFF_MAX)
                except ValueError:
                    pass
        # Exponential backoff with jitter
        delay: float = _BACKOFF_BASE * (2**attempt)
        jitter = random.uniform(0, delay * 0.25)  # noqa: S311
        capped: float = min(delay + jitter, _BACKOFF_MAX)
        return capped

    async def _post_with_retry(
        self,
        url: str,
        body: dict[str, Any],
        config: LLMConfig,
    ) -> dict[str, Any]:
        """POST with exponential backoff retry on retryable errors."""
        last_exc: Exception | None = None

        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = await self._client.post(
                    url,
                    json=body,
                    timeout=httpx.Timeout(config.timeout_seconds),
                )
                if resp.status_code in _RETRYABLE_STATUS_CODES:
                    if attempt < _MAX_RETRIES:
                        delay = self._compute_delay(attempt, resp)
                        logger.warning(
                            "anthropic_retryable_error",
                            status_code=resp.status_code,
                            delay=round(delay, 1),
                            attempt=attempt + 1,
                            max_retries=_MAX_RETRIES,
                        )
                        await asyncio.sleep(delay)
                        continue
                    if resp.status_code == 429:
                        raise LLMRateLimitError(
                            f"Anthropic rate limit exceeded after "
                            f"{_MAX_RETRIES} retries"
                        )
                    raise LLMResponseError(
                        f"Anthropic API returned {resp.status_code} after "
                        f"{_MAX_RETRIES} retries"
                    )

                self._check_status(resp)

                try:
                    return resp.json()  # type: ignore[no-any-return]
                except (json.JSONDecodeError, ValueError) as exc:
                    if attempt < _MAX_RETRIES:
                        logger.warning(
                            "anthropic_malformed_response",
                            attempt=attempt + 1,
                            max_retries=_MAX_RETRIES,
                        )
                        await asyncio.sleep(_BACKOFF_BASE)
                        continue
                    raise LLMResponseError(
                        "Malformed JSON response from Anthropic API"
                    ) from exc

            except httpx.TimeoutException as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    logger.warning(
                        "anthropic_timeout",
                        attempt=attempt + 1,
                        max_retries=_MAX_RETRIES,
                    )
                    await asyncio.sleep(_BACKOFF_BASE)
                    continue
                raise LLMTimeoutError(
                    f"Anthropic request timed out after "
                    f"{config.timeout_seconds}s "
                    f"({_MAX_RETRIES} retries exhausted)"
                ) from last_exc

        # Should not reach here, but satisfy type checker
        msg = "Unexpected retry loop exit"
        raise LLMResponseError(msg)  # pragma: no cover

    @staticmethod
    def _check_status(resp: httpx.Response) -> None:
        """Raise specific errors for non-retryable status codes."""
        status_code = resp.status_code
        if status_code == 401 or status_code == 403:  # noqa: PLR1714
            raise LLMAuthError(
                f"Anthropic authentication failed (HTTP {status_code}). "
                "Check your ANTHROPIC_API_KEY."
            )
        if status_code >= 400:
            try:
                body = resp.json()
                detail = body.get("error", {}).get(
                    "message", resp.text[:500]
                )
            except Exception:  # noqa: BLE001
                detail = resp.text[:500]
            logger.error(
                "anthropic_api_error",
                status_code=status_code,
                detail=detail,
            )
            raise LLMResponseError(
                f"Anthropic API error (HTTP {status_code}): {detail}"
            )
