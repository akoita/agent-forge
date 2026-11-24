"""Interactive multi-turn CLI chat runtime with warm session sandbox and slash commands."""

from __future__ import annotations

import asyncio
import inspect
import os
import shlex
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from agent_forge.agent.context import ContextAssembly, ContextEngine, estimate_messages_tokens
from agent_forge.agent.models import AgentConfig, AgentRun
from agent_forge.agent.runner import (
    _attach_mcp_tools,
    _create_provider_from_config,
    _prepare_agent_run_for_task,
    _sync_context_engine_window,
    build_sandbox_config,
    execute_agent_run,
)
from agent_forge.observability.cost import build_pricing_overrides

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from agent_forge.agent.session import DevSession, SessionStore
    from agent_forge.config import ForgeConfig
    from agent_forge.llm.base import LLMProvider
    from agent_forge.orchestration.events import EventBus
    from agent_forge.sandbox.base import Sandbox, SandboxConfig
    from agent_forge.tools.base import ToolRegistry


_HELP_TEXT = """Available slash commands:
  /compact [keep_recent]  Compact older conversation turns while keeping working set
  /context                Show token usage, compaction stats, active files, and warm sandbox status
  /model [spec]           Show or switch active model (model, provider/model, or profile)
  /clear [--sandbox]      Reset conversation history and optionally restart the warm sandbox
  /help                   Show this help message
  /exit, /quit            Save session state, stop the warm sandbox, and exit"""


@dataclass
class SlashCommandResult:
    """Outcome from handling a local interactive slash command."""

    handled: bool
    output: str = ""
    should_exit: bool = False
    payload: dict[str, Any] = field(default_factory=dict)


def _extract_reply(run: AgentRun) -> str:
    for msg in reversed(run.messages):
        if msg.role.value == "assistant" and msg.content:
            return msg.content
    return ""


def _resolve_default_api_key(cfg: ForgeConfig, provider_name: str) -> str:
    from agent_forge.llm.qualification import is_api_key_required

    provider_cfg = cfg.providers.get(provider_name)
    if provider_cfg is None:
        return ""
    api_key = os.environ.get(provider_cfg.api_key_env, "") if provider_cfg.api_key_env else ""
    if is_api_key_required(cfg, provider_name, base_url=provider_cfg.base_url) and not api_key:
        msg = (
            f"Missing API key: set {provider_cfg.api_key_env} "
            f"environment variable for provider '{provider_name}'"
        )
        raise ValueError(msg)
    return api_key


def _agent_config_from_runtime(
    cfg: ForgeConfig,
    session: DevSession,
    *,
    approval_mode: str,
    profile: Any | None = None,
) -> AgentConfig:
    provider = session.provider or cfg.agent.default_provider
    model = session.model or cfg.agent.default_model
    raw_effort = getattr(cfg.agent, "reasoning_effort", None)
    raw_mode = getattr(cfg.agent, "thinking_mode", None)
    raw_budget = getattr(cfg.agent, "thinking_budget_tokens", None)
    raw_level = getattr(cfg.agent, "thinking_level", None)
    return AgentConfig(
        model=model,
        provider=provider,
        max_iterations=cfg.agent.max_iterations,
        max_tokens_per_run=cfg.agent.max_tokens_per_run,
        temperature=cfg.agent.temperature,
        profile_id=profile.id if profile is not None else session.profile_id,
        approval_mode=approval_mode,
        max_cost_usd=cfg.agent.max_cost_usd,
        max_duration_seconds=cfg.agent.max_duration_seconds,
        budget_action=cfg.agent.budget_action,
        context_enabled=cfg.context.enabled,
        pricing_overrides=build_pricing_overrides(cfg),
        reasoning_effort=raw_effort if isinstance(raw_effort, str) else None,
        thinking_mode=raw_mode if isinstance(raw_mode, str) else None,
        thinking_budget_tokens=(
            raw_budget if isinstance(raw_budget, int) and not isinstance(raw_budget, bool) else None
        ),
        thinking_level=raw_level if isinstance(raw_level, str) else None,
    )


