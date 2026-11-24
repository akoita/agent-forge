"""Configuration loading and merging for Agent Forge.

Implements 5-layer config precedence (highest priority first):
1. CLI flags
2. Environment variables (AGENT_FORGE_{SECTION}_{KEY})
3. Project config (./agent-forge.toml)
4. User config (~/.agent-forge/config.toml)
5. Built-in defaults (Pydantic model defaults)
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from types import UnionType
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Config Section Models
# ---------------------------------------------------------------------------

PROJECT_CONFIG_FILENAME = "agent-forge.toml"
USER_CONFIG_DIR = Path.home() / ".agent-forge"
USER_CONFIG_PATH = USER_CONFIG_DIR / "config.toml"
ENV_PREFIX = "AGENT_FORGE"
DEFAULT_SANDBOX_IMAGE = "agent-forge-sandbox:latest"


class AgentSettings(BaseModel):
    """Settings for the agent core (ReAct loop)."""

    max_iterations: int = 25
    max_tokens_per_run: int = 200_000
    default_provider: str = "gemini"
    default_model: str = "gemini-3.1-flash-lite-preview"
    temperature: float = 0.0
    system_prompt_path: str = ""
    approval_mode: Literal["auto", "prompt", "deny"] = "auto"
    max_cost_usd: float | None = None
    max_duration_seconds: float | None = None
    budget_action: Literal["timeout", "pause"] = "timeout"
    input_cost_per_million: float | None = Field(default=None, ge=0.0)
    output_cost_per_million: float | None = Field(default=None, ge=0.0)
    reasoning_effort: str | None = None
    thinking_mode: Literal["disabled", "enabled", "adaptive"] | None = None
    thinking_budget_tokens: int | None = Field(default=None, ge=0)
    thinking_level: Literal["minimal", "low", "medium", "high"] | None = None


class ContextSettings(BaseModel):
    """Settings for the context engine (instructions, repo map, working set, compaction)."""

    enabled: bool = True
    discover_instructions: bool = True
    max_instruction_tokens: int = 4_000
    repo_map_enabled: bool = True
    max_repo_map_tokens: int = 1_500
    max_repo_files: int = 500
    max_working_set_files: int = 12
    context_window_tokens: int = 100_000
    auto_size_context_window: bool = True
    compaction_trigger_ratio: float = 0.75
    compaction_target_ratio: float = 0.45
    keep_recent_turns: int = 4
    stale_tool_result_max_chars: int = 600


class SandboxSettings(BaseModel):
    """Settings for the sandbox runtime."""

    backend: str = "docker"
    image: str = DEFAULT_SANDBOX_IMAGE
    toolchain: Literal["auto", "python", "node", "go", "rust", "jvm", "full", "universal"] = "auto"
    dockerfile: str = ""
    setup_command: str = ""
    setup_timeout_seconds: int = Field(default=300, gt=0)
    cpu_limit: float = 1.0
    memory_limit: str = "512m"
    timeout_seconds: int = 300
    network_enabled: bool = False
    writable_cache_mounts: bool = True
    mount_host_toolchains: bool = True
    extra_ro_binds: list[str] = Field(default_factory=list)
    published_ports: list[str] = Field(default_factory=list)

    @field_validator("published_ports")
    @classmethod
    def _validate_published_ports(cls, value: list[str]) -> list[str]:
        if value:
            from agent_forge.sandbox.base import parse_published_ports

            parse_published_ports(value)
        return [item.strip() for item in value]

    @field_validator("extra_ro_binds")
    @classmethod
    def _validate_extra_ro_binds(cls, value: list[str]) -> list[str]:
        if value:
            from agent_forge.sandbox.base import validate_ro_bind_path

            return [validate_ro_bind_path(item) for item in value]
        return value


class QueueSettings(BaseModel):
    """Settings for the task queue backend and parallel run isolation."""

    backend: str = "memory"
    redis_url: str = "redis://localhost:6379/0"
    max_concurrent_runs: int = 4
    worktree_isolation: bool = True
    worktree_root: str = ""
    worktree_branch_prefix: str = "agent-forge/run"
    auto_remove_unchanged_worktrees: bool = True
    merge_strategy: Literal["merge", "rebase"] = "merge"


class LoggingSettings(BaseModel):
    """Settings for structured logging."""

    level: str = "INFO"
    format: str = "text"
    log_file: str = ""


class ServiceSettings(BaseModel):
    """Settings for the hosted service runtime."""

    host: str = "127.0.0.1"
    port: int = 8000
    root_dir: str = str(USER_CONFIG_DIR / "service")
    healthcheck_path: str = "/healthz"
    auth_enabled: bool = False
    api_key_header: str = "X-Agent-Forge-API-Key"
    clients_path: str = str(USER_CONFIG_DIR / "service" / "clients.toml")
    allow_local_path_sources: bool = False
    max_source_size_bytes: int = 50_000_000
    lease_ttl_seconds: float = Field(default=300.0, gt=0)
    recovery_interval_seconds: float = Field(default=5.0, gt=0)


class ProviderSettings(BaseModel):
    """Settings for a single LLM provider."""

    api_key_env: str = ""
    default_model: str = ""
    base_url: str = ""
    api_mode: Literal["auto", "responses", "chat_completions"] = "auto"
    api_key_required: bool | None = None
    capabilities: dict[str, Any] = Field(default_factory=dict)


class ModelProfileSettings(BaseModel):
    """A named model selection for a supported provider."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    base_url: str = ""
    api_key_env: str = ""
    api_key_required: bool | None = None
    temperature: float | None = None
    api_mode: Literal["auto", "responses", "chat_completions"] = "auto"
    capabilities: dict[str, Any] = Field(default_factory=dict)
    input_cost_per_million: float | None = Field(default=None, ge=0.0)
    output_cost_per_million: float | None = Field(default=None, ge=0.0)
    cache_read_cost_per_million: float | None = Field(default=None, ge=0.0)
    reasoning_effort: str | None = None
    thinking_mode: Literal["disabled", "enabled", "adaptive"] | None = None
    thinking_budget_tokens: int | None = Field(default=None, ge=0)
    thinking_level: Literal["minimal", "low", "medium", "high"] | None = None


