"""Configuration and two-turn qualification for named model profiles."""

from __future__ import annotations

import asyncio
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from agent_forge.config import ForgeConfig, ModelProfileSettings, load_config
from agent_forge.llm.base import LLMConfig, Message, Role, TokenUsage, ToolDefinition
from agent_forge.llm.errors import LLMError
from agent_forge.llm.factory import create_provider, provider_names

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


def resolve_model_profile(config: ForgeConfig, name: str) -> ModelProfileSettings:
    """Resolve an alias and check that its adapter and credentials setting exist."""
    profile = config.model_profiles.get(name)
    if profile is None:
        raise ValueError(f"Unknown model profile '{name}'.")
    if profile.provider not in provider_names() or profile.provider not in config.providers:
        raise ValueError(f"Unavailable provider '{profile.provider}' for profile '{name}'.")
    if not config.providers[profile.provider].api_key_env:
        raise ValueError(f"Missing credential variable setting for profile '{name}'.")
    return profile


async def probe_model(config: ForgeConfig, profile: ModelProfileSettings) -> ProbeResult:
    """Ask for one tool call and check whether the next turn consumes its result."""
    key_env = config.providers[profile.provider].api_key_env
    api_key = os.environ.get(key_env)
    result = ProbeResult("failed", "", 0, TokenUsage(0, 0, 0), [])
    if not api_key:
        result.reason = f"Missing credential variable {key_env}."
        return result

    provider = create_provider(profile.provider, api_key)
    options = LLMConfig(
        model=profile.model,
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
                    Message(Role.ASSISTANT, first.content or "", tool_calls=[call]),
                    Message(Role.TOOL, _PROBE_VALUE, tool_call_id=call.id),
                ]
            )
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


def qualification_report(name: str, *, live: bool) -> dict[str, object]:
    """Return separately labelled configuration and live qualification results."""
    config = load_config()
    profile = resolve_model_profile(config, name)
    return {
        "profile": name,
        "provider": profile.provider,
        "requested_model": profile.model,
        "checked_at": datetime.now(UTC).isoformat(),
        "configuration": {"status": "passed"},
        "live_tool_exchange": (
            asdict(asyncio.run(probe_model(config, profile))) if live else {"status": "not_run"}
        ),
        "independent_fix": {"status": "not_run"},
    }
