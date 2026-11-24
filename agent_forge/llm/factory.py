"""LLM provider factory for instantiating adapters by name."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agent_forge.llm.anthropic import AnthropicProvider
from agent_forge.llm.gemini import GeminiProvider
from agent_forge.llm.openai import OpenAIProvider
from agent_forge.llm.openrouter import OpenRouterProvider

if TYPE_CHECKING:
    from agent_forge.llm.base import LLMProvider

_PROVIDERS: dict[str, type[LLMProvider]] = {
    "gemini": GeminiProvider,
    "openai": OpenAIProvider,
    "openai-compatible": OpenAIProvider,
    "openrouter": OpenRouterProvider,
    "anthropic": AnthropicProvider,
}

_OPENROUTER_SETTING_FIELDS: tuple[str, ...] = (
    "http_referer",
    "app_title",
    "fallback_models",
    "allow_fallbacks",
    "require_parameters",
    "data_collection",
    "zdr",
    "provider_order",
    "only_providers",
    "ignore_providers",
    "provider_sort",
)


def provider_names() -> tuple[str, ...]:
    """Return the names supported by the provider factory."""
    return tuple(sorted(_PROVIDERS))


def provider_kwargs_from_settings(
    provider_name: str,
    provider_cfg: Any | None,
) -> dict[str, Any]:
    """Extract adapter constructor kwargs from a resolved ``ProviderSettings`` object."""
    if provider_cfg is None:
        return {}
    kwargs: dict[str, Any] = {}
    base_url = getattr(provider_cfg, "base_url", "")
    if isinstance(base_url, str) and base_url:
        kwargs["base_url"] = base_url
    api_mode = getattr(provider_cfg, "api_mode", "auto")
    if (
        provider_name in ("openai", "openai-compatible", "openrouter")
        and isinstance(api_mode, str)
        and api_mode != "auto"
    ):
        kwargs["api_mode"] = api_mode
    caps = getattr(provider_cfg, "capabilities", None)
    if isinstance(caps, dict) and caps:
        kwargs["capability_overrides"] = dict(caps)
    if provider_name == "openrouter":
        for field_name in _OPENROUTER_SETTING_FIELDS:
            val = getattr(provider_cfg, field_name, None)
            if val is None or val == "" or val == []:
                continue
            kwargs[field_name] = list(val) if isinstance(val, list) else val
    return kwargs


def create_provider(
    name: str,
    api_key: str = "",
    **kwargs: object,
) -> LLMProvider:
    """Create an LLM provider instance by name.

    Args:
        name: Provider name (e.g. ``"gemini"``, ``"openai"``,
            ``"openai-compatible"``, ``"openrouter"``, ``"anthropic"``).
        api_key: API key for the provider (optional for local endpoints).
        **kwargs: Additional keyword arguments forwarded to the provider
            constructor (e.g. ``base_url``, ``capability_overrides``).

    Returns:
        An initialized ``LLMProvider`` instance.

    Raises:
        ValueError: If the provider name is not registered.
    """
    provider_cls = _PROVIDERS.get(name)
    if provider_cls is None:
        available = ", ".join(provider_names())
        msg = f"Unknown LLM provider '{name}'. Available: {available}"
        raise ValueError(msg)

    init_kwargs = dict(kwargs)
    if name == "openai-compatible":
        init_kwargs.setdefault("api_mode", "chat_completions")
        init_kwargs.setdefault("provider_name", "openai-compatible")

    return provider_cls(api_key=api_key, **init_kwargs)  # type: ignore[call-arg]
