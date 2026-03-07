"""LLM provider factory for instantiating adapters by name."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_forge.llm.gemini import GeminiProvider

if TYPE_CHECKING:
    from agent_forge.llm.base import LLMProvider

_PROVIDERS: dict[str, type[GeminiProvider]] = {
    "gemini": GeminiProvider,
}


def create_provider(
    name: str,
    api_key: str,
    **kwargs: object,
) -> LLMProvider:
    """Create an LLM provider instance by name.

    Args:
        name: Provider name (e.g. ``"gemini"``).
        api_key: API key for the provider.
        **kwargs: Additional keyword arguments forwarded to the provider
            constructor (e.g. ``base_url``).

    Returns:
        An initialized ``LLMProvider`` instance.

    Raises:
        ValueError: If the provider name is not registered.
    """
    provider_cls = _PROVIDERS.get(name)
    if provider_cls is None:
        available = ", ".join(sorted(_PROVIDERS))
        msg = f"Unknown LLM provider '{name}'. Available: {available}"
        raise ValueError(msg)

    return provider_cls(api_key=api_key, **kwargs)  # type: ignore[arg-type]
