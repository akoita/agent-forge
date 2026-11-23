"""LLM provider factory for instantiating adapters by name."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_forge.llm.anthropic import AnthropicProvider
from agent_forge.llm.gemini import GeminiProvider
from agent_forge.llm.openai import OpenAIProvider

if TYPE_CHECKING:
    from agent_forge.llm.base import LLMProvider

_PROVIDERS: dict[str, type[LLMProvider]] = {
    "gemini": GeminiProvider,
    "openai": OpenAIProvider,
    "openai-compatible": OpenAIProvider,
    "anthropic": AnthropicProvider,
}


def provider_names() -> tuple[str, ...]:
    """Return the names supported by the provider factory."""
    return tuple(sorted(_PROVIDERS))


def create_provider(
    name: str,
    api_key: str = "",
    **kwargs: object,
) -> LLMProvider:
    """Create an LLM provider instance by name.

    Args:
        name: Provider name (e.g. ``"gemini"``, ``"openai"``,
            ``"openai-compatible"``, ``"anthropic"``).
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
