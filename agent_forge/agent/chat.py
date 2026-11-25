"""Interactive multi-turn CLI chat runtime with warm session sandbox and slash commands."""

from __future__ import annotations

import asyncio
import inspect
import os
import shlex
import shutil
import subprocess
import sys
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_forge.agent.approvals import ApprovalDecision, ApprovalGate, ApprovalRequest
from agent_forge.agent.context import ContextAssembly, ContextEngine, estimate_messages_tokens
from agent_forge.agent.models import AgentConfig, AgentRun
from agent_forge.agent.runner import (
    _attach_mcp_tools,
    _create_provider_from_config,
    _prepare_agent_run_for_task,
    _sync_context_engine_window,
    attach_subagent_tool,
    build_sandbox_config,
    execute_agent_run,
)
from agent_forge.agent.workspace_undo import (
    TurnWorkspaceSnapshot,
    build_turn_snapshot,
    capture_workspace_state,
    render_workspace_diff,
    restore_turn_snapshot,
)
from agent_forge.config import USER_CONFIG_DIR
from agent_forge.observability.cost import build_pricing_overrides
from agent_forge.orchestration.events import Event, EventBus, EventType
from agent_forge.orchestration.worktree import WorktreeManager, is_git_repo

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from typing import Literal

    from agent_forge.agent.session import DevSession, SessionStore, SessionSummary
    from agent_forge.config import ForgeConfig
    from agent_forge.llm.base import LLMProvider
    from agent_forge.orchestration.worktree import IntegrationResult
    from agent_forge.sandbox.base import Sandbox, SandboxConfig
    from agent_forge.tools.base import ToolRegistry