class RoutingSettings(BaseModel):
    """Settings for capability- and cost-aware task/turn routing."""

    enabled: bool = False
    strategy: Literal["cost_first", "balanced", "capability_first"] = "balanced"
    candidates: list[str] = Field(default_factory=list)
    downgrade_budget_fraction: float = 0.75
    escalate_on_failures: int = 2


class MCPServerSettings(BaseModel):
    """Configuration for a single external MCP tool server."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    enabled: bool = True
    transport: Literal["stdio", "http"] = "stdio"
    command: str = ""
    args: list[str] = Field(default_factory=list)
    cwd: str = ""
    env: dict[str, str] = Field(default_factory=dict)
    env_passthrough: list[str] = Field(default_factory=list)
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    auth_env: str = ""
    auth_scheme: str = "Bearer"
    allow_tools: list[str] = Field(default_factory=list)
    deny_tools: list[str] = Field(default_factory=list)
    approval_mode: Literal["inherit", "auto", "prompt", "deny"] = "inherit"
    allow_read_only_without_approval: bool = False
    timeout_seconds: float = Field(default=30.0, gt=0)
    startup_timeout_seconds: float = Field(default=15.0, gt=0)
    failure_threshold: int = Field(default=3, ge=1)
    recovery_timeout_seconds: float = Field(default=30.0, gt=0)
    max_output_chars: int = Field(default=32_768, ge=256)


class MCPSettings(BaseModel):
    """Settings for external Model Context Protocol (MCP) servers."""

    enabled: bool = True
    servers: dict[str, MCPServerSettings] = Field(default_factory=dict)


def _default_providers() -> dict[str, ProviderSettings]:
    return {
        "gemini": ProviderSettings(
            api_key_env="GEMINI_API_KEY",
            default_model="gemini-3.1-flash-lite-preview",
        ),
        "openai": ProviderSettings(
            api_key_env="OPENAI_API_KEY",
            default_model="gpt-4o",
        ),
        "anthropic": ProviderSettings(
            api_key_env="ANTHROPIC_API_KEY",
            default_model="claude-sonnet-4-20250514",
        ),
        "openai-compatible": ProviderSettings(
            api_key_env="",
            default_model="",
            base_url="",
            api_mode="chat_completions",
            api_key_required=False,
        ),
    }


class ForgeConfig(BaseModel):
    """Top-level configuration composing all sections."""

    agent: AgentSettings = Field(default_factory=AgentSettings)
    context: ContextSettings = Field(default_factory=ContextSettings)
    routing: RoutingSettings = Field(default_factory=RoutingSettings)
    mcp: MCPSettings = Field(default_factory=MCPSettings)
    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)
    queue: QueueSettings = Field(default_factory=QueueSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    service: ServiceSettings = Field(default_factory=ServiceSettings)
    model_profiles: dict[str, ModelProfileSettings] = Field(default_factory=dict)
    providers: dict[str, ProviderSettings] = Field(default_factory=_default_providers)


# ---------------------------------------------------------------------------
# TOML Loading
# ---------------------------------------------------------------------------


def _load_toml(path: Path) -> dict[str, Any]:
    """Load a TOML file and return its contents as a dict.

    Returns an empty dict if the file does not exist or is malformed.
    """
    if not path.is_file():
        return {}
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (tomllib.TOMLDecodeError, OSError):
        return {}


# ---------------------------------------------------------------------------
# Deep Merge
# ---------------------------------------------------------------------------


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge *override* into *base*, returning a new dict.

    - Nested dicts are merged recursively.
    - All other values in *override* replace those in *base*.
    """
    merged = base.copy()
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