class ChatSessionRuntime:
    """Manage a multi-turn CLI session with a warm session-scoped sandbox."""

    def __init__(
        self,
        cfg: ForgeConfig,
        session: DevSession,
        session_store: SessionStore,
        *,
        approval_mode: str = "auto",
        approval_gate: Any = None,
        profile: Any | None = None,
        prompt_scope: str | None = None,
        idle_timeout_seconds: float | None = 1800.0,
        clock: Callable[[], float] | None = None,
        sandbox_factory: Callable[[SandboxConfig], Sandbox] | None = None,
        provider_factory: Callable[[ForgeConfig, str, str], LLMProvider] | None = None,
        tools_factory: Callable[[], ToolRegistry] | None = None,
        react_loop_fn: Any = None,
        api_key_resolver: Callable[[ForgeConfig, str], str] | None = None,
    ) -> None:
        self.cfg = cfg.model_copy(deep=True)
        self.session = session
        self.session_store = session_store
        self.approval_mode = approval_mode
        self.approval_gate = approval_gate
        self.profile = profile
        self.prompt_scope = prompt_scope
        self.idle_timeout_seconds = idle_timeout_seconds
        self._clock = clock or time.monotonic
        self._sandbox_factory = sandbox_factory
        self._provider_factory = provider_factory
        self._tools_factory = tools_factory
        self._react_loop_fn = react_loop_fn
        self._api_key_resolver = api_key_resolver or _resolve_default_api_key

        self.cfg.agent.default_provider = self.session.provider
        self.cfg.agent.default_model = self.session.model

        self._sandbox: Sandbox | None = None
        self._sandbox_config: SandboxConfig | None = None
        self._last_activity_at: float | None = None
        self._tools: ToolRegistry | None = None
        self._mcp_manager: Any = None

        self.context_engine: ContextEngine = self.session.build_context_engine()
        agent_cfg = _agent_config_from_runtime(
            self.cfg,
            self.session,
            approval_mode=self.approval_mode,
            profile=self.profile,
        )
        _sync_context_engine_window(self.context_engine, self.cfg, agent_cfg)

    @property
    def sandbox(self) -> Sandbox | None:
        """Currently attached warm sandbox instance, if started."""
        return self._sandbox

    def _is_idle_expired(self) -> bool:
        if self.idle_timeout_seconds is None or self.idle_timeout_seconds <= 0:
            return False
        if self._last_activity_at is None:
            return False
        return (self._clock() - self._last_activity_at) > self.idle_timeout_seconds

    async def _ensure_tools(self) -> ToolRegistry:
        if self._tools is not None:
            return self._tools
        if self._tools_factory is None:
            from agent_forge.tools import create_default_registry

            tools = create_default_registry()
        else:
            tools = self._tools_factory()
        self._mcp_manager = await _attach_mcp_tools(self.cfg, tools)
        self._tools = tools
        return tools

    async def _ensure_sandbox(self) -> tuple[Sandbox, SandboxConfig]:
        if self._sandbox is not None:
            expired = self._is_idle_expired()
            alive = False if expired else await self._sandbox.is_alive()
            if expired or not alive:
                await self.close_sandbox()

        if self._sandbox is not None and self._sandbox_config is not None:
            return self._sandbox, self._sandbox_config

        sandbox_cfg = build_sandbox_config(self.cfg, repo_path=self.session.repo_path)
        if self._sandbox_factory is None:
            from agent_forge.sandbox.factory import create_sandbox

            sandbox = create_sandbox(sandbox_cfg)
        else:
            sandbox = self._sandbox_factory(sandbox_cfg)
        await sandbox.start(repo_path=self.session.repo_path, config=sandbox_cfg)
        self._sandbox = sandbox
        self._sandbox_config = sandbox_cfg
        self.session.sandbox_backend = sandbox_cfg.backend
        self.session.sandbox_image = sandbox_cfg.image
        self._last_activity_at = self._clock()
        return sandbox, sandbox_cfg

    def _build_llm(self, provider_name: str) -> LLMProvider:
        from agent_forge.config import RoutingSettings
        from agent_forge.llm.router import RoutingLLMProvider

        routing_cfg = getattr(self.cfg, "routing", None)
        if isinstance(routing_cfg, RoutingSettings) and routing_cfg.enabled:
            return RoutingLLMProvider.from_config(self.cfg)
        api_key = self._api_key_resolver(self.cfg, provider_name)
        if self._provider_factory is not None:
            return self._provider_factory(self.cfg, provider_name, api_key)
        return _create_provider_from_config(self.cfg, provider_name, api_key)

    async def close_sandbox(self) -> None:
        """Stop the warm sandbox and cancel any active terminal sessions inside it."""
        sandbox = self._sandbox
        self._sandbox = None
        self._sandbox_config = None
        self._last_activity_at = None
        if sandbox is None:
            return
        try:
            await sandbox.close_all_sessions()
        finally:
            await sandbox.stop()

    async def close(self) -> None:
        """Persist session state and tear down warm sandbox and MCP connections."""
        try:
            await self.close_sandbox()
        finally:
            try:
                if self._mcp_manager is not None:
                    await self._mcp_manager.close()
                    self._mcp_manager = None
            finally:
                self.session.capture_context_engine(self.context_engine)
                self.session_store.save(self.session)

    async def run_turn(
        self,
        prompt: str,
        *,
        event_bus: EventBus | None = None,
        cancellation_token: Any = None,
        verify_command: str | None = None,
        on_sandbox_started: Callable[[Sandbox], None] | None = None,
    ) -> AgentRun:
        """Execute a single conversational turn using the warm session sandbox."""
        tools = await self._ensure_tools()
        sandbox, sandbox_cfg = await self._ensure_sandbox()

        agent_cfg = _agent_config_from_runtime(
            self.cfg,
            self.session,
            approval_mode=self.approval_mode,
            profile=self.profile,
        )
        _sync_context_engine_window(self.context_engine, self.cfg, agent_cfg)
        scope = self.prompt_scope or (self.profile.prompt_scope if self.profile else None)

        agent_run = _prepare_agent_run_for_task(
            prompt,
            self.session.repo_path,
            tools,
            sandbox_cfg,
            agent_cfg,
            run_id=None,
            active_context=self.context_engine,
            prompt_scope=scope,
            report_schema=None,
            discover_extensions=True,
            dev_session=self.session,
        )
        llm = self._build_llm(agent_cfg.provider)
        result_run = await execute_agent_run(
            agent_run,
            sandbox_cfg,
            llm,
            tools,
            sandbox,
            repo_path=self.session.repo_path,
            event_bus=event_bus,
            approval_gate=self.approval_gate,
            cancellation_token=cancellation_token,
            verify_command=verify_command,
            context_engine=self.context_engine,
            on_sandbox_started=on_sandbox_started,
            react_loop_fn=self._react_loop_fn,
            manage_sandbox=False,
        )
        self.session.record_turn(
            prompt=prompt,
            run=result_run,
            context_engine=self.context_engine,
        )
        self.session_store.save(self.session)
        self._last_activity_at = self._clock()
        return result_run

    def compact(self, *, keep_recent_turns: int | None = None) -> ContextAssembly:
        """Deterministically run two-stage compaction on the stored session history."""
        agent_cfg = _agent_config_from_runtime(
            self.cfg,
            self.session,
            approval_mode=self.approval_mode,
            profile=self.profile,
        )
        task_label = self.session.title or (
            self.session.turns[0].prompt if self.session.turns else ""
        )
        run_view = AgentRun(
            task=task_label,
            repo_path=self.session.repo_path,
            config=agent_cfg,
            messages=list(self.session.messages),
            tool_invocations=list(self.session.tool_invocations),
        )
        tool_defs = self._tools.list_definitions() if self._tools is not None else ()
        assembly = self.context_engine.force_compact(
            run_view,
            tool_definitions=tool_defs,
            keep_recent_turns=keep_recent_turns,
        )
        self.session.capture_context_engine(self.context_engine)
        self.session_store.save(self.session)
        return assembly

    def context_budget_summary(self) -> dict[str, Any]:
        """Return live context token usage, compaction state, and session diagnostics."""
        raw_tokens = estimate_messages_tokens(self.session.messages)
        assembled_tokens = self.context_engine.assembled_token_estimate(self.session.messages)
        stats = self.context_engine.stats_dict()
        policy = self.context_engine.policy
        return {
            "session_id": self.session.id,
            "provider": self.session.provider,
            "model": self.session.model,
            "turns": len(self.session.turns),
            "messages": len(self.session.messages),
            "raw_tokens": raw_tokens,
            "assembled_tokens": assembled_tokens,
            "trigger_tokens": policy.trigger_tokens,
            "target_tokens": policy.target_tokens,
            "max_tokens": policy.context_window_tokens,
            "sandbox_warm": self._sandbox is not None,
            **stats,
        }

    def switch_model(self, spec: str) -> tuple[str, str]:
        """Switch provider/model using ``provider/model``, a model profile, or bare model name."""
        cleaned = spec.strip()
        if not cleaned:
            msg = "Model specification cannot be empty"
            raise ValueError(msg)

        new_provider = self.session.provider
        new_model = cleaned

        if "/" in cleaned:
            candidate_provider, candidate_model = cleaned.split("/", 1)
            candidate_provider = candidate_provider.strip()
            candidate_model = candidate_model.strip()
            if not candidate_provider or not candidate_model:
                msg = f"Invalid model specification '{spec}' (expected provider/model)"
                raise ValueError(msg)
            if candidate_provider not in self.cfg.providers:
                msg = f"Unknown provider '{candidate_provider}' in '{spec}'"
                raise ValueError(msg)
            new_provider = candidate_provider
            new_model = candidate_model
        elif cleaned in self.cfg.model_profiles:
            selected = self.cfg.model_profiles[cleaned]
            new_provider = selected.provider
            new_model = selected.model

        self.session.provider = new_provider
        self.session.model = new_model
        self.cfg.agent.default_provider = new_provider
        self.cfg.agent.default_model = new_model
        agent_cfg = _agent_config_from_runtime(
            self.cfg,
            self.session,
            approval_mode=self.approval_mode,
            profile=self.profile,
        )
        _sync_context_engine_window(self.context_engine, self.cfg, agent_cfg)
        self.session.capture_context_engine(self.context_engine)
        self.session_store.save(self.session)
        return new_provider, new_model

    async def clear_session(self, *, reset_sandbox: bool = False) -> None:
        """Reset conversation history while keeping the session ID and repo."""
        self.session.messages.clear()
        self.session.tool_invocations.clear()
        self.session.turns.clear()
        self.session.system_prompt = None
        self.session.context_state = {}
        self.session.total_tokens.prompt_tokens = 0
        self.session.total_tokens.completion_tokens = 0
        self.session.total_tokens.total_tokens = 0
        self.session.total_tokens.cached_prompt_tokens = 0
        self.session.total_tokens.reasoning_tokens = 0

        self.context_engine = self.session.build_context_engine()
        agent_cfg = _agent_config_from_runtime(
            self.cfg,
            self.session,
            approval_mode=self.approval_mode,
            profile=self.profile,
        )
        _sync_context_engine_window(self.context_engine, self.cfg, agent_cfg)
        self.session.capture_context_engine(self.context_engine)
        self.session_store.save(self.session)
        if reset_sandbox:
            await self.close_sandbox()

    async def _handle_compact_command(self, args: list[str]) -> SlashCommandResult:
        keep_recent: int | None = None
        if args:
            try:
                keep_recent = int(args[0])
            except ValueError:
                return SlashCommandResult(
                    handled=True,
                    output=f"Usage: /compact [keep_recent] (got invalid integer '{args[0]}')",
                )
            if keep_recent < 0:
                return SlashCommandResult(
                    handled=True,
                    output="Usage: /compact [keep_recent] (must be non-negative)",
                )
        assembly = self.compact(keep_recent_turns=keep_recent)
        msg = (
            f"Compacted context (epoch {assembly.compaction_epoch}): "
            f"{assembly.raw_prompt_tokens:,} -> {assembly.assembled_prompt_tokens:,} tokens "
            f"(saved {assembly.tokens_saved:,})."
        )
        return SlashCommandResult(
            handled=True,
            output=msg,
            payload={
                "epoch": assembly.compaction_epoch,
                "raw_prompt_tokens": assembly.raw_prompt_tokens,
                "assembled_prompt_tokens": assembly.assembled_prompt_tokens,
                "tokens_saved": assembly.tokens_saved,
                "compacted_this_turn": assembly.compacted_this_turn,
            },
        )

    async def _handle_context_command(self) -> SlashCommandResult:
        summary = self.context_budget_summary()
        active_pty = 0
        if self._sandbox is not None and await self._sandbox.is_alive():
            active_pty = len(await self._sandbox.list_sessions())
        summary["active_terminal_sessions"] = active_pty

        pct = (
            round(100.0 * summary["assembled_tokens"] / summary["max_tokens"], 1)
            if summary["max_tokens"] > 0
            else 0.0
        )
        sandbox_state = (
            f"warm ({active_pty} active PTY)" if summary["sandbox_warm"] else "idle (not started)"
        )
        lines = [
            f"Session: {summary['session_id']} ({summary['provider']}/{summary['model']})",
            (
                f"Context: {summary['assembled_tokens']:,} / {summary['max_tokens']:,} tokens "
                f"({pct}%, raw={summary['raw_tokens']:,}, "
                f"trigger={summary['trigger_tokens']:,}, target={summary['target_tokens']:,})"
            ),
            (
                f"Turns: {summary['turns']}, Messages: {summary['messages']}, "
                f"Compactions: {summary['compactions']}, Saved: {summary['tokens_saved']:,}"
            ),
            (
                f"Working set files: {len(summary['working_set_files'])}, "
                f"Modified: {len(summary['modified_files'])}"
            ),
            f"Sandbox: {sandbox_state}",
        ]
        return SlashCommandResult(handled=True, output="\n".join(lines), payload=summary)

    async def _handle_model_command(self, args: list[str]) -> SlashCommandResult:
        if not args:
            return SlashCommandResult(
                handled=True,
                output=f"Current model: {self.session.provider}/{self.session.model}",
                payload={"provider": self.session.provider, "model": self.session.model},
            )
        try:
            provider, model = self.switch_model(args[0])
        except ValueError as exc:
            return SlashCommandResult(handled=True, output=f"Model switch failed: {exc}")
        return SlashCommandResult(
            handled=True,
            output=f"Switched model to {provider}/{model}",
            payload={"provider": provider, "model": model},
        )

    async def handle_slash_command(self, line: str) -> SlashCommandResult:
        """Dispatch a ``/...`` slash command locally without invoking the model."""
        stripped = line.strip()
        if not stripped.startswith("/"):
            return SlashCommandResult(handled=False)

        try:
            parts = shlex.split(stripped)
        except ValueError:
            parts = stripped.split()
        cmd = parts[0].lower()
        args = parts[1:]

        if cmd in {"/exit", "/quit"}:
            return SlashCommandResult(
                handled=True,
                output="Ending chat session.",
                should_exit=True,
            )
        if cmd == "/help":
            return SlashCommandResult(handled=True, output=_HELP_TEXT)
        if cmd == "/compact":
            return await self._handle_compact_command(args)
        if cmd == "/context":
            return await self._handle_context_command()
        if cmd == "/model":
            return await self._handle_model_command(args)
        if cmd == "/clear":
            reset_sandbox = "--sandbox" in args
            await self.clear_session(reset_sandbox=reset_sandbox)
            suffix = " and stopped warm sandbox" if reset_sandbox else ""
            return SlashCommandResult(
                handled=True,
                output=f"Cleared conversation history{suffix}.",
            )
        return SlashCommandResult(
            handled=True,
            output=f"Unknown command: {cmd}. Type /help for available commands.",
        )