_HELP_TEXT = """Available slash commands:
  /compact [keep_recent]  Compact older conversation turns while keeping working set
  /context                Show token usage, mode flags, active files, and warm sandbox status
  /diff [--stat]          Show uncommitted workspace changes in the active repository
  /undo [--force]         Revert the last turn's workspace edits (checks for external edits)
  /subagents              List committed subagent worker branches ready for integration
  /apply <branch> [--strategy merge|rebase]
                          Integrate a committed subagent worker branch into HEAD
  /sessions [--all] [N]   List saved sessions for this repo (or all repos)
  /resume <id|latest>     Switch to another saved session in-place
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
    raw_exec = getattr(cfg.agent, "execution_mode", None)
    raw_diag = getattr(cfg.agent, "post_edit_diagnostics", False)
    return AgentConfig(
        model=model,
        provider=provider,
        max_iterations=cfg.agent.max_iterations,
        max_tokens_per_run=cfg.agent.max_tokens_per_run,
        temperature=cfg.agent.temperature,
        profile_id=profile.id if profile is not None else session.profile_id,
        approval_mode=approval_mode,
        execution_mode=raw_exec if isinstance(raw_exec, str) else "tool_calling",
        post_edit_diagnostics=bool(raw_diag) if isinstance(raw_diag, bool) else False,
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
        self._undo_stack: list[TurnWorkspaceSnapshot] = []

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
        stream_tokens: bool = False,
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

        run_holder: list[AgentRun] = []
        attach_subagent_tool(
            self.cfg,
            tools,
            repo_path=self.session.repo_path,
            sandbox_config=sandbox_cfg,
            agent_config=agent_cfg,
            run_holder=run_holder,
            llm_factory=lambda: self._build_llm(agent_cfg.provider),
            sandbox_factory=self._sandbox_factory,
            event_bus=event_bus,
            approval_gate=self.approval_gate,
            cancellation_token=cancellation_token,
            react_loop_fn=self._react_loop_fn,
        )

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
        run_holder.append(agent_run)
        repo_dir = Path(self.session.repo_path)
        before_state = capture_workspace_state(repo_dir)
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
            stream_tokens=stream_tokens,
        )
        after_state = capture_workspace_state(repo_dir)
        turn_idx = len(self.session.turns)
        snapshot = build_turn_snapshot(turn_idx, before_state, after_state)
        if snapshot is not None:
            self._undo_stack.append(snapshot)
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
        raw_exec = getattr(self.cfg.agent, "execution_mode", "tool_calling")
        exec_mode = raw_exec if isinstance(raw_exec, str) else "tool_calling"
        sub_cfg = getattr(self.cfg, "subagents", None)
        sub_enabled = bool(getattr(sub_cfg, "enabled", False))
        return {
            "session_id": self.session.id,
            "provider": self.session.provider,
            "model": self.session.model,
            "execution_mode": exec_mode,
            "subagents_enabled": sub_enabled,
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
        self._undo_stack.clear()

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
                f"Mode: execution={summary['execution_mode']}, "
                f"subagents={'enabled' if summary['subagents_enabled'] else 'disabled'}"
            ),
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

    def workspace_diff(self, *, stat_only: bool = False) -> dict[str, Any]:
        """Inspect uncommitted Git changes in the active session workspace."""
        return render_workspace_diff(Path(self.session.repo_path), stat_only=stat_only)

    def undo_last_turn(self, *, force: bool = False) -> dict[str, Any]:
        """Revert workspace edits from the latest turn that modified files."""
        if not self._undo_stack:
            return {
                "reverted": False,
                "reason": "empty",
                "output": "Nothing to undo in this session.",
            }
        snapshot = self._undo_stack[-1]
        restored, removed, conflicts = restore_turn_snapshot(
            Path(self.session.repo_path),
            snapshot,
            force=force,
        )
        if conflicts:
            return {
                "reverted": False,
                "reason": "conflict",
                "conflicts": conflicts,
                "output": (
                    "Undo blocked: modified since last turn: "
                    + ", ".join(conflicts)
                    + ". Re-run with /undo --force to overwrite."
                ),
            }
        self._undo_stack.pop()
        self.session.capture_context_engine(self.context_engine)
        self.session_store.save(self.session)
        parts = []
        if restored:
            parts.append(f"restored {', '.join(restored)}")
        if removed:
            parts.append(f"removed {', '.join(removed)}")
        summary = "; ".join(parts) or "no file changes"
        return {
            "reverted": True,
            "turn_index": snapshot.turn_index,
            "restored": restored,
            "removed": removed,
            "remaining_undo_steps": len(self._undo_stack),
            "output": f"Undid turn {snapshot.turn_index + 1} workspace changes ({summary}).",
        }

    def list_stored_sessions(
        self,
        *,
        all_repos: bool = False,
        limit: int | None = 20,
    ) -> list[SessionSummary]:
        """List stored sessions sorted newest-first."""
        repo_filter = None if all_repos else self.session.repo_path
        return self.session_store.list_sessions(repo_path=repo_filter, limit=limit)

    async def switch_session(self, target: str) -> DevSession:
        """Persist the current session and switch to another stored session ID or ``latest``."""
        cleaned = target.strip()
        if not cleaned:
            msg = "Session identifier or 'latest' is required"
            raise ValueError(msg)

        self.session.capture_context_engine(self.context_engine)
        self.session_store.save(self.session)

        if cleaned.lower() == "latest":
            loaded = self.session_store.latest_for_repo(self.session.repo_path)
            if loaded is None:
                msg = f"No saved sessions found for repo: {self.session.repo_path}"
                raise FileNotFoundError(msg)
        else:
            loaded = self.session_store.load(cleaned)

        if Path(loaded.repo_path).resolve() != Path(self.session.repo_path).resolve():
            await self.close_sandbox()

        self.session = loaded
        self.cfg.agent.default_provider = loaded.provider
        self.cfg.agent.default_model = loaded.model
        self._undo_stack.clear()
        self.context_engine = loaded.build_context_engine()
        agent_cfg = _agent_config_from_runtime(
            self.cfg,
            self.session,
            approval_mode=self.approval_mode,
            profile=self.profile,
        )
        _sync_context_engine_window(self.context_engine, self.cfg, agent_cfg)
        return loaded

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

    async def _handle_diff_command(self, args: list[str]) -> SlashCommandResult:
        stat_only = "--stat" in args
        result = self.workspace_diff(stat_only=stat_only)
        return SlashCommandResult(handled=True, output=str(result["output"]), payload=result)

    async def _handle_undo_command(self, args: list[str]) -> SlashCommandResult:
        force = "--force" in args
        result = self.undo_last_turn(force=force)
        return SlashCommandResult(handled=True, output=str(result["output"]), payload=result)

    def _subagent_branch_prefix(self) -> str:
        sub_cfg = getattr(self.cfg, "subagents", None)
        raw = getattr(sub_cfg, "branch_prefix", "agent-forge/subagent")
        cleaned = str(raw).strip().rstrip("/")
        return cleaned or "agent-forge/subagent"

    def list_subagent_branches(self) -> list[dict[str, str]]:
        """List committed subagent worker branches in the active repository."""
        repo = self.session.repo_path
        if not is_git_repo(repo):
            return []
        prefix = self._subagent_branch_prefix()
        git_bin = shutil.which("git") or "git"
        proc = subprocess.run(  # noqa: S603
            [
                git_bin,
                "for-each-ref",
                "--sort=-committerdate",
                "--format=%(refname:short)\t%(objectname:short)\t%(subject)",
                f"refs/heads/{prefix}",
            ],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            return []
        branches: list[dict[str, str]] = []
        for line in proc.stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) == 3 and parts[0].strip():
                branches.append(
                    {
                        "branch": parts[0].strip(),
                        "sha": parts[1].strip(),
                        "subject": parts[2].strip(),
                    }
                )
        return branches

    async def apply_subagent_branch(
        self,
        branch: str,
        *,
        strategy: Literal["merge", "rebase"] = "merge",
    ) -> IntegrationResult:
        """Integrate a committed subagent branch into HEAD using ``WorktreeManager``."""
        from agent_forge.orchestration.worktree import IntegrationResult

        prefix = self._subagent_branch_prefix()
        cleaned = branch.strip()
        if not cleaned.startswith(f"{prefix}/"):
            return IntegrationResult(
                run_id=cleaned or "unknown",
                branch=cleaned,
                target_branch="",
                strategy=strategy,
                status="error",
                detail=f"Branch '{cleaned}' must start with '{prefix}/'.",
            )
        if not is_git_repo(self.session.repo_path):
            return IntegrationResult(
                run_id=cleaned,
                branch=cleaned,
                target_branch="",
                strategy=strategy,
                status="error",
                detail="Active workspace is not a Git repository.",
            )
        manager = WorktreeManager(self.session.repo_path, branch_prefix=prefix)
        return await manager.integrate_branch(
            cleaned,
            strategy=strategy,
            delete_branch_on_success=True,
        )

    async def _handle_subagents_command(self) -> SlashCommandResult:
        branches = self.list_subagent_branches()
        prefix = self._subagent_branch_prefix()
        if not branches:
            return SlashCommandResult(
                handled=True,
                output=(
                    f"No committed subagent branches found under '{prefix}/'. "
                    "Use --subagents with a worker task to produce isolated branches."
                ),
                payload={"branches": []},
            )
        lines = ["Subagent worker branches:"]
        for item in branches:
            lines.append(f"  * {item['branch']} ({item['sha']}) — {item['subject']}")
        return SlashCommandResult(
            handled=True,
            output="\n".join(lines),
            payload={"branches": branches},
        )

    async def _handle_apply_command(self, args: list[str]) -> SlashCommandResult:
        branch, strategy, usage_err = _parse_apply_args(args)
        if usage_err is not None or branch is None:
            return SlashCommandResult(
                handled=True,
                output=usage_err or "Usage: /apply <branch> [--strategy merge|rebase]",
            )
        res = await self.apply_subagent_branch(branch, strategy=strategy)
        return SlashCommandResult(
            handled=True,
            output=_format_apply_result(res),
            payload={
                "branch": res.branch,
                "target_branch": res.target_branch,
                "strategy": res.strategy,
                "status": res.status,
                "merged_revision": res.merged_revision,
                "conflicted_files": list(res.conflicted_files),
                "detail": res.detail,
            },
        )

    async def _handle_sessions_command(self, args: list[str]) -> SlashCommandResult:
        all_repos = "--all" in args
        limit = 20
        for arg in args:
            if arg.startswith("-"):
                continue
            try:
                limit = max(1, int(arg))
            except ValueError:
                return SlashCommandResult(
                    handled=True,
                    output=f"Usage: /sessions [--all] [limit] (got invalid limit '{arg}')",
                )
        summaries = self.list_stored_sessions(all_repos=all_repos, limit=limit)
        if not summaries:
            return SlashCommandResult(
                handled=True,
                output="No stored sessions found.",
                payload={"sessions": []},
            )
        lines = ["Stored sessions:"]
        serialized = []
        for s in summaries:
            marker = "*" if s.id == self.session.id else " "
            title = s.title or "(empty)"
            lines.append(
                f"  {marker} {s.id} | {s.turn_count} turns | {s.provider}/{s.model} | {title}"
            )
            serialized.append({"id": s.id, "turns": s.turn_count, "model": s.model, "title": title})
        return SlashCommandResult(
            handled=True,
            output="\n".join(lines),
            payload={"sessions": serialized, "active_session_id": self.session.id},
        )

    async def _handle_resume_command(self, args: list[str]) -> SlashCommandResult:
        if not args:
            return SlashCommandResult(handled=True, output="Usage: /resume <session_id|latest>")
        try:
            switched = await self.switch_session(args[0])
        except (FileNotFoundError, ValueError) as exc:
            return SlashCommandResult(handled=True, output=f"Resume failed: {exc}")
        return SlashCommandResult(
            handled=True,
            output=(
                f"Resumed session {switched.id} "
                f"({len(switched.turns)} turns, {switched.provider}/{switched.model})."
            ),
            payload={
                "session_id": switched.id,
                "turns": len(switched.turns),
                "provider": switched.provider,
                "model": switched.model,
            },
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
        if cmd == "/clear":
            reset_sandbox = "--sandbox" in args
            await self.clear_session(reset_sandbox=reset_sandbox)
            suffix = " and stopped warm sandbox" if reset_sandbox else ""
            return SlashCommandResult(
                handled=True,
                output=f"Cleared conversation history{suffix}.",
            )
        if cmd == "/help":
            return SlashCommandResult(handled=True, output=_HELP_TEXT)

        handlers: dict[str, Callable[[], Awaitable[SlashCommandResult]]] = {
            "/compact": lambda: self._handle_compact_command(args),
            "/context": self._handle_context_command,
            "/diff": lambda: self._handle_diff_command(args),
            "/undo": lambda: self._handle_undo_command(args),
            "/subagents": self._handle_subagents_command,
            "/apply": lambda: self._handle_apply_command(args),
            "/sessions": lambda: self._handle_sessions_command(args),
            "/resume": lambda: self._handle_resume_command(args),
            "/model": lambda: self._handle_model_command(args),
        }
        handler = handlers.get(cmd)
        if handler is not None:
            return await handler()
        return SlashCommandResult(
            handled=True,
            output=f"Unknown command: {cmd}. Type /help for available commands.",
        )


class _ReplOutputCoordinator:
    """Coordinate streamed assistant tokens, tool status lines, and full-line replies."""

    def __init__(
        self,
        output_fn: Callable[[str], None],
        *,
        stream_fn: Callable[[str], None] | None = None,
        status_fn: Callable[[str], None] | None = None,
    ) -> None:
        self.output_fn = output_fn
        self.stream_fn = stream_fn
        self.status_fn = status_fn
        self.in_stream_line = False
        self.streamed_any = False

    def reset_turn(self) -> None:
        self.in_stream_line = False
        self.streamed_any = False

    def emit_delta(self, text: str) -> None:
        if not text or self.stream_fn is None:
            return
        self.stream_fn(text)
        self.streamed_any = True
        self.in_stream_line = not text.endswith("\n")

    def finish_stream_line(self) -> None:
        if self.in_stream_line and self.stream_fn is not None:
            self.stream_fn("\n")
            self.in_stream_line = False

    def emit_status(self, line: str) -> None:
        self.finish_stream_line()
        if self.status_fn is not None and line:
            self.status_fn(line)

    def emit_output(self, line: str) -> None:
        self.finish_stream_line()
        self.output_fn(line)


class _CoordinatorApprovalGate:
    """Flush any partial streamed line before delegating approval evaluation."""

    def __init__(self, inner: Any, coordinator: _ReplOutputCoordinator) -> None:
        self._inner = inner
        self._coordinator = coordinator

    @property
    def mode(self) -> str:
        return str(getattr(self._inner, "mode", "prompt"))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def evaluate(self, request: ApprovalRequest) -> ApprovalDecision:
        self._coordinator.finish_stream_line()
        result: ApprovalDecision = await self._inner.evaluate(request)
        return result


class _PromptHistory:
    """Manage persistent line-based REPL prompt history with optional readline integration."""

    def __init__(
        self,
        history_path: Path | None,
        *,
        use_readline: bool,
        max_entries: int = 1000,
    ) -> None:
        self._path = history_path
        self._use_readline = use_readline
        self._max_entries = max_entries
        self._entries: list[str] = []

    def load(self) -> None:
        if self._path is not None and self._path.is_file():
            with suppress(OSError):
                raw_lines = self._path.read_text(encoding="utf-8").splitlines()
                self._entries = [line for line in raw_lines if line.strip()][-self._max_entries :]
        if self._use_readline:
            try:
                import readline

                readline.set_history_length(self._max_entries)
                for item in self._entries:
                    readline.add_history(item)
            except Exception:  # noqa: BLE001
                self._use_readline = False

    def record(self, entry: str) -> None:
        cleaned = entry.strip()
        if not cleaned:
            return
        single_line = " ".join(cleaned.splitlines())
        if not self._entries or self._entries[-1] != single_line:
            self._entries.append(single_line)
            if len(self._entries) > self._max_entries:
                self._entries = self._entries[-self._max_entries :]
        if self._use_readline:
            with suppress(Exception):
                import readline

                readline.add_history(single_line)

    def save(self) -> None:
        if self._path is None:
            return
        with suppress(OSError):
            self._path.parent.mkdir(parents=True, exist_ok=True)
            content = "\n".join(self._entries) + ("\n" if self._entries else "")
            self._path.write_text(content, encoding="utf-8")


def _truncate_value(value: Any, *, max_len: int = 56) -> str:
    text = str(value).replace("\n", "\\n")
    return text[: max_len - 3] + "..." if len(text) > max_len else text


def _parse_apply_args(
    args: list[str],
) -> tuple[str | None, Literal["merge", "rebase"], str | None]:
    usage = "Usage: /apply <branch> [--strategy merge|rebase]"
    branch: str | None = None
    strategy: Literal["merge", "rebase"] = "merge"
    idx = 0
    while idx < len(args):
        tok = args[idx]
        if tok == "--strategy":
            if idx + 1 >= len(args):
                return None, strategy, usage
            raw_strat = args[idx + 1].strip().lower()
            if raw_strat not in {"merge", "rebase"}:
                return None, strategy, f"Invalid strategy '{raw_strat}'. {usage}"
            strategy = "rebase" if raw_strat == "rebase" else "merge"
            idx += 2
            continue
        if tok == "--rebase":
            strategy = "rebase"
            idx += 1
            continue
        if tok.startswith("-"):
            return None, strategy, f"Unknown flag '{tok}'. {usage}"
        if branch is not None:
            return None, strategy, usage
        branch = tok
        idx += 1
    if branch is None:
        return None, strategy, usage
    return branch, strategy, None


def _format_apply_result(res: IntegrationResult) -> str:
    if res.status == "merged":
        rev = res.merged_revision[:8] if res.merged_revision else "HEAD"
        return f"Applied '{res.branch}' onto '{res.target_branch}' via {res.strategy} ({rev})."
    if res.status == "no_changes":
        return f"No unmerged changes on '{res.branch}'; cleaned up branch."
    if res.status == "conflict":
        files = ", ".join(res.conflicted_files) if res.conflicted_files else "unknown files"
        return f"Apply conflict on '{res.branch}' ({files}); integration aborted cleanly."
    return f"Apply failed for '{res.branch}': {res.detail or res.status}"


def _format_tool_call_status(data: dict[str, Any]) -> str:
    tool_name = str(data.get("tool_name", "tool"))
    prefix = "[code-mode] " if data.get("code_mode") else ""
    args = data.get("arguments")
    if isinstance(args, dict) and args:
        first_k, first_v = next(iter(args.items()))
        return f"  -> {prefix}{tool_name}({first_k}={_truncate_value(first_v)})"
    return f"  -> {prefix}{tool_name}()"


def _format_tool_result_status(data: dict[str, Any]) -> str:
    tool_name = str(data.get("tool_name", "tool"))
    prefix = "[code-mode] " if data.get("code_mode") else ""
    exit_code = int(data.get("exit_code", 0) or 0)
    duration_ms = int(data.get("duration_ms", 0) or 0)
    has_err = bool(data.get("has_error")) or bool(data.get("error")) or exit_code != 0
    if not has_err:
        return f"  [ok] {prefix}{tool_name} ({duration_ms}ms)"
    return f"  [err] {prefix}{tool_name} (exit {exit_code}, {duration_ms}ms)"


async def _attach_repl_event_handlers(
    event_bus: EventBus,
    coordinator: _ReplOutputCoordinator,
) -> None:
    async def _on_delta(event: Event) -> None:
        coordinator.emit_delta(str(event.data.get("delta", "")))

    async def _on_tool_called(event: Event) -> None:
        coordinator.emit_status(_format_tool_call_status(event.data))

    async def _on_tool_completed(event: Event) -> None:
        coordinator.emit_status(_format_tool_result_status(event.data))

    async def _on_approval_requested(event: Event) -> None:
        tool_name = str(event.data.get("tool_name", "tool"))
        prefix = "[code-mode] " if event.data.get("code_mode") else ""
        reason = str(event.data.get("reason", "")).strip()
        msg = (
            f"  [?] {prefix}Approval needed for {tool_name}: {reason}"
            if reason
            else f"  [?] {prefix}Approval needed for {tool_name}"
        )
        coordinator.emit_status(msg)

    async def _on_approval_decided(event: Event) -> None:
        tool_name = str(event.data.get("tool_name", "tool"))
        prefix = "[code-mode] " if event.data.get("code_mode") else ""
        verdict = "approved" if event.data.get("approved") else "denied"
        coordinator.emit_status(f"  [{verdict}] {prefix}{tool_name}")

    async def _on_subagent_started(event: Event) -> None:
        sid = str(event.data.get("subagent_id", "sub"))
        mode = str(event.data.get("mode", "research"))
        task = _truncate_value(event.data.get("task", ""), max_len=48)
        coordinator.emit_status(f"  [subagent {sid} started ({mode})] {task}")

    async def _on_subagent_completed(event: Event) -> None:
        sid = str(event.data.get("subagent_id", "sub"))
        state = str(event.data.get("state", "completed"))
        tokens = int(event.data.get("total_tokens", 0) or 0)
        coordinator.emit_status(f"  [subagent {sid} {state}: {tokens:,} tokens]")

    await event_bus.subscribe(EventType.ASSISTANT_DELTA, _on_delta)
    await event_bus.subscribe(EventType.TOOL_CALLED, _on_tool_called)
    await event_bus.subscribe(EventType.TOOL_COMPLETED, _on_tool_completed)
    await event_bus.subscribe(EventType.APPROVAL_REQUESTED, _on_approval_requested)
    await event_bus.subscribe(EventType.APPROVAL_DECIDED, _on_approval_decided)
    await event_bus.subscribe(EventType.SUBAGENT_STARTED, _on_subagent_started)
    await event_bus.subscribe(EventType.SUBAGENT_COMPLETED, _on_subagent_completed)


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


async def _read_triple_quoted_block(
    first_body: str,
    continuation_prompt: str,
    input_fn: Callable[[str], str | Awaitable[str]],
) -> str:
    if '"""' in first_body:
        return first_body.split('"""', 1)[0]
    block_lines = [first_body] if first_body else []
    while True:
        nxt = await _read_repl_line(continuation_prompt, input_fn)
        if nxt is None:
            break
        if '"""' in nxt:
            before = nxt.split('"""', 1)[0]
            if before:
                block_lines.append(before)
            break
        block_lines.append(nxt)
    return "\n".join(block_lines)