# ---------------------------------------------------------------------------
# Environment Variable Override
# ---------------------------------------------------------------------------

_SECTION_MODELS: dict[str, type[BaseModel]] = {
    "agent": AgentSettings,
    "context": ContextSettings,
    "routing": RoutingSettings,
    "mcp": MCPSettings,
    "sandbox": SandboxSettings,
    "queue": QueueSettings,
    "logging": LoggingSettings,
    "service": ServiceSettings,
}


_PROVIDER_ENV_ALIASES: tuple[tuple[str, str], ...] = (
    ("OPENAI_COMPATIBLE_", "openai-compatible"),
    ("GEMINI_", "gemini"),
    ("OPENAI_", "openai"),
    ("ANTHROPIC_", "anthropic"),
)


def _coerce_value(field_type: object, raw: str) -> Any:
    """Coerce a raw string env value to the target field type."""
    origin = get_origin(field_type)
    if field_type is bool or (origin in (Union, UnionType) and bool in get_args(field_type)):
        return raw.lower() in ("true", "1", "yes")
    if field_type is int or (origin in (Union, UnionType) and int in get_args(field_type)):
        return None if raw.strip().lower() in ("", "none", "null") else int(raw)
    if field_type is float or (origin in (Union, UnionType) and float in get_args(field_type)):
        return None if raw.strip().lower() in ("", "none", "null") else float(raw)
    if origin is list:
        return [item.strip() for item in raw.split(",") if item.strip()]
    return raw


def _apply_provider_env_override(
    remainder: str,
    env_value: str,
    result: dict[str, Any],
) -> None:
    if not remainder.startswith("PROVIDERS_"):
        return
    sub = remainder[len("PROVIDERS_") :]
    for prefix, provider_name in _PROVIDER_ENV_ALIASES:
        if not sub.startswith(prefix):
            continue
        field_name = sub[len(prefix) :].lower()
        if field_name not in ProviderSettings.model_fields:
            return
        field_type = ProviderSettings.model_fields[field_name].annotation
        if field_type is None:
            return
        result.setdefault("providers", {}).setdefault(provider_name, {})[field_name] = (
            _coerce_value(field_type, env_value)
        )
        return


