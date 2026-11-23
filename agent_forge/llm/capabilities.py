"""Provider capability model and runtime capability negotiation."""

from __future__ import annotations

import inspect
from dataclasses import asdict, dataclass, fields, replace
from typing import Any
from urllib.parse import urlparse

_CAPABILITY_BOOL_FIELDS: tuple[str, ...] = (
    "tool_calling",
    "reasoning_continuity",
    "parallel_tool_calls",
    "prompt_caching",
    "context_editing",
    "structured_output",
    "stateful_response_ids",
    "native_tools",
)
_LONG_CONTEXT_PREFIXES: tuple[str, ...] = ("gpt-5", "o3", "o4", "gpt-4.1")
_LOCAL_ENDPOINT_HOSTS: frozenset[str] = frozenset(
    {"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"}  # noqa: S104
)


@dataclass(frozen=True)
class ProviderCapabilities:
    """Declared runtime capabilities for an LLM adapter/model combination."""

    tool_calling: bool = True
    reasoning_continuity: bool = False
    parallel_tool_calls: bool = False
    prompt_caching: bool = False
    context_editing: bool = False
    structured_output: bool = False
    stateful_response_ids: bool = False
    native_tools: bool = False
    max_context_tokens: int = 128_000
    api_protocol: str = "chat_completions"

    def supports(self, capability: str) -> bool:
        """Return whether a named boolean capability flag is enabled."""
        if capability not in _CAPABILITY_BOOL_FIELDS:
            return False
        return bool(getattr(self, capability, False))

    def enabled_flags(self) -> tuple[str, ...]:
        """Return all enabled boolean capability names in canonical order."""
        return tuple(name for name in _CAPABILITY_BOOL_FIELDS if bool(getattr(self, name)))

    def to_dict(self) -> dict[str, Any]:
        """Serialize declared capabilities to a JSON-compatible dictionary."""
        return asdict(self)


@dataclass(frozen=True)
class CapabilityRequirement:
    """Required and preferred capabilities derived for a task or turn."""

    required: frozenset[str] = frozenset()
    preferred: frozenset[str] = frozenset()
    min_context_tokens: int = 0


def is_local_endpoint(base_url: str | None) -> bool:
    """Return whether *base_url* targets a local loopback or container host."""
    raw = (base_url or "").strip()
    if not raw:
        return False
    parsed = urlparse(raw if "://" in raw else f"http://{raw}")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False
    return host in _LOCAL_ENDPOINT_HOSTS or host.endswith(".localhost")


def _is_native_openai_url(base_url: str | None) -> bool:
    normalized = (base_url or "").strip().rstrip("/")
    return not normalized or normalized.startswith("https://api.openai.com")


def _openai_capabilities(
    model: str,
    *,
    api_mode: str | None,
    base_url: str | None,
    force_non_native: bool = False,
) -> ProviderCapabilities:
    is_native = False if force_non_native else _is_native_openai_url(base_url)
    if api_mode in ("responses", "chat_completions"):
        effective_mode = api_mode
    else:
        effective_mode = "responses" if is_native else "chat_completions"

    max_ctx = 200_000 if model.lower().startswith(_LONG_CONTEXT_PREFIXES) else 128_000
    if effective_mode == "responses":
        return ProviderCapabilities(
            tool_calling=True,
            reasoning_continuity=True,
            parallel_tool_calls=True,
            prompt_caching=True,
            context_editing=True,
            structured_output=True,
            stateful_response_ids=True,
            native_tools=True,
            max_context_tokens=max_ctx,
            api_protocol="openai_responses",
        )
    return ProviderCapabilities(
        tool_calling=True,
        reasoning_continuity=False,
        parallel_tool_calls=is_native,
        prompt_caching=is_native,
        context_editing=False,
        structured_output=True,
        stateful_response_ids=False,
        native_tools=is_native,
        max_context_tokens=max_ctx,
        api_protocol="openai_chat_completions",
    )


def _apply_overrides(
    base: ProviderCapabilities,
    overrides: dict[str, Any] | None,
) -> ProviderCapabilities:
    if not overrides:
        return base
    valid_names = {f.name for f in fields(ProviderCapabilities)}
    updates: dict[str, Any] = {}
    for key, value in overrides.items():
        if key not in valid_names:
            continue
        if key in _CAPABILITY_BOOL_FIELDS and isinstance(value, bool):
            updates[key] = value
        elif key == "max_context_tokens" and isinstance(value, int) and not isinstance(value, bool):
            updates[key] = max(1, value)
        elif key == "api_protocol" and isinstance(value, str) and value.strip():
            updates[key] = value.strip()
    return replace(base, **updates) if updates else base


def resolve_capabilities(
    provider: str,
    model: str = "",
    *,
    api_mode: str | None = None,
    base_url: str | None = None,
    overrides: dict[str, Any] | None = None,
) -> ProviderCapabilities:
    """Resolve declared capabilities for *provider* and *model*."""
    normalized = provider.strip().lower()
    if normalized == "openai":
        base = _openai_capabilities(model, api_mode=api_mode, base_url=base_url)
    elif normalized in ("openai-compatible", "openai_compatible"):
        base = _openai_capabilities(
            model,
            api_mode=api_mode or "chat_completions",
            base_url=base_url,
            force_non_native=True,
        )
    elif normalized == "anthropic":
        base = ProviderCapabilities(
            reasoning_continuity=True,
            parallel_tool_calls=True,
            prompt_caching=True,
            context_editing=True,
            structured_output=True,
            stateful_response_ids=False,
            native_tools=True,
            max_context_tokens=200_000,
            api_protocol="anthropic_messages",
        )
    elif normalized == "gemini":
        base = ProviderCapabilities(
            reasoning_continuity=True,
            parallel_tool_calls=True,
            prompt_caching=True,
            context_editing=False,
            structured_output=True,
            stateful_response_ids=False,
            native_tools=True,
            max_context_tokens=1_000_000,
            api_protocol="gemini_generate_content",
        )
    else:
        base = ProviderCapabilities()
    return _apply_overrides(base, overrides)


def query_provider_capabilities(
    provider: object,
    model: str | None = None,
) -> ProviderCapabilities:
    """Safely query runtime capabilities from *provider*, falling back to defaults."""
    if inspect.getattr_static(provider, "capabilities", None) is None:
        return ProviderCapabilities()
    cap_fn = getattr(provider, "capabilities", None)
    if callable(cap_fn) and not inspect.iscoroutinefunction(cap_fn):
        result = cap_fn(model)
        if isinstance(result, ProviderCapabilities):
            return result
    return ProviderCapabilities()
