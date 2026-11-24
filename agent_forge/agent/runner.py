"""Shared agent composition root and sandbox execution helpers.

Provides a single runner code path shared by the CLI and the hosted service
so sandbox configuration, system prompt rendering, ReAct execution, post-run
hooks, and cleanup stay uniform across entry points.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from agent_forge.agent.context import ContextEngine, ContextPolicy
from agent_forge.agent.models import AgentConfig, AgentRun
from agent_forge.agent.prompts import build_hosted_system_prompt, build_system_prompt
from agent_forge.config import ContextSettings
from agent_forge.extensions.discovery import discover_extension_prompt_fragments
from agent_forge.sandbox.base import SandboxConfig
from agent_forge.sandbox.toolchain import resolve_sandbox_image

if TYPE_CHECKING:
    from pathlib import Path

    from agent_forge.config import ForgeConfig
    from agent_forge.llm.base import LLMProvider
    from agent_forge.orchestration.events import EventBus
    from agent_forge.sandbox.base import Sandbox
    from agent_forge.tools.base import ToolRegistry

PostRunHook = Callable[["AgentRun", "LLMProvider", "ToolRegistry", "Sandbox"], Awaitable[None]]


def build_sandbox_config(
    cfg: ForgeConfig,
    *,
    repo_path: str | Path | None = None,
) -> SandboxConfig:
    """Convert resolved application settings into a :class:`SandboxConfig`."""
    raw_toolchain = getattr(cfg.sandbox, "toolchain", None)
    toolchain = raw_toolchain if isinstance(raw_toolchain, str) and raw_toolchain else "auto"
    raw_ports = getattr(cfg.sandbox, "published_ports", None)
    published_ports = list(raw_ports) if isinstance(raw_ports, (list, tuple)) else []
    resolved_image, resolved_toolchain = resolve_sandbox_image(
        cfg.sandbox.image,
        toolchain=toolchain,
        repo_path=repo_path,
    )
    return SandboxConfig(
        backend=cfg.sandbox.backend,
        image=resolved_image,
        toolchain=resolved_toolchain,
        cpu_limit=cfg.sandbox.cpu_limit,
        memory_limit=cfg.sandbox.memory_limit,
        timeout_seconds=cfg.sandbox.timeout_seconds,
        network_enabled=cfg.sandbox.network_enabled,
        writable_cache_mounts=cfg.sandbox.writable_cache_mounts,
        published_ports=published_ports,
    )


def _sync_context_engine_window(
    engine: ContextEngine, cfg: Any, agent_config: AgentConfig | None
) -> None:
    if not engine.policy.auto_size_context_window:
        return
    from agent_forge.llm.capabilities import resolve_capabilities

    agent_cfg = getattr(cfg, "agent", None)
    raw_provider = getattr(agent_cfg, "default_provider", None)
    raw_model = getattr(agent_cfg, "default_model", None)
    provider_name = (
        agent_config.provider
        if agent_config is not None
        else (raw_provider if isinstance(raw_provider, str) else "")
    )
    model_name = (
        agent_config.model
        if agent_config is not None
        else (raw_model if isinstance(raw_model, str) else "")
    )
    if not provider_name:
        return
    providers = getattr(cfg, "providers", None)
    provider_cfg = providers.get(provider_name) if isinstance(providers, dict) else None
    raw_mode = getattr(provider_cfg, "api_mode", None)
    raw_url = getattr(provider_cfg, "base_url", None)
    raw_caps = getattr(provider_cfg, "capabilities", None)
    caps = resolve_capabilities(
        provider_name,
        model_name,
        api_mode=raw_mode if isinstance(raw_mode, str) else None,
        base_url=raw_url if isinstance(raw_url, str) and raw_url else None,
        overrides=dict(raw_caps) if isinstance(raw_caps, dict) and raw_caps else None,
    )
    engine.sync_context_window(caps.max_context_tokens)


def _resolve_context_engine_for_config(
    repo_path: str | Path,
    task: str,
    cfg: Any,
    agent_config: AgentConfig | None = None,
) -> ContextEngine | None:
    ctx_cfg = getattr(cfg, "context", None)
    if isinstance(ctx_cfg, ContextSettings):
        if not ctx_cfg.enabled:
            if agent_config is not None:
                agent_config.context_enabled = False
            return None
        engine = ContextEngine(
            repo_path=repo_path,
            task=task,
            policy=ContextPolicy.from_settings(ctx_cfg),
        )
        _sync_context_engine_window(engine, cfg, agent_config)
        return engine
    return ContextEngine(repo_path=repo_path, task=task)


def render_task_system_prompt(
    task: str,
    tools: ToolRegistry,
    sandbox_config: SandboxConfig,
    *,
    repo_path: str | Path | None = None,
    context_engine: ContextEngine | None = None,
    prompt_scope: str | None = None,
    report_schema: str | None = None,
    discover_extensions: bool = True,
) -> str:
    """Render either the hosted or local system prompt for a task."""
    extension_prompts = (
        discover_extension_prompt_fragments() or None if discover_extensions else None
    )
    active_context = context_engine
    if active_context is None and repo_path is not None:
        active_context = ContextEngine(repo_path=repo_path, task=task)
    repo_instructions: str | None = None
    repo_map: str | None = None
    scoped_instructions: str | None = None
    if active_context is not None:
        instructions, repo_map_obj = active_context.prepare_initial_context()
        repo_instructions = instructions.rendered or None
        repo_map = repo_map_obj.rendered or None
        scoped_instructions = active_context.task_scoped_instructions_rendered or None

    tool_definitions = tools.list_definitions()
    if report_schema is not None:
        return build_hosted_system_prompt(
            task,
            tool_definitions,
            report_schema=report_schema,
            sandbox_backend=sandbox_config.backend,
            sandbox_image=sandbox_config.image,
            network_enabled=sandbox_config.network_enabled,
            command_timeout_seconds=sandbox_config.timeout_seconds,
            published_ports=sandbox_config.published_ports or None,
            repo_instructions=repo_instructions,
            repo_map=repo_map,
            scoped_instructions=scoped_instructions,
            prompt_scope=prompt_scope,
            extension_prompts=extension_prompts,
        )
    return build_system_prompt(
        task,
        tool_definitions,
        sandbox_backend=sandbox_config.backend,
        sandbox_image=sandbox_config.image,
        network_enabled=sandbox_config.network_enabled,
        command_timeout_seconds=sandbox_config.timeout_seconds,
        published_ports=sandbox_config.published_ports or None,
        repo_instructions=repo_instructions,
        repo_map=repo_map,
        scoped_instructions=scoped_instructions,
        prompt_scope=prompt_scope,
        extension_prompts=extension_prompts,
    )


def _build_react_loop_kwargs(
    *,
    event_bus: EventBus | None,
    verify: Any,
    approval_gate: Any,
    on_checkpoint: Any,
    emit_lifecycle: bool,
    cancellation_token: Any,
    journal: Any,
    raise_on_cancel: bool,
    base_revision: str,
    verify_command: str | None,
    completion_contract: Any = None,
    context_engine: ContextEngine | None = None,
) -> dict[str, Any]:
    optional = {
        "verify": verify,
        "approval_gate": approval_gate,
        "on_checkpoint": on_checkpoint,
        "cancellation_token": cancellation_token,
        "journal": journal,
        "verify_command": verify_command,
        "completion_contract": completion_contract,
        "context_engine": context_engine,
    }
    loop_kwargs: dict[str, Any] = {
        "event_bus": event_bus,
        **{k: v for k, v in optional.items() if v is not None},
    }
    if not emit_lifecycle:
        loop_kwargs["emit_lifecycle"] = False
    if not raise_on_cancel:
        loop_kwargs["raise_on_cancel"] = False
    if base_revision:
        loop_kwargs["base_revision"] = base_revision
    return loop_kwargs


async def execute_agent_run(
    agent_run: AgentRun,
    sandbox_config: SandboxConfig,
    llm: LLMProvider,
    tools: ToolRegistry,
    sandbox: Sandbox,
    *,
    repo_path: str | None = None,
    event_bus: EventBus | None = None,
    verify: Any = None,
    approval_gate: Any = None,
    on_checkpoint: Any = None,
    emit_lifecycle: bool = True,
    cancellation_token: Any = None,
    journal: Any = None,
    raise_on_cancel: bool = True,
    base_revision: str = "",
    verify_command: str | None = None,
    completion_contract: Any = None,
    context_engine: ContextEngine | None = None,
    on_sandbox_started: Callable[[Sandbox], None] | None = None,
    post_run_hook: PostRunHook | None = None,
    react_loop_fn: Any = None,
) -> AgentRun:
    """Start the sandbox, execute ``react_loop``, run an optional hook, and clean up."""
    if react_loop_fn is None:
        from agent_forge.agent.core import react_loop as default_react_loop

        loop_fn = default_react_loop
    else:
        loop_fn = react_loop_fn

    loop_kwargs = _build_react_loop_kwargs(
        event_bus=event_bus,
        verify=verify,
        approval_gate=approval_gate,
        on_checkpoint=on_checkpoint,
        emit_lifecycle=emit_lifecycle,
        cancellation_token=cancellation_token,
        journal=journal,
        raise_on_cancel=raise_on_cancel,
        base_revision=base_revision,
        verify_command=verify_command,
        completion_contract=completion_contract,
        context_engine=context_engine,
    )

    try:
        await sandbox.start(repo_path=repo_path or agent_run.repo_path, config=sandbox_config)
        if on_sandbox_started is not None:
            on_sandbox_started(sandbox)
        result: AgentRun = await loop_fn(agent_run, llm, tools, sandbox, **loop_kwargs)
        if post_run_hook is not None:
            await post_run_hook(agent_run, llm, tools, sandbox)
        return result
    finally:
        try:
            await sandbox.stop()
        finally:
            await llm.close()


def _create_provider_from_config(
    cfg: ForgeConfig,
    provider_name: str,
    api_key: str,
) -> LLMProvider:
    from agent_forge.llm.factory import create_provider as default_provider_factory

    providers = getattr(cfg, "providers", None)
    provider_cfg = providers.get(provider_name) if isinstance(providers, dict) else None
    kwargs: dict[str, Any] = {}
    if provider_cfg is not None:
        if provider_cfg.base_url:
            kwargs["base_url"] = provider_cfg.base_url
        if provider_name in ("openai", "openai-compatible") and provider_cfg.api_mode != "auto":
            kwargs["api_mode"] = provider_cfg.api_mode
        if provider_cfg.capabilities:
            kwargs["capability_overrides"] = dict(provider_cfg.capabilities)
    return default_provider_factory(provider_name, api_key, **kwargs)


async def _attach_mcp_tools(cfg: Any, tools: ToolRegistry) -> Any:
    """Connect configured MCP servers and register their tools into *tools*."""
    from agent_forge.config import MCPSettings
    from agent_forge.tools.mcp import MCPClientManager

    raw_mcp = getattr(cfg, "mcp", None)
    if not isinstance(raw_mcp, MCPSettings) or not raw_mcp.enabled or not raw_mcp.servers:
        return None
    manager = MCPClientManager.from_settings(raw_mcp)
    await manager.connect_all()
    manager.register_tools(tools)
    return manager


async def run_agent_task(
    task: str,
    repo_path: str,
    cfg: ForgeConfig,
    agent_config: AgentConfig,
    provider_name: str,
    api_key: str,
    *,
    run_id: str | None = None,
    prompt_scope: str | None = None,
    report_schema: str | None = None,
    discover_extensions: bool = True,
    event_bus: EventBus | None = None,
    emit_lifecycle: bool = True,
    cancellation_token: Any = None,
    completion_contract: Any = None,
    context_engine: ContextEngine | None = None,
    approval_gate: Any = None,
    on_sandbox_started: Callable[[Sandbox], None] | None = None,
    post_run_hook: PostRunHook | None = None,
    tools_factory: Callable[[], ToolRegistry] | None = None,
    sandbox_factory: Callable[[SandboxConfig], Sandbox] | None = None,
    provider_factory: Callable[[str, str], LLMProvider] | None = None,
    react_loop_fn: Any = None,
) -> AgentRun:
    """Compose dependencies and execute an agent task via :func:`execute_agent_run`."""
    if tools_factory is None:
        from agent_forge.tools import create_default_registry as default_tools_factory

        tools = default_tools_factory()
    else:
        tools = tools_factory()

    mcp_manager = await _attach_mcp_tools(cfg, tools)
    try:
        active_context = (
            context_engine
            if context_engine is not None
            else _resolve_context_engine_for_config(repo_path, task, cfg, agent_config)
        )
        sandbox_config = build_sandbox_config(cfg, repo_path=repo_path)
        agent_config.system_prompt = render_task_system_prompt(
            task,
            tools,
            sandbox_config,
            repo_path=repo_path,
            context_engine=active_context,
            prompt_scope=prompt_scope,
            report_schema=report_schema,
            discover_extensions=discover_extensions,
        )
        run_kwargs: dict[str, Any] = {
            "task": task,
            "repo_path": repo_path,
            "config": agent_config,
        }
        if run_id is not None:
            run_kwargs["id"] = run_id
        agent_run = AgentRun(**run_kwargs)

        if sandbox_factory is None:
            from agent_forge.sandbox.factory import create_sandbox as default_sandbox_factory

            sandbox = default_sandbox_factory(sandbox_config)
        else:
            sandbox = sandbox_factory(sandbox_config)

        if provider_factory is None:
            from agent_forge.config import RoutingSettings
            from agent_forge.llm.router import RoutingLLMProvider

            routing_cfg = getattr(cfg, "routing", None)
            if isinstance(routing_cfg, RoutingSettings) and routing_cfg.enabled:
                llm: LLMProvider = RoutingLLMProvider.from_config(cfg)
            else:
                llm = _create_provider_from_config(cfg, provider_name, api_key)
        else:
            llm = provider_factory(provider_name, api_key)

        return await execute_agent_run(
            agent_run,
            sandbox_config,
            llm,
            tools,
            sandbox,
            event_bus=event_bus,
            emit_lifecycle=emit_lifecycle,
            cancellation_token=cancellation_token,
            completion_contract=completion_contract,
            approval_gate=approval_gate,
            context_engine=active_context,
            on_sandbox_started=on_sandbox_started,
            post_run_hook=post_run_hook,
            react_loop_fn=react_loop_fn,
        )
    finally:
        if mcp_manager is not None:
            await mcp_manager.close()
