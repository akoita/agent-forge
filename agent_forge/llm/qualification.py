"""Configuration and two-turn qualification for named model profiles."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from agent_forge.llm.base import LLMConfig, LLMProvider, Message, Role, TokenUsage, ToolDefinition
from agent_forge.llm.capabilities import (
    ProviderCapabilities,
    is_local_endpoint,
    resolve_capabilities,
)
from agent_forge.llm.errors import LLMError, LLMResponseError
from agent_forge.llm.factory import create_provider, provider_names

if TYPE_CHECKING:
    from agent_forge.config import ForgeConfig, ModelProfileSettings

_TOOL = ToolDefinition(
    name="read_probe_value",
    description="Return the value to copy to the final response.",
    parameters={"type": "object", "properties": {}},
)
_SECONDARY_PROBE_TOOL = ToolDefinition(
    name="read_secondary_value",
    description="Return a secondary value for parallel tool verification.",
    parameters={"type": "object", "properties": {}},
)
_PROBE_VALUE = "forge-probe-7391"
_MAX_OUTPUT_TOKENS = 256
_TIMEOUT_SECONDS = 45
_STRUCTURED_PROBE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


@dataclass
class ProbeResult:
    """Evidence from a single direct tool exchange."""

    status: str
    reason: str
    requests: int
    usage: TokenUsage
    reported_models: list[str]


@dataclass
class CapabilityProbeResult:
    """Observed endpoint capability probe report for an OpenAI-compatible or named profile."""

    reachable: bool
    model_listed: bool | None
    capabilities: ProviderCapabilities
    checks: dict[str, str]
    error: str | None = None


def resolve_profile_api_key_env(config: ForgeConfig, profile: ModelProfileSettings) -> str:
    """Return the credential environment variable name for *profile*."""
    provider_cfg = config.providers.get(profile.provider)
    default_env = provider_cfg.api_key_env if provider_cfg is not None else ""
    return profile.api_key_env or default_env


def resolve_profile_base_url(config: ForgeConfig, profile: ModelProfileSettings) -> str | None:
    """Return an explicit API base URL for *profile* when configured."""
    provider_cfg = config.providers.get(profile.provider)
    default_url = provider_cfg.base_url if provider_cfg is not None else ""
    base_url = profile.base_url or default_url
    return base_url or None


def resolve_profile_api_mode(config: ForgeConfig, profile: ModelProfileSettings) -> str:
    """Return the effective API mode configured for *profile*."""
    if profile.api_mode != "auto":
        return profile.api_mode
    provider_cfg = config.providers.get(profile.provider)
    return provider_cfg.api_mode if provider_cfg is not None else "auto"


def resolve_profile_capabilities(
    config: ForgeConfig,
    profile: ModelProfileSettings,
) -> dict[str, Any]:
    """Merge provider-level and profile-level capability overrides."""
    provider_cfg = config.providers.get(profile.provider)
    merged: dict[str, Any] = dict(provider_cfg.capabilities) if provider_cfg is not None else {}
    merged.update(profile.capabilities)
    return merged


def is_api_key_required(
    config: ForgeConfig,
    provider_name: str,
    *,
    profile: ModelProfileSettings | None = None,
    base_url: str | None = None,
) -> bool:
    """Return whether an API key must be present for this provider/profile/endpoint."""
    if profile is not None and profile.api_key_required is not None:
        return profile.api_key_required
    provider_cfg = config.providers.get(provider_name)
    if provider_cfg is not None and provider_cfg.api_key_required is not None:
        return provider_cfg.api_key_required
    effective_url = (
        base_url
        if base_url is not None
        else (
            resolve_profile_base_url(config, profile)
            if profile is not None
            else (provider_cfg.base_url if provider_cfg is not None else "")
        )
    )
    if provider_name in ("openai", "openai-compatible") and is_local_endpoint(effective_url):
        return False
    if provider_name == "openai-compatible":
        key_env = (
            resolve_profile_api_key_env(config, profile)
            if profile is not None
            else (provider_cfg.api_key_env if provider_cfg is not None else "")
        )
        return bool(key_env)
    return True


def resolve_profile_credentials(
    config: ForgeConfig,
    profile: ModelProfileSettings,
) -> tuple[str, str, bool]:
    """Return ``(key_env, api_key, key_required)`` for *profile*."""
    key_env = resolve_profile_api_key_env(config, profile)
    api_key = os.environ.get(key_env, "") if key_env else ""
    required = is_api_key_required(config, profile.provider, profile=profile)
    return key_env, api_key, required


def resolve_model_profile(config: ForgeConfig, name: str) -> ModelProfileSettings:
    """Resolve an alias and check that its adapter, endpoint, and credential settings exist."""
    profile = config.model_profiles.get(name)
    if profile is None:
        raise ValueError(f"Unknown model profile '{name}'.")
    if profile.provider not in provider_names() or profile.provider not in config.providers:
        raise ValueError(f"Unavailable provider '{profile.provider}' for profile '{name}'.")
    if profile.provider == "openai-compatible" and not resolve_profile_base_url(config, profile):
        msg = f"Model profile '{name}' with provider 'openai-compatible' requires a base_url."
        raise ValueError(msg)
    if is_api_key_required(
        config, profile.provider, profile=profile
    ) and not resolve_profile_api_key_env(config, profile):
        raise ValueError(f"Missing credential variable setting for profile '{name}'.")
    return profile


def _create_profile_provider(
    config: ForgeConfig,
    profile: ModelProfileSettings,
    api_key: str,
    *,
    capability_overrides: dict[str, Any] | None = None,
) -> LLMProvider:
    from agent_forge.llm.factory import provider_kwargs_from_settings

    base_url = resolve_profile_base_url(config, profile)
    api_mode = resolve_profile_api_mode(config, profile)
    provider_kwargs: dict[str, object] = dict(
        provider_kwargs_from_settings(profile.provider, config.providers.get(profile.provider))
    )
    if base_url:
        provider_kwargs["base_url"] = base_url
    if profile.provider in ("openai", "openai-compatible", "openrouter") and api_mode != "auto":
        provider_kwargs["api_mode"] = api_mode
    merged_caps = (
        dict(capability_overrides)
        if capability_overrides is not None
        else resolve_profile_capabilities(config, profile)
    )
    if merged_caps and profile.provider in ("openai", "openai-compatible", "openrouter"):
        provider_kwargs["capability_overrides"] = merged_caps
    return create_provider(profile.provider, api_key, **provider_kwargs)


async def probe_model(config: ForgeConfig, profile: ModelProfileSettings) -> ProbeResult:
    """Ask for one tool call and check whether the next turn consumes its result."""
    key_env, api_key, key_required = resolve_profile_credentials(config, profile)
    result = ProbeResult("failed", "", 0, TokenUsage(0, 0, 0), [])
    if key_required and not api_key:
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


async def _probe_tool_calling_support(
    config: ForgeConfig,
    profile: ModelProfileSettings,
    api_key: str,
) -> tuple[bool, str]:
    provider = _create_profile_provider(
        config,
        profile,
        api_key,
        capability_overrides={"tool_calling": True, "parallel_tool_calls": False},
    )
    options = LLMConfig(
        model=profile.model,
        temperature=0.0,
        max_tokens=128,
        timeout_seconds=_TIMEOUT_SECONDS,
        max_retries=0,
    )
    try:
        resp = await provider.complete(
            [Message(Role.USER, "Call read_probe_value with no arguments.")],
            [_TOOL],
            options,
        )
        return (True, "supported") if resp.tool_calls else (False, "unsupported")
    except LLMResponseError as exc:
        if "Could not connect" in str(exc):
            raise
        return False, "unsupported"
    finally:
        await provider.close()


async def _probe_structured_output_support(
    config: ForgeConfig,
    profile: ModelProfileSettings,
    api_key: str,
) -> tuple[bool, str]:
    provider = _create_profile_provider(
        config,
        profile,
        api_key,
        capability_overrides={"structured_output": True},
    )
    options = LLMConfig(
        model=profile.model,
        temperature=0.0,
        max_tokens=128,
        timeout_seconds=_TIMEOUT_SECONDS,
        max_retries=0,
        response_schema=_STRUCTURED_PROBE_SCHEMA,
    )
    try:
        resp = await provider.complete(
            [Message(Role.USER, 'Return JSON {"ok": true}.')],
            None,
            options,
        )
        parsed = json.loads((resp.content or "").strip())
        return (True, "supported") if isinstance(parsed, dict) else (False, "unsupported")
    except LLMResponseError as exc:
        if "Could not connect" in str(exc):
            raise
        return False, "unsupported"
    except (json.JSONDecodeError, ValueError):
        return False, "unsupported"
    finally:
        await provider.close()


async def _probe_parallel_tools_support(
    config: ForgeConfig,
    profile: ModelProfileSettings,
    api_key: str,
) -> tuple[bool, str]:
    provider = _create_profile_provider(
        config,
        profile,
        api_key,
        capability_overrides={"tool_calling": True, "parallel_tool_calls": True},
    )
    options = LLMConfig(
        model=profile.model,
        temperature=0.0,
        max_tokens=128,
        timeout_seconds=_TIMEOUT_SECONDS,
        max_retries=0,
        parallel_tool_calls=True,
    )
    try:
        await provider.complete(
            [Message(Role.USER, "Call read_probe_value and read_secondary_value.")],
            [_TOOL, _SECONDARY_PROBE_TOOL],
            options,
        )
        return True, "supported"
    except LLMResponseError as exc:
        if "Could not connect" in str(exc):
            raise
        return False, "unsupported"
    finally:
        await provider.close()


async def probe_endpoint_capabilities(
    config: ForgeConfig,
    profile: ModelProfileSettings,
) -> CapabilityProbeResult:
    """Probe an OpenAI-compatible or named endpoint to detect supported capabilities."""
    key_env, api_key, key_required = resolve_profile_credentials(config, profile)
    explicit_overrides = resolve_profile_capabilities(config, profile)
    base_url = resolve_profile_base_url(config, profile)
    api_mode = resolve_profile_api_mode(config, profile)
    declared = resolve_capabilities(
        profile.provider,
        profile.model,
        api_mode=api_mode,
        base_url=base_url,
        overrides=explicit_overrides,
    )
    if key_required and not api_key:
        return CapabilityProbeResult(
            reachable=False,
            model_listed=None,
            capabilities=declared,
            checks={},
            error=f"Missing credential variable {key_env}.",
        )

    model_listed: bool | None = None
    discovery_provider = _create_profile_provider(config, profile, api_key)
    try:
        list_fn = getattr(discovery_provider, "list_models", None)
        if callable(list_fn):
            listed = await list_fn()
            if listed:
                model_listed = profile.model in listed
    finally:
        await discovery_provider.close()

    checks: dict[str, str] = {}
    probed_overrides: dict[str, Any] = {}
    reachable = True
    error_msg: str | None = None
    try:
        tool_ok, tool_status = await _probe_tool_calling_support(config, profile, api_key)
        checks["tool_calling"] = tool_status
        probed_overrides["tool_calling"] = tool_ok

        schema_ok, schema_status = await _probe_structured_output_support(config, profile, api_key)
        checks["structured_output"] = schema_status
        probed_overrides["structured_output"] = schema_ok

        if tool_ok:
            par_ok, par_status = await _probe_parallel_tools_support(config, profile, api_key)
            checks["parallel_tool_calls"] = par_status
            probed_overrides["parallel_tool_calls"] = par_ok
        else:
            checks["parallel_tool_calls"] = "skipped"
            probed_overrides["parallel_tool_calls"] = False
    except (LLMError, TimeoutError) as exc:
        reachable = False
        error_msg = str(exc)

    final_caps = resolve_capabilities(
        profile.provider,
        profile.model,
        api_mode=api_mode,
        base_url=base_url,
        overrides={**probed_overrides, **explicit_overrides},
    )
    return CapabilityProbeResult(
        reachable=reachable,
        model_listed=model_listed,
        capabilities=final_caps,
        checks=checks,
        error=error_msg,
    )


def qualification_report(config: ForgeConfig, name: str, *, live: bool) -> dict[str, object]:
    """Return separately labelled configuration and live qualification results."""
    profile = resolve_model_profile(config, name)
    caps = resolve_capabilities(
        profile.provider,
        profile.model,
        api_mode=resolve_profile_api_mode(config, profile),
        base_url=resolve_profile_base_url(config, profile),
        overrides=resolve_profile_capabilities(config, profile),
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