async def _read_repl_entry(
    prompt_text: str,
    input_fn: Callable[[str], str | Awaitable[str]],
    *,
    continuation_prompt: str = "... ",
) -> str | None:
    first = await _read_repl_line(prompt_text, input_fn)
    if first is None:
        return None

    stripped_leading = first.lstrip()
    if stripped_leading.startswith('"""'):
        return await _read_triple_quoted_block(stripped_leading[3:], continuation_prompt, input_fn)

    if not first.endswith("\\"):
        return first

    parts = [first[:-1]]
    while True:
        nxt = await _read_repl_line(continuation_prompt, input_fn)
        if nxt is None:
            break
        parts.append(nxt[:-1] if nxt.endswith("\\") else nxt)
        if not nxt.endswith("\\"):
            break
    return "\n".join(parts)


async def _default_stdin_reader(prompt_text: str) -> str:
    return await asyncio.to_thread(input, prompt_text)


def _default_stream_writer(chunk: str) -> None:
    sys.stdout.write(chunk)
    sys.stdout.flush()


def _resolve_turn_approval_gate(
    runtime: ChatSessionRuntime,
    coordinator: _ReplOutputCoordinator,
    confirm_fn: Callable[[ApprovalRequest], bool | Awaitable[bool]] | None,
) -> tuple[bool, Any]:
    existing = runtime.approval_gate
    if confirm_fn is not None:

        async def _confirm_callback(request: ApprovalRequest) -> bool:
            coordinator.finish_stream_line()
            ans = confirm_fn(request)
            if inspect.isawaitable(ans):
                return bool(await ans)
            return bool(ans)

        mode = getattr(existing, "mode", None) or runtime.approval_mode
        runtime.approval_gate = ApprovalGate(mode=str(mode), callback=_confirm_callback)
        return True, existing
    if existing is not None and getattr(existing, "mode", "") == "prompt":
        runtime.approval_gate = _CoordinatorApprovalGate(existing, coordinator)
        return True, existing
    return False, existing