async def _read_repl_line(
    prompt_text: str,
    input_fn: Callable[[str], str | Awaitable[str]],
) -> str | None:
    try:
        raw = input_fn(prompt_text)
        if inspect.isawaitable(raw):
            return str(await raw)
        return str(raw)
    except (EOFError, KeyboardInterrupt):
        return None


async def _default_stdin_reader(prompt_text: str) -> str:
    return await asyncio.to_thread(input, prompt_text)


async def run_chat_repl(
    runtime: ChatSessionRuntime,
    *,
    input_fn: Callable[[str], str | Awaitable[str]] | None = None,
    output_fn: Callable[[str], None] | None = None,
    prompt_text: str = "you> ",
) -> DevSession:
    """Run an interactive terminal loop over *runtime* until ``/exit`` or EOF."""
    reader = input_fn if input_fn is not None else _default_stdin_reader
    writer = output_fn or print

    writer(
        f"Session {runtime.session.id} ({runtime.session.provider}/{runtime.session.model}) "
        f"in {runtime.session.repo_path}. Type /help for commands."
    )
    try:
        while True:
            line = await _read_repl_line(prompt_text, reader)
            if line is None:
                break
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("/"):
                cmd_res = await runtime.handle_slash_command(stripped)
                if cmd_res.output:
                    writer(cmd_res.output)
                if cmd_res.should_exit:
                    break
                continue
            result_run = await runtime.run_turn(stripped)
            reply = _extract_reply(result_run)
            if reply:
                writer(reply)
            elif result_run.error:
                writer(f"[{result_run.state.value}] {result_run.error}")
            else:
                writer(f"[{result_run.state.value}]")
    finally:
        await runtime.close()
    return runtime.session
