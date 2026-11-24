"""OpenRouter LLM gateway provider adapter.

Extends :class:`~agent_forge.llm.openai.OpenAIProvider` with OpenRouter-specific
attribution headers, fallback model routing, provider selection/privacy controls,
multi-turn ``reasoning_details`` preservation, upstream provider metadata, and
gateway-reported USD cost extraction.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal

import httpx

from agent_forge.llm.base import LLMConfig, LLMResponse, Message, Role, TokenUsage
from agent_forge.llm.capabilities import ProviderCapabilities, resolve_capabilities
from agent_forge.llm.errors import LLMResponseError, LLMTimeoutError
from agent_forge.llm.openai import OpenAIProvider

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from agent_forge.llm.base import ToolDefinition

_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
_DEFAULT_OPENROUTER_MODEL = "anthropic/claude-sonnet-4-6"


def _parse_openrouter_usage(usage_data: dict[str, Any] | None) -> TokenUsage:
    if not usage_data:
        return TokenUsage(0, 0, 0)
    cached = int((usage_data.get("prompt_tokens_details") or {}).get("cached_tokens", 0))
    reasoning_toks = int(
        (usage_data.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)
    )
    raw_cost = usage_data.get("cost")
    reported_cost = (
        float(raw_cost)
        if isinstance(raw_cost, (int, float)) and not isinstance(raw_cost, bool)
        else None
    )
    return TokenUsage(
        prompt_tokens=int(usage_data.get("prompt_tokens", 0)),
        completion_tokens=int(usage_data.get("completion_tokens", 0)),
        total_tokens=int(usage_data.get("total_tokens", 0)),
        cached_prompt_tokens=cached,
        reasoning_tokens=reasoning_toks,
        reported_cost_usd=reported_cost,
    )


def _extract_reasoning_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    raw_details = message.get("reasoning_details")
    if isinstance(raw_details, list):
        blocks = [dict(item) for item in raw_details if isinstance(item, dict)]
        if blocks:
            return blocks
    raw_reasoning = message.get("reasoning_content") or message.get("reasoning")
    if isinstance(raw_reasoning, str) and raw_reasoning.strip():
        return [{"type": "reasoning_content", "text": raw_reasoning}]
    return []


def _extract_replayable_reasoning_details(
    reasoning_blocks: Sequence[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    if not reasoning_blocks:
        return []
    details: list[dict[str, Any]] = []
    for block in reasoning_blocks:
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type", ""))
        if block_type.startswith("reasoning.") or "data" in block or "signature" in block:
            details.append(dict(block))
    return details


def _build_provider_preferences(
    *,
    allow_fallbacks: bool | None,
    require_parameters: bool | None,
    data_collection: Literal["allow", "deny"] | None,
    zdr: bool | None,
    provider_order: Sequence[str],
    only_providers: Sequence[str],
    ignore_providers: Sequence[str],
    provider_sort: Literal["price", "throughput", "latency"] | None,
) -> dict[str, Any]:
    prefs: dict[str, Any] = {}
    if allow_fallbacks is not None:
        prefs["allow_fallbacks"] = allow_fallbacks
    if require_parameters is not None:
        prefs["require_parameters"] = require_parameters
    if data_collection is not None:
        prefs["data_collection"] = data_collection
    if zdr is not None:
        prefs["zdr"] = zdr
    if provider_order:
        prefs["order"] = list(provider_order)
    if only_providers:
        prefs["only"] = list(only_providers)
    if ignore_providers:
        prefs["ignore"] = list(ignore_providers)
    if provider_sort is not None:
        prefs["sort"] = provider_sort
    return prefs


class OpenRouterProvider(OpenAIProvider):
    """OpenRouter multi-provider gateway adapter over OpenAI Chat Completions."""

    def __init__(
        self,
        api_key: str = "",
        *,
        base_url: str | None = None,
        api_mode: Literal["auto", "responses", "chat_completions"] = "chat_completions",
        capability_overrides: dict[str, Any] | None = None,
        http_referer: str = "",
        app_title: str = "",
        fallback_models: Sequence[str] | None = None,
        allow_fallbacks: bool | None = None,
        require_parameters: bool | None = None,
        data_collection: Literal["allow", "deny"] | None = None,
        zdr: bool | None = None,
        provider_order: Sequence[str] | None = None,
        only_providers: Sequence[str] | None = None,
        ignore_providers: Sequence[str] | None = None,
        provider_sort: Literal["price", "throughput", "latency"] | None = None,
        include_usage: bool = True,
        provider_name: str = "openrouter",
    ) -> None:
        effective_mode: Literal["auto", "responses", "chat_completions"] = (
            "chat_completions" if api_mode == "auto" else api_mode
        )
        normalized_url = (base_url or "").strip().rstrip("/") or _OPENROUTER_BASE_URL
        super().__init__(
            api_key=api_key,
            base_url=normalized_url,
            api_mode=effective_mode,
            capability_overrides=capability_overrides,
            provider_name=provider_name,
        )
        self._fallback_models = tuple(
            str(m).strip() for m in (fallback_models or ()) if str(m).strip()
        )
        self._include_usage = include_usage
        self._provider_prefs = _build_provider_preferences(
            allow_fallbacks=allow_fallbacks,
            require_parameters=require_parameters,
            data_collection=data_collection,
            zdr=zdr,
            provider_order=tuple(str(p).strip() for p in (provider_order or ()) if str(p).strip()),
            only_providers=tuple(str(p).strip() for p in (only_providers or ()) if str(p).strip()),
            ignore_providers=tuple(
                str(p).strip() for p in (ignore_providers or ()) if str(p).strip()
            ),
            provider_sort=provider_sort,
        )
        if http_referer.strip():
            self._client.headers["HTTP-Referer"] = http_referer.strip()
        if app_title.strip():
            self._client.headers["X-Title"] = app_title.strip()

    def capabilities(self, model: str | None = None) -> ProviderCapabilities:
        """Return declared capabilities for OpenRouter and the requested *model*."""
        return resolve_capabilities(
            self._provider_name,
            model or _DEFAULT_OPENROUTER_MODEL,
            api_mode=self._api_mode,
            base_url=self._base_url,
            overrides=self._capability_overrides,
        )

    def _build_request_body(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None,
        config: LLMConfig,
    ) -> dict[str, Any]:
        body = super()._build_request_body(messages, tools, config)
        if self._include_usage and "usage" not in body:
            body["usage"] = {"include": True}
        if self._fallback_models:
            ordered = [m for m in (config.model, *self._fallback_models) if m]
            deduped = list(dict.fromkeys(ordered))
            if len(deduped) > 1:
                body["models"] = deduped
        if self._provider_prefs:
            body["provider"] = dict(self._provider_prefs)
        return body

    @staticmethod
    def _messages_to_openai(messages: list[Message]) -> list[dict[str, Any]]:
        oai_messages = OpenAIProvider._messages_to_openai(messages)
        for msg, oai_msg in zip(messages, oai_messages, strict=True):
            if msg.role == Role.ASSISTANT and msg.reasoning_blocks:
                details = _extract_replayable_reasoning_details(msg.reasoning_blocks)
                if details:
                    oai_msg["reasoning_details"] = details
        return oai_messages

    def _parse_response(self, data: dict[str, Any], model: str) -> LLMResponse:
        choices = data.get("choices", [])
        if not choices:
            return LLMResponse(
                content=None,
                model=model,
                finish_reason="error",
                reported_model=data.get("model"),
                upstream_provider=data.get("provider"),
            )

        choice = choices[0]
        message = choice.get("message", {})
        native_reason = choice.get("native_finish_reason") or choice.get("finish_reason")
        finish_reason = choice.get("finish_reason") or "stop"

        content = message.get("content")
        tool_calls = self._parse_tool_calls(message.get("tool_calls"))
        caps = self.capabilities(model)
        if not tool_calls and not caps.tool_calling and isinstance(content, str) and content:
            content, tool_calls = self._extract_emulated_tool_calls(content)

        reasoning_blocks = _extract_reasoning_blocks(message)
        usage = _parse_openrouter_usage(data.get("usage"))
        mapped_reason = self._map_finish_reason(finish_reason, tool_calls)

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            usage=usage,
            model=model,
            finish_reason=mapped_reason,
            reported_model=data.get("model"),
            upstream_provider=data.get("provider"),
            native_finish_reason=str(native_reason) if native_reason is not None else None,
            response_id=data.get("id"),
            reasoning_blocks=reasoning_blocks,
        )

    def _parse_stream_chunk(self, data: dict[str, Any], model: str) -> LLMResponse:
        choices = data.get("choices", [])
        usage = (
            _parse_openrouter_usage(data.get("usage")) if data.get("usage") else TokenUsage(0, 0, 0)
        )
        reported_model = data.get("model")
        upstream_provider = data.get("provider")

        if not choices:
            return LLMResponse(
                content=None,
                usage=usage,
                model=model,
                finish_reason="",
                reported_model=reported_model,
                upstream_provider=upstream_provider,
                response_id=data.get("id"),
            )

        choice = choices[0]
        delta = choice.get("delta", {})
        content = delta.get("content")
        tool_calls = self._parse_tool_calls(delta.get("tool_calls"))
        raw_finish = choice.get("finish_reason") or ""
        native_reason = choice.get("native_finish_reason") or (raw_finish or None)
        reasoning_blocks = _extract_reasoning_blocks(delta)

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            usage=usage,
            model=model,
            finish_reason=raw_finish,
            reported_model=reported_model,
            upstream_provider=upstream_provider,
            native_finish_reason=str(native_reason) if native_reason is not None else None,
            response_id=data.get("id"),
            reasoning_blocks=reasoning_blocks,
        )

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: LLMConfig | None = None,
    ) -> AsyncIterator[LLMResponse]:
        """Stream OpenRouter Chat Completions SSE chunks including trailing usage/cost."""
        cfg = config or LLMConfig(model=_DEFAULT_OPENROUTER_MODEL)
        body = self._build_request_body(messages, tools, cfg)
        body["stream"] = True
        if self._include_usage:
            body["stream_options"] = {"include_usage": True}

        try:
            async with self._client.stream(
                "POST",
                "/chat/completions",
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
                        chunk = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    yield self._parse_stream_chunk(chunk, cfg.model)
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(
                f"OpenRouter streaming request timed out after {cfg.timeout_seconds}s"
            ) from exc
        except httpx.ConnectError as exc:
            raise LLMResponseError(
                f"Could not connect to OpenRouter endpoint {self._base_url}: {exc}"
            ) from exc