async def _run_repl_turn(
    runtime: ChatSessionRuntime,
    prompt: str,
    coordinator: _ReplOutputCoordinator,
    *,
    stream_tokens: bool,
) -> None:
    coordinator.reset_turn()
    turn_bus = EventBus()
    await _attach_repl_event_handlers(turn_bus, coordinator)
    result_run = await runtime.run_turn(
        prompt,
        event_bus=turn_bus,
        stream_tokens=stream_tokens and coordinator.stream_fn is not None,
    )
    coordinator.finish_stream_line()
    reply = _extract_reply(result_run)
    if reply:
        if not coordinator.streamed_any:
            coordinator.emit_output(reply)
    elif result_run.error:
        coordinator.emit_output(f"[{result_run.state.value}] {result_run.error}")
    else:
        coordinator.emit_output(f"[{result_run.state.value}]")


async def run_chat_repl(
    runtime: ChatSessionRuntime,
    *,
    input_fn: Callable[[str], str | Awaitable[str]] | None = None,
    output_fn: Callable[[str], None] | None = None,
    stream_fn: Callable[[str], None] | None = None,
    status_fn: Callable[[str], None] | None = None,
    confirm_fn: Callable[[ApprovalRequest], bool | Awaitable[bool]] | None = None,
    history_path: Path | str | None = None,
    stream_tokens: bool = True,
    prompt_text: str = "you> ",
) -> DevSession:
    """Run an interactive terminal loop over *runtime* until ``/exit`` or EOF."""
    reader = input_fn if input_fn is not None else _default_stdin_reader
    writer = output_fn or print
    effective_stream_fn = (
        _default_stream_writer if (stream_fn is None and output_fn is None) else stream_fn
    )
    coordinator = _ReplOutputCoordinator(writer, stream_fn=effective_stream_fn, status_fn=status_fn)

    resolved_history: Path | None = (
        Path(history_path)
        if history_path is not None
        else (USER_CONFIG_DIR / "chat_history" if input_fn is None else None)
    )
    history = _PromptHistory(resolved_history, use_readline=(input_fn is None))
    history.load()

    gate_swapped, original_gate = _resolve_turn_approval_gate(runtime, coordinator, confirm_fn)
    writer(
        f"Session {runtime.session.id} ({runtime.session.provider}/{runtime.session.model}) "
        f"in {runtime.session.repo_path}. Type /help for commands."
    )
    try:
        while True:
            entry = await _read_repl_entry(prompt_text, reader)
            if entry is None:
                break
            stripped = entry.strip()
            if not stripped:
                continue
            history.record(stripped)
            if stripped.startswith("/"):
                cmd_res = await runtime.handle_slash_command(stripped)
                if cmd_res.output:
                    coordinator.emit_output(cmd_res.output)
                if cmd_res.should_exit:
                    break
                continue
            await _run_repl_turn(runtime, stripped, coordinator, stream_tokens=stream_tokens)
    finally:
        history.save()
        if gate_swapped:
            runtime.approval_gate = original_gate
        await runtime.close()
    return runtime.session
