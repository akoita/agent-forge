"""Configuration and two-turn qualification for named model profiles."""

from __future__ import annotations

import asyncio
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from agent_forge.llm.base import LLMConfig, LLMProvider, Message, Role, TokenUsage, ToolDefinition
from agent_forge.llm.capabilities import resolve_capabilities
from agent_forge.llm.errors import LLMError
from agent_forge.llm.factory import create_provider, provider_names

if TYPE_CHECKING:
    from agent_forge.config import ForgeConfig, ModelProfileSettings

_TOOL = ToolDefinition(
    name="read_probe_value",
    description="Return the value to copy to the final response.",
    parameters={"type": "object", "properties": {}},
)
_PROBE_VALUE = "forge-probe-7391"
_MAX_OUTPUT_TOKENS = 256
_TIMEOUT_SECONDS = 45


@dataclass
class ProbeResult:
    """Evidence from a single direct tool exchange."""

    status: str
    reason: str
    requests: int
    usage: TokenUsage
    reported_models: list[str]


def resolve_profile_api_key_env(config: ForgeConfig, profile: ModelProfileSettings) -> str:
    """Return the credential environment variable name for *profile*."""
    return profile.api_key_env or config.providers[profile.provider].api_key_env


def resolve_profile_base_url(config: ForgeConfig, profile: ModelProfileSettings) -> str | None:
    """Return an explicit API base URL for *profile* when configured."""
    base_url = profile.base_url or config.providers[profile.provider].base_url
    return base_url or None


def resolve_profile_api_mode(config: ForgeConfig, profile: ModelProfileSettings) -> str:
    """Return the effective API mode configured for *profile*."""
    if profile.api_mode != "auto":
        return profile.api_mode
    provider_cfg = config.providers.get(profile.provider)
    return provider_cfg.api_mode if provider_cfg is not None else "auto"


def resolve_model_profile(config: ForgeConfig, name: str) -> ModelProfileSettings:
    """Resolve an alias and check that its adapter and credentials setting exist."""
    profile = config.model_profiles.get(name)
    if profile is None:
        raise ValueError(f"Unknown model profile '{name}'.")
    if profile.provider not in provider_names() or profile.provider not in config.providers:
        raise ValueError(f"Unavailable provider '{profile.provider}' for profile '{name}'.")
    if not resolve_profile_api_key_env(config, profile):
        raise ValueError(f"Missing credential variable setting for profile '{name}'.")
    return profile


def _create_profile_provider(
    config: ForgeConfig,
    profile: ModelProfileSettings,
    api_key: str,
) -> LLMProvider:
    base_url = resolve_profile_base_url(config, profile)
    api_mode = resolve_profile_api_mode(config, profile)
    provider_kwargs: dict[str, object] = {}
    if base_url:
        provider_kwargs["base_url"] = base_url
    if profile.provider == "openai" and api_mode != "auto":
        provider_kwargs["api_mode"] = api_mode
    return create_provider(profile.provider, api_key, **provider_kwargs)


async def probe_model(config: ForgeConfig, profile: ModelProfileSettings) -> ProbeResult:
    """Ask for one tool call and check whether the next turn consumes its result."""
    key_env = resolve_profile_api_key_env(config, profile)
    api_key = os.environ.get(key_env)
    result = ProbeResult("failed", "", 0, TokenUsage(0, 0, 0), [])
    if not api_key:
        result.reason = f"Missing credential variable {key_env}."
        return result

    provider = _create_profile_provider(config, profile, api_key)
    options = LLMConfig(
        model=profile.model,
        temperature=profile.temperature if profile.temperature is not None else 0.0,
        max_tokens=_MAX_OUTPUT_TOKENS,
        timeout_seconds=_TIMEOUT_SECONDS,
        max_retries=0,
    )
    history = [
        Message(
            Role.USER,
            "Call read_probe_value with no arguments. "
            "After receiving the result, reply with exactly that value and no other text.",
        ),
    ]

    try:
        async with asyncio.timeout(_TIMEOUT_SECONDS):
            result.requests += 1
            first = await provider.complete(history, [_TOOL], options)
            result.usage += first.usage
            if first.reported_model:
                result.reported_models.append(first.reported_model)
            if len(first.tool_calls) != 1:
                result.reason = "Expected exactly one tool call."
                return result
            call = first.tool_calls[0]
            if call.name != _TOOL.name or call.arguments:
                result.reason = "Unexpected tool name or arguments."
                return result
            history.extend(
                [
                    Message(
                        Role.ASSISTANT,
                        first.content or "",
                        tool_calls=[call],
                        reasoning_blocks=first.reasoning_blocks or None,
                        response_id=first.response_id,
                    ),
                    Message(Role.TOOL, _PROBE_VALUE, tool_call_id=call.id),
                ]
            )
            if provider.capabilities(profile.model).stateful_response_ids and first.response_id:
                options.previous_response_id = first.response_id
            result.requests += 1
            second = await provider.complete(history, [_TOOL], options)
            result.usage += second.usage
            if second.reported_model and second.reported_model not in result.reported_models:
                result.reported_models.append(second.reported_model)
            if (
                second.finish_reason == "stop"
                and not second.tool_calls
                and (second.content or "").strip() == _PROBE_VALUE
            ):
                result.status = "passed"
                result.reason = "Model returned the tool result."
            else:
                result.reason = "Model did not return the expected tool result."
    except (LLMError, TimeoutError) as exc:
        # Provider exception messages can contain response bodies or request URLs.
        result.reason = type(exc).__name__
    finally:
        await provider.close()
    return result


def qualification_report(config: ForgeConfig, name: str, *, live: bool) -> dict[str, object]:
    """Return separately labelled configuration and live qualification results."""
    profile = resolve_model_profile(config, name)
    caps = resolve_capabilities(
        profile.provider,
        profile.model,
        api_mode=resolve_profile_api_mode(config, profile),
        base_url=resolve_profile_base_url(config, profile),
        overrides=profile.capabilities,
    )
    return {
        "profile": name,
        "provider": profile.provider,
        "requested_model": profile.model,
        "capabilities": caps.to_dict(),
        "checked_at": datetime.now(UTC).isoformat(),
        "configuration": {"status": "passed"},
        "live_tool_exchange": (
            asdict(asyncio.run(probe_model(config, profile))) if live else {"status": "not_run"}
        ),
        "independent_fix": {"status": "not_run"},
    }
