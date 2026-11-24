"""Bounded hierarchical subagent delegation (research & isolated worktree workers)."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal

from agent_forge.agent.context import ContextEngine
from agent_forge.agent.core import react_loop
from agent_forge.agent.models import AgentConfig, AgentRun, RunState
from agent_forge.agent.prompts import build_system_prompt
from agent_forge.llm.base import TokenUsage
from agent_forge.orchestration.events import Event, EventBus, EventType
from agent_forge.orchestration.worktree import (
    WorktreeError,
    WorktreeLease,
    WorktreeManager,
    is_git_repo,
)
from agent_forge.tools.base import ToolRegistry

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from agent_forge.agent.approvals import ApprovalGate
    from agent_forge.agent.budget import CancellationToken
    from agent_forge.config import SubagentSettings
    from agent_forge.llm.base import LLMProvider
    from agent_forge.sandbox.base import Sandbox, SandboxConfig

_READ_ONLY_TOOLS = frozenset({"read_file", "list_directory", "search_codebase", "git_diff"})
_WORKER_EXCLUDED_TOOLS = frozenset({"git_push", "create_pr", "spawn_subagent"})


@dataclass(frozen=True)
class SubagentLimits:
    """Resource and recursion limits applied to delegated child runs."""

    max_depth: int = 1
    max_subagents_per_run: int = 8
    max_iterations: int = 10
    max_tokens: int = 60_000
    max_result_chars: int = 8_000
    branch_prefix: str = "agent-forge/subagent"

    @classmethod
    def from_settings(cls, settings: SubagentSettings) -> SubagentLimits:
        """Build :class:`SubagentLimits` from Pydantic :class:`SubagentSettings`."""
        return cls(
            max_depth=settings.max_depth,
            max_subagents_per_run=settings.max_subagents_per_run,
            max_iterations=settings.max_iterations,
            max_tokens=settings.max_tokens,
            max_result_chars=settings.max_result_chars,
            branch_prefix=settings.branch_prefix,
        )


@dataclass
class SubagentResult:
    """Structured outcome returned to a parent agent after subagent completion."""

    subagent_id: str
    mode: Literal["research", "worker"]
    task: str
    state: str
    summary: str
    iterations: int = 0
    tokens: TokenUsage = field(default_factory=lambda: TokenUsage(0, 0, 0))
    files_modified: list[str] = field(default_factory=list)
    patch: str = ""
    branch: str | None = None
    error: str | None = None

    def to_tool_output(self, max_chars: int = 8_000) -> str:
        """Render a concise, token-bounded text report for the parent context window."""
        lines = [
            f"[subagent {self.subagent_id} mode={self.mode} state={self.state} "
            f"iterations={self.iterations} tokens={self.tokens.total_tokens:,}]",
        ]
        if self.branch:
            lines.append(f"Branch: {self.branch}")
        if self.files_modified:
            lines.append("Files modified: " + ", ".join(self.files_modified))
        if self.error:
            lines.append(f"Error: {self.error}")
        if self.summary:
            lines.append("")
            lines.append(self.summary.strip())
        if self.patch.strip():
            lines.append("")
            lines.append("Patch preview:")
            lines.append(self.patch.strip())
        rendered = "\n".join(lines).strip()
        if len(rendered) > max_chars:
            suffix = "\n... [subagent output truncated]"
            return rendered[: max(0, max_chars - len(suffix))].rstrip() + suffix
        return rendered


def build_read_only_registry(parent_tools: ToolRegistry) -> ToolRegistry:
    """Return a strictly read-only subset registry for ``research`` subagents."""
    child = ToolRegistry()
    for tool_def in parent_tools.list_definitions():
        if tool_def.name in _READ_ONLY_TOOLS:
            child.register(parent_tools.get(tool_def.name))
    return child


def build_worker_registry(
    parent_tools: ToolRegistry,
    *,
    allow_nested_subagents: bool = False,
) -> ToolRegistry:
    """Return a coding registry for ``worker`` subagents excluding push/PR and cycle tools."""
    child = ToolRegistry()
    for tool_def in parent_tools.list_definitions():
        if tool_def.name in _WORKER_EXCLUDED_TOOLS and not (
            allow_nested_subagents and tool_def.name == "spawn_subagent"
        ):
            continue
        child.register(parent_tools.get(tool_def.name))
    return child


def _extract_last_assistant_text(run: AgentRun) -> str:
    for msg in reversed(run.messages):
        if msg.role.value == "assistant" and msg.content:
            return msg.content.strip()
    return ""


def _collect_modified_files(run: AgentRun) -> list[str]:
    modified: list[str] = []
    for inv in run.tool_invocations:
        if inv.tool_name in {"write_file", "edit_file"} and not inv.result.error:
            path = inv.arguments.get("path") or inv.arguments.get("file_path")
            if isinstance(path, str) and path not in modified:
                modified.append(path)
    return modified


class SubagentRunner:
    """Coordinate bounded child ``research`` and worktree-isolated ``worker`` runs."""

    def __init__(
        self,
        repo_path: str | Path,
        parent_config: AgentConfig,
        sandbox_config: SandboxConfig,
        llm_factory: Callable[[], LLMProvider],
        sandbox_factory: Callable[[SandboxConfig], Sandbox],
        parent_tools: ToolRegistry,
        *,
        limits: SubagentLimits | None = None,
        current_depth: int = 0,
        parent_run_id: str = "",
        event_bus: EventBus | None = None,
        approval_gate: ApprovalGate | None = None,
        cancellation_token: CancellationToken | None = None,
        on_tokens: Callable[[TokenUsage], None] | None = None,
        react_loop_fn: Any = None,
        worktree_root: str | Path | None = None,
    ) -> None:
        self.repo_path = str(repo_path)
        self.parent_config = parent_config
        self.sandbox_config = sandbox_config
        self._llm_factory = llm_factory
        self._sandbox_factory = sandbox_factory
        self.parent_tools = parent_tools
        self.limits = limits or SubagentLimits()
        self.current_depth = current_depth
        self.parent_run_id = parent_run_id
        self.event_bus = event_bus
        self.approval_gate = approval_gate
        self.cancellation_token = cancellation_token
        self._on_tokens = on_tokens
        self._react_loop_fn = react_loop_fn or react_loop
        self.worktree_root = worktree_root
        self._spawned_count = 0

    @property
    def spawned_count(self) -> int:
        """Number of delegated child runs launched so far."""
        return self._spawned_count

    async def _emit(self, event_type: EventType, data: dict[str, Any]) -> None:
        if self.event_bus is not None:
            await self.event_bus.publish(
                Event(
                    type=event_type,
                    run_id=self.parent_run_id or "parent",
                    data=data,
                )
            )

    def _build_child_config(
        self,
        *,
        mode: Literal["research", "worker"],
        child_tools: ToolRegistry,
        task: str,
        max_iterations: int | None,
    ) -> AgentConfig:
        cap = max(1, self.limits.max_iterations)
        chosen_iters = min(max_iterations, cap) if max_iterations and max_iterations > 0 else cap
        chosen_tokens = min(self.parent_config.max_tokens_per_run, self.limits.max_tokens)
        scope = (
            "You are a read-only research subagent. Gather exact evidence using read tools and "
            "return a concise, factual summary without modifying files."
            if mode == "research"
            else (
                "You are an isolated coding subagent working in your own Git worktree. Implement "
                "the requested change and verify it locally; do not push or open pull requests."
            )
        )
        prompt = build_system_prompt(
            task,
            child_tools.list_definitions(),
            sandbox_backend=self.sandbox_config.backend,
            sandbox_image=self.sandbox_config.image,
            network_enabled=self.sandbox_config.network_enabled,
            command_timeout_seconds=self.sandbox_config.timeout_seconds,
            prompt_scope=scope,
        )
        return replace(
            self.parent_config,
            max_iterations=chosen_iters,
            max_tokens_per_run=chosen_tokens,
            system_prompt=prompt,
            approval_mode="auto" if mode == "research" else self.parent_config.approval_mode,
        )

    def _check_budget_guard(
        self,
        subagent_id: str,
        mode: Literal["research", "worker"],
        task: str,
    ) -> SubagentResult | None:
        if self.current_depth >= self.limits.max_depth:
            return SubagentResult(
                subagent_id=subagent_id,
                mode=mode,
                task=task,
                state="failed",
                summary="",
                error=f"Maximum subagent recursion depth ({self.limits.max_depth}) reached.",
            )
        if self._spawned_count >= self.limits.max_subagents_per_run:
            return SubagentResult(
                subagent_id=subagent_id,
                mode=mode,
                task=task,
                state="failed",
                summary="",
                error=(f"Maximum subagents per run ({self.limits.max_subagents_per_run}) reached."),
            )
        if not task.strip():
            return SubagentResult(
                subagent_id=subagent_id,
                mode=mode,
                task=task,
                state="failed",
                summary="",
                error="Subagent task cannot be empty.",
            )
        return None

    async def _run_child_loop(
        self,
        *,
        subagent_id: str,
        mode: Literal["research", "worker"],
        task: str,
        work_repo: str,
        child_tools: ToolRegistry,
        child_config: AgentConfig,
        sandbox: Sandbox,
        own_sandbox: bool,
    ) -> AgentRun:
        child_run = AgentRun(id=subagent_id, task=task, repo_path=work_repo, config=child_config)
        child_context = (
            ContextEngine(work_repo, task=task) if child_config.context_enabled else None
        )
        llm = self._llm_factory()
        child_sb_cfg = replace(self.sandbox_config)
        started_own = False
        try:
            if own_sandbox:
                await sandbox.start(repo_path=work_repo, config=child_sb_cfg)
                started_own = True
            await self._react_loop_fn(
                child_run,
                llm,
                child_tools,
                sandbox,
                emit_lifecycle=False,
                approval_gate=None if mode == "research" else self.approval_gate,
                cancellation_token=self.cancellation_token,
                raise_on_cancel=False,
                context_engine=child_context,
            )
            return child_run
        finally:
            try:
                if started_own:
                    await sandbox.stop()
            finally:
                await llm.close()

    async def delegate(
        self,
        task: str,
        *,
        mode: Literal["research", "worker"] = "research",
        parent_sandbox: Sandbox | None = None,
        max_iterations: int | None = None,
    ) -> SubagentResult:
        """Delegate *task* to a bounded ``research`` or worktree-isolated ``worker`` child."""
        subagent_id = f"sub-{uuid.uuid4().hex[:8]}"
        guard_err = self._check_budget_guard(subagent_id, mode, task)
        if guard_err is not None:
            return guard_err

        self._spawned_count += 1
        await self._emit(
            EventType.SUBAGENT_STARTED,
            {
                "subagent_id": subagent_id,
                "mode": mode,
                "task": task,
                "depth": self.current_depth + 1,
            },
        )

        result = (
            await self._delegate_research(
                subagent_id=subagent_id,
                task=task,
                parent_sandbox=parent_sandbox,
                max_iterations=max_iterations,
            )
            if mode == "research"
            else await self._delegate_worker(
                subagent_id=subagent_id,
                task=task,
                max_iterations=max_iterations,
            )
        )
        if self._on_tokens is not None and result.tokens.total_tokens > 0:
            self._on_tokens(result.tokens)

        await self._emit(
            EventType.SUBAGENT_COMPLETED,
            {
                "subagent_id": subagent_id,
                "mode": mode,
                "state": result.state,
                "iterations": result.iterations,
                "total_tokens": result.tokens.total_tokens,
                "branch": result.branch,
                "files_modified": list(result.files_modified),
            },
        )
        return result

    async def _delegate_research(
        self,
        *,
        subagent_id: str,
        task: str,
        parent_sandbox: Sandbox | None,
        max_iterations: int | None,
    ) -> SubagentResult:
        child_tools = build_read_only_registry(self.parent_tools)
        child_config = self._build_child_config(
            mode="research",
            child_tools=child_tools,
            task=task,
            max_iterations=max_iterations,
        )
        own_sandbox = parent_sandbox is None
        sandbox = (
            self._sandbox_factory(replace(self.sandbox_config))
            if parent_sandbox is None
            else parent_sandbox
        )
        child_run = await self._run_child_loop(
            subagent_id=subagent_id,
            mode="research",
            task=task,
            work_repo=self.repo_path,
            child_tools=child_tools,
            child_config=child_config,
            sandbox=sandbox,
            own_sandbox=own_sandbox,
        )
        return SubagentResult(
            subagent_id=subagent_id,
            mode="research",
            task=task,
            state=child_run.state.value,
            summary=_extract_last_assistant_text(child_run),
            iterations=child_run.iterations,
            tokens=child_run.total_tokens,
            error=child_run.error,
        )

    async def _delegate_worker(
        self,
        *,
        subagent_id: str,
        task: str,
        max_iterations: int | None,
    ) -> SubagentResult:
        if not is_git_repo(self.repo_path):
            return SubagentResult(
                subagent_id=subagent_id,
                mode="worker",
                task=task,
                state="failed",
                summary="",
                error="Worker subagent isolation requires a Git repository.",
            )

        manager = WorktreeManager(
            self.repo_path,
            worktrees_root=self.worktree_root,
            branch_prefix=self.limits.branch_prefix,
        )
        lease: WorktreeLease | None = None
        finalized_ok = False
        child_tools = build_worker_registry(self.parent_tools, allow_nested_subagents=False)
        child_config = self._build_child_config(
            mode="worker",
            child_tools=child_tools,
            task=task,
            max_iterations=max_iterations,
        )
        try:
            lease = await manager.create(subagent_id)
            child_sandbox = self._sandbox_factory(replace(self.sandbox_config))
            child_run = await self._run_child_loop(
                subagent_id=subagent_id,
                mode="worker",
                task=task,
                work_repo=str(lease.worktree_path),
                child_tools=child_tools,
                child_config=child_config,
                sandbox=child_sandbox,
                own_sandbox=True,
            )
            abort = child_run.state != RunState.COMPLETED
            finalized = await manager.finalize(
                subagent_id,
                commit_message=f"agent-forge({subagent_id}): {task[:60]}",
                keep_changed_worktree=False,
                auto_remove_unchanged=True,
                abort=abort,
            )
            finalized_ok = True
            return SubagentResult(
                subagent_id=subagent_id,
                mode="worker",
                task=task,
                state=child_run.state.value,
                summary=_extract_last_assistant_text(child_run),
                iterations=child_run.iterations,
                tokens=child_run.total_tokens,
                files_modified=_collect_modified_files(child_run),
                patch=finalized.patch,
                branch=finalized.branch if finalized.has_changes else None,
                error=child_run.error,
            )
        except (WorktreeError, OSError, RuntimeError) as exc:
            return SubagentResult(
                subagent_id=subagent_id,
                mode="worker",
                task=task,
                state="failed",
                summary="",
                error=str(exc),
            )
        finally:
            if lease is not None and not finalized_ok:
                await manager.finalize(subagent_id, abort=True)

    async def run_batch(
        self,
        specs: Sequence[tuple[str, Literal["research", "worker"]]],
        *,
        parent_sandbox: Sandbox | None = None,
    ) -> list[SubagentResult]:
        """Execute delegated subagent tasks, running multiple research tasks concurrently."""
        if all(mode == "research" for _, mode in specs) and len(specs) > 1:
            return list(
                await asyncio.gather(
                    *(
                        self.delegate(task, mode=mode, parent_sandbox=parent_sandbox)
                        for task, mode in specs
                    )
                )
            )
        results: list[SubagentResult] = []
        for task, mode in specs:
            results.append(await self.delegate(task, mode=mode, parent_sandbox=parent_sandbox))
        return results