def _collect_env_overrides() -> dict[str, Any]:
    """Scan environment for AGENT_FORGE_{SECTION}_{KEY} variables.

    Returns a nested dict suitable for deep-merge, e.g.::

        {"agent": {"max_iterations": 10}, "sandbox": {"memory_limit": "1g"}}
    """
    result: dict[str, Any] = {}

    for env_key, env_value in os.environ.items():
        if not env_key.startswith(f"{ENV_PREFIX}_"):
            continue

        # Strip prefix and split: AGENT_FORGE_AGENT_MAX_ITERATIONS -> [AGENT, MAX_ITERATIONS]
        remainder = env_key[len(ENV_PREFIX) + 1 :]  # "AGENT_MAX_ITERATIONS"
        if remainder.startswith("PROVIDERS_"):
            _apply_provider_env_override(remainder, env_value, result)
            continue

        # Try to match the first token as a known section
        matched_section: str | None = None
        field_name = ""
        for section_name in _SECTION_MODELS:
            prefix_upper = section_name.upper() + "_"
            if remainder.startswith(prefix_upper):
                matched_section = section_name
                field_name = remainder[len(prefix_upper) :].lower()
                break
        if matched_section is None:
            continue

        # Validate the field exists in the model
        model_cls = _SECTION_MODELS[matched_section]
        if field_name not in model_cls.model_fields:
            continue

        # Coerce to the correct type
        field_info = model_cls.model_fields[field_name]
        field_type = field_info.annotation
        if field_type is None:
            continue

        coerced = _coerce_value(field_type, env_value)

        result.setdefault(matched_section, {})[field_name] = coerced

    return result


# ---------------------------------------------------------------------------
# CLI Override Mapping
# ---------------------------------------------------------------------------


def _flatten_cli_overrides(cli_overrides: dict[str, Any]) -> dict[str, Any]:
    """Convert flat CLI override keys to nested dict.

    Accepts two formats:
    - Dotted keys: ``{"agent.max_iterations": 10}``
    - Already nested: ``{"agent": {"max_iterations": 10}}``
    """
    result: dict[str, Any] = {}

    for key, value in cli_overrides.items():
        if value is None:
            continue  # Skip unset CLI options

        if "." in key:
            parts = key.split(".", 1)
            section, field = parts[0], parts[1]
            result.setdefault(section, {})[field] = value
        elif isinstance(value, dict):
            result[key] = value
        else:
            # Top-level scalar — try to place in 'agent' section by convention
            result.setdefault("agent", {})[key] = value

    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_config(
    cli_overrides: dict[str, Any] | None = None,
    *,
    project_path: Path | None = None,
    user_path: Path | None = None,
) -> ForgeConfig:
    """Load and merge configuration from all sources.

    Precedence (highest first):
        1. *cli_overrides* — dict from Click CLI flags
        2. Environment variables — ``AGENT_FORGE_{SECTION}_{KEY}``
        3. Project config — ``./agent-forge.toml`` (or *project_path*)
        4. User config — ``~/.agent-forge/config.toml`` (or *user_path*)
        5. Built-in defaults — Pydantic model defaults

    Args:
        cli_overrides: Flat or dotted-key dict of CLI flag values.
        project_path: Override path for project config (testing).
        user_path: Override path for user config (testing).

    Returns:
        Fully resolved ``ForgeConfig``.
    """
    # Layer 5: Built-in defaults (preserve default provider entries across TOML overrides)
    merged: dict[str, Any] = {
        "providers": {name: cfg.model_dump() for name, cfg in _default_providers().items()}
    }

    # Layer 4: User config
    _user_path = user_path if user_path is not None else USER_CONFIG_PATH
    user_data = _load_toml(_user_path)
    merged = _deep_merge(merged, user_data)

    # Layer 3: Project config
    _project_path = (
        project_path if project_path is not None else Path.cwd() / PROJECT_CONFIG_FILENAME
    )
    project_data = _load_toml(_project_path)
    merged = _deep_merge(merged, project_data)

    # Layer 2: Environment variables
    env_data = _collect_env_overrides()
    merged = _deep_merge(merged, env_data)

    # Layer 1: CLI flags
    if cli_overrides:
        cli_data = _flatten_cli_overrides(cli_overrides)
        merged = _deep_merge(merged, cli_data)

    return ForgeConfig(**merged)
