"""Parallel worktree worker dispatcher, independent verification gate, and conflict remediation."""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from agent_forge.agent.approvals import ApprovalGate
from agent_forge.agent.core import react_loop
from agent_forge.agent.models import AgentConfig, RunState
from agent_forge.agent.prompts import build_system_prompt
from agent_forge.agent.session import DevSession
from agent_forge.agent.subagent import (
    _collect_modified_files,
    _extract_last_assistant_text,
    build_read_only_registry,
    build_worker_registry,
)
from agent_forge.orchestration.events import Event, EventBus, EventType
from agent_forge.orchestration.super_agent import (
    AttentionBudget,
    InboxItem,
    Ticket,
    TicketKind,
    TicketLedger,
    TicketState,
    scopes_overlap,
)
from agent_forge.orchestration.worktree import IntegrationResult, WorktreeManager
from agent_forge.sandbox.base import SandboxConfig, sanitize_sandbox_env
from agent_forge.sandbox.factory import create_sandbox

if TYPE_CHECKING:
    from agent_forge.llm.base import LLMProvider
    from agent_forge.sandbox.base import Sandbox
    from agent_forge.tools.base import ToolRegistry

_READ_ONLY_KINDS = frozenset({TicketKind.RESEARCH, TicketKind.REVIEW})


@dataclass(frozen=True)
class ConflictContext:
    """Context passed to a conflict remediator when target integration collides."""

    target_branch: str
    source_branch: str
    conflicted_files: tuple[str, ...]
    detail: str
    source_patch: str = ""


@dataclass(frozen=True)
class TicketExecutionContext:
    """Isolated execution environment provided to a ticket worker invocation."""

    ticket: Ticket
    worktree_path: Path
    branch: str
    base_revision: str
    attempt: int
    approval_gate: ApprovalGate
    prior_verification_feedback: str = ""
    conflict_context: ConflictContext | None = None


@dataclass(frozen=True)
class WorkerExecutionResult:
    """Outcome produced by a single worker execution inside an isolated worktree."""

    success: bool
    summary: str = ""
    error: str = ""
    tokens_used: int = 0
    iterations: int = 0
    files_modified: tuple[str, ...] = ()
    session_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TicketVerificationResult:
    """Independent verification outcome on a clean detached candidate checkout."""

    passed: bool
    summary: str
    command: str = ""
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    reason: str = ""


@dataclass(frozen=True)
class TicketDispatchOutcome:
    """Complete execution, verification, and integration outcome for one ticket."""

    ticket: Ticket
    worker_result: WorkerExecutionResult | None = None
    verification: TicketVerificationResult | None = None
    integration: IntegrationResult | None = None
    remediation_attempts: int = 0


@dataclass(frozen=True)
class DispatchBatchResult:
    """Aggregate summary returned when ``run_until_idle`` finishes."""

    outcomes: tuple[TicketDispatchOutcome, ...]
    integrated_ids: tuple[str, ...]
    ready_for_review_ids: tuple[str, ...]
    blocked_ids: tuple[str, ...]
    failed_ids: tuple[str, ...]
    inbox_items: tuple[InboxItem, ...]


TicketWorkerFn = Callable[[TicketExecutionContext], Awaitable[WorkerExecutionResult]]
TicketVerifierFn = Callable[["Ticket", "Path", str, str], Awaitable[TicketVerificationResult]]
ConflictRemediatorFn = Callable[[TicketExecutionContext], Awaitable[WorkerExecutionResult]]


def _git_sanitized_env() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }


def _git_sync(
    cwd: Path,
    *args: str,
    input_bytes: bytes | None = None,
    timeout: int = 30,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603
        ["git", "-c", f"core.hooksPath={os.devnull}", *args],  # noqa: S607
        cwd=cwd,
        input=input_bytes,
        env=_git_sanitized_env(),
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _isolated_sandbox_config(base: SandboxConfig | None) -> SandboxConfig:
    cfg = replace(base) if base is not None else SandboxConfig()
    return replace(
        cfg,
        network_enabled=False,
        writable_cache_mounts=False,
        published_ports=[],
        allowed_egress_domains=[],
        setup_command="",
        env_vars=sanitize_sandbox_env(dict(cfg.env_vars)),
    )


def _format_ticket_prompt(ctx: TicketExecutionContext) -> str:
    lines = [
        f"Ticket: {ctx.ticket.title}",
        f"Kind: {ctx.ticket.kind.value}",
    ]
    if ctx.ticket.scope_globs:
        lines.append("Owned scope globs: " + ", ".join(ctx.ticket.scope_globs))
    lines.extend(["", "Goal:", ctx.ticket.goal])
    if ctx.prior_verification_feedback:
        lines.extend(
            [
                "",
                "Previous verification attempt failed. Address the failures below:",
                ctx.prior_verification_feedback,
            ]
        )
    if ctx.conflict_context is not None:
        files_text = ", ".join(ctx.conflict_context.conflicted_files) or "(unspecified)"
        target_branch = ctx.conflict_context.target_branch
        lines.extend(
            [
                "",
                f"Integration against target branch '{target_branch}' conflicted on: {files_text}",
                f"Git conflict detail: {ctx.conflict_context.detail}",
            ]
        )
        if ctx.conflict_context.source_patch:
            lines.extend(
                ["", "Original verified patch to port cleanly:", ctx.conflict_context.source_patch]
            )
    return "\n".join(lines)


class SubagentTicketWorker:
    """Execute bounded tickets using DevSession, filtered tools, and isolated sandboxes."""

    def __init__(
        self,
        *,
        parent_config: AgentConfig,
        sandbox_config: SandboxConfig,
        llm_factory: Callable[[], LLMProvider],
        sandbox_factory: Callable[[SandboxConfig], Sandbox],
        parent_tools: ToolRegistry,
        react_loop_fn: Any = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self._parent_config = parent_config
        self._sandbox_config = sandbox_config
        self._llm_factory = llm_factory
        self._sandbox_factory = sandbox_factory
        self._parent_tools = parent_tools
        self._react_loop_fn = react_loop_fn or react_loop
        self._event_bus = event_bus
        self._active_sandboxes: set[Sandbox] = set()

    async def cleanup_active_sandboxes(self) -> None:
        """Stop any still-active sandboxes defensively after cancellation."""
        for sb in list(self._active_sandboxes):
            try:
                await sb.stop()
            finally:
                self._active_sandboxes.discard(sb)

    async def __call__(self, ctx: TicketExecutionContext) -> WorkerExecutionResult:
        """Run one ticket turn inside its isolated worktree and sandbox."""
        is_read_only = ctx.ticket.kind in _READ_ONLY_KINDS
        child_tools = (
            build_read_only_registry(self._parent_tools)
            if is_read_only
            else build_worker_registry(self._parent_tools, allow_nested_subagents=False)
        )
        child_sb_cfg = _isolated_sandbox_config(self._sandbox_config)
        prompt = _format_ticket_prompt(ctx)
        scope_text = (
            "You are a read-only Super-Agent research worker. Gather factual evidence and return "
            "a concise summary without modifying workspace files."
            if is_read_only
            else (
                "You are an isolated Super-Agent worker executing in your own Git worktree. "
                "Complete only the assigned ticket goal within its scope; "
                "never push branches or open PRs."
            )
        )
        system_prompt = build_system_prompt(
            prompt,
            child_tools.list_definitions(),
            sandbox_backend=child_sb_cfg.backend,
            sandbox_image=child_sb_cfg.image,
            network_enabled=False,
            command_timeout_seconds=child_sb_cfg.timeout_seconds,
            prompt_scope=scope_text,
        )
        effective_approval = "auto" if is_read_only else ctx.approval_gate.mode
        child_cfg = replace(
            self._parent_config,
            max_iterations=min(
                self._parent_config.max_iterations, ctx.ticket.budget.max_iterations
            ),
            max_tokens_per_run=min(
                self._parent_config.max_tokens_per_run, ctx.ticket.budget.max_tokens
            ),
            system_prompt=system_prompt,
            approval_mode=effective_approval,
        )
        session = DevSession(
            repo_path=str(ctx.worktree_path),
            title=ctx.ticket.title,
            provider=child_cfg.provider,
            model=child_cfg.model,
            approval_mode=effective_approval,
        )
        run_id = f"{ctx.ticket.ticket_id}-a{ctx.attempt}"
        run = session.prepare_next_run(prompt, config=child_cfg, run_id=run_id)
        sandbox = self._sandbox_factory(child_sb_cfg)
        llm = self._llm_factory()
        started = False
        try:
            await sandbox.start(str(ctx.worktree_path), child_sb_cfg)
            started = True
            self._active_sandboxes.add(sandbox)
            await self._react_loop_fn(
                run,
                llm,
                child_tools,
                sandbox,
                event_bus=self._event_bus,
                emit_lifecycle=False,
                approval_gate=ApprovalGate(mode="auto") if is_read_only else ctx.approval_gate,
                base_revision=ctx.base_revision,
                raise_on_cancel=True,
            )
            session.record_turn(prompt=prompt, run=run)
            success = run.state == RunState.COMPLETED
            return WorkerExecutionResult(
                success=success,
                summary=_extract_last_assistant_text(run),
                error=run.error or ("" if success else f"Run ended with state {run.state.value}"),
                tokens_used=run.total_tokens.total_tokens,
                iterations=run.iterations,
                files_modified=tuple(_collect_modified_files(run)),
                session_id=session.id,
            )
        finally:
            try:
                if started:
                    await sandbox.stop()
            finally:
                self._active_sandboxes.discard(sandbox)
                await llm.close()


async def verify_patch_independently(
    *,
    repo_root: Path,
    base_revision: str,
    patch: str,
    ticket: Ticket,
    verifier_fn: TicketVerifierFn | None = None,
    verify_command: str = "",
    sandbox_config: SandboxConfig | None = None,
    sandbox_factory: Callable[[SandboxConfig], Sandbox] | None = None,
    active_sandboxes: set[Sandbox] | None = None,
) -> TicketVerificationResult:
    """Apply *patch* to a clean detached checkout at *base_revision* and verify independently."""
    if not patch.strip():
        return TicketVerificationResult(
            passed=False,
            summary="Worker produced no repository diff to verify.",
            reason="empty_patch",
        )

    tmp_dir = tempfile.TemporaryDirectory(prefix="af-super-verify-")
    verifier_dir = Path(tmp_dir.name) / "checkout"
    sandbox: Sandbox | None = None
    started = False
    try:
        clone_proc = await asyncio.to_thread(
            _git_sync,
            Path(tmp_dir.name),
            "clone",
            "--no-local",
            "--no-checkout",
            "--template=",
            "--",
            str(repo_root),
            str(verifier_dir),
        )
        if clone_proc.returncode != 0:
            detail = clone_proc.stderr.decode(errors="replace").strip()
            return TicketVerificationResult(
                passed=False,
                summary="Failed to clone repository for independent verification.",
                stderr=detail,
                reason="verification_error",
            )

        checkout_proc = await asyncio.to_thread(
            _git_sync, verifier_dir, "checkout", "--detach", base_revision
        )
        if checkout_proc.returncode != 0:
            detail = checkout_proc.stderr.decode(errors="replace").strip()
            return TicketVerificationResult(
                passed=False,
                summary=f"Failed to check out base revision {base_revision}.",
                stderr=detail,
                reason="verification_error",
            )

        apply_proc = await asyncio.to_thread(
            _git_sync,
            verifier_dir,
            "apply",
            "-",
            input_bytes=patch.encode(errors="surrogateescape"),
        )
        if apply_proc.returncode != 0:
            detail = apply_proc.stderr.decode(errors="replace").strip()
            return TicketVerificationResult(
                passed=False,
                summary="Candidate patch failed to apply cleanly on independent checkout.",
                stderr=detail,
                reason="verification_error",
            )

        if verifier_fn is not None:
            return await verifier_fn(ticket, verifier_dir, patch, base_revision)

        cleaned_cmd = verify_command.strip()
        if not cleaned_cmd:
            return TicketVerificationResult(
                passed=False,
                summary="No independent verification command or verifier configured.",
                reason="verification_error",
            )

        ver_sb_cfg = _isolated_sandbox_config(sandbox_config)
        sandbox = (sandbox_factory or create_sandbox)(ver_sb_cfg)
        if active_sandboxes is not None:
            active_sandboxes.add(sandbox)
        await sandbox.start(str(verifier_dir), ver_sb_cfg)
        started = True
        exec_result = await sandbox.exec(cleaned_cmd, timeout_seconds=ver_sb_cfg.timeout_seconds)
        passed = exec_result.exit_code == 0
        summary = (
            f"Independent check passed: `{cleaned_cmd}`"
            if passed
            else f"Independent check failed (exit {exec_result.exit_code}): `{cleaned_cmd}`"
        )
        return TicketVerificationResult(
            passed=passed,
            summary=summary,
            command=cleaned_cmd,
            exit_code=exec_result.exit_code,
            stdout=exec_result.stdout,
            stderr=exec_result.stderr,
            reason="verified" if passed else "check_failed",
        )
    finally:
        try:
            if sandbox is not None and started:
                await sandbox.stop()
        finally:
            if sandbox is not None and active_sandboxes is not None:
                active_sandboxes.discard(sandbox)
            tmp_dir.cleanup()


class SuperAgentDispatcher:
    """Coordinate parallel worktree workers, independent verification, and ordered integration."""

    def __init__(
        self,
        *,
        repo_path: str | Path,
        ledger: TicketLedger,
        worker_fn: TicketWorkerFn,
        verifier_fn: TicketVerifierFn | None = None,
        remediator_fn: ConflictRemediatorFn | None = None,
        attention_budget: AttentionBudget | None = None,
        event_bus: EventBus | None = None,
        worktree_manager: WorktreeManager | None = None,
        approval_gate: ApprovalGate | None = None,
        max_concurrent_workers: int = 3,
        max_verification_retries: int = 1,
        max_conflict_remediations: int = 1,
        merge_strategy: Literal["merge", "rebase"] = "merge",
        target_branch: str | None = None,
        auto_integrate: bool = True,
        verify_command: str = "",
        sandbox_config: SandboxConfig | None = None,
        sandbox_factory: Callable[[SandboxConfig], Sandbox] | None = None,
        worktree_root: str | Path | None = None,
        worktree_branch_prefix: str = "agent-forge/super",
        allow_worker_remediation: bool = False,
    ) -> None:
        if max_concurrent_workers < 1:
            msg = "max_concurrent_workers must be >= 1"
            raise ValueError(msg)
        self.ledger = ledger
        self.worktree_manager = worktree_manager or WorktreeManager(
            repo_path,
            worktrees_root=worktree_root,
            branch_prefix=worktree_branch_prefix,
            base_ref=target_branch,
        )
        self.repo_root = self.worktree_manager.repo_root
        self.attention_budget = attention_budget or AttentionBudget()
        self.approval_gate = approval_gate or ApprovalGate(mode="prompt")
        self._worker_fn = worker_fn
        self._verifier_fn = verifier_fn
        self._remediator_fn = remediator_fn
        self._event_bus = event_bus
        self._max_concurrent_workers = max_concurrent_workers
        self._max_verification_retries = max(0, max_verification_retries)
        self._max_conflict_remediations = max(0, max_conflict_remediations)
        self._merge_strategy: Literal["merge", "rebase"] = merge_strategy
        self._target_branch = target_branch
        self._auto_integrate = auto_integrate
        self._verify_command = verify_command
        self._sandbox_config = sandbox_config
        self._sandbox_factory = sandbox_factory
        self._allow_worker_remediation = allow_worker_remediation

        self._integration_lock = asyncio.Lock()
        self._active_worktree_runs: set[str] = set()
        self._active_verification_sandboxes: set[Sandbox] = set()
        self._ticket_run_ids: dict[str, str] = {}
        self._ticket_patches: dict[str, str] = {}

    async def _publish(self, event_type: EventType, run_id: str, data: dict[str, Any]) -> None:
        if self._event_bus is not None:
            await self._event_bus.publish(Event(type=event_type, run_id=run_id, data=data))

    async def _emit_ticket_state(
        self, event_type: EventType, ticket: Ticket, extra: dict[str, Any] | None = None
    ) -> None:
        payload = {"ticket_id": ticket.ticket_id, "state": ticket.state.value}
        if ticket.branch:
            payload["branch"] = ticket.branch
        if extra:
            payload.update(extra)
        await self._publish(event_type, ticket.parent_input_id, payload)
        if event_type != EventType.SUPER_TICKET_UPDATED:
            await self._publish(
                EventType.SUPER_TICKET_UPDATED,
                ticket.parent_input_id,
                {"ticket_id": ticket.ticket_id, "state": ticket.state.value},
            )

    def _mark_cancelled_sync(self, ticket_id: str) -> None:
        with contextlib.suppress(KeyError, ValueError):
            current = self.ledger.get_ticket(ticket_id)
            if current.state in {
                TicketState.ADMITTED,
                TicketState.DISPATCHED,
                TicketState.VERIFYING,
                TicketState.READY_FOR_REVIEW,
                TicketState.CONFLICT,
            }:
                self.ledger.transition_ticket(
                    ticket_id,
                    TicketState.CANCELLED,
                    blocked_reason="Execution cancelled.",
                )

    def _delete_branch_safely(self, run_id: str | None, branch: str) -> None:
        if run_id and self.worktree_manager.get_lease(run_id) is not None:
            with self.worktree_manager._lock:
                self.worktree_manager._remove_sync(run_id, delete_branch=True)
            return
        if branch:
            with self.worktree_manager._lock:
                checkout = self.worktree_manager._find_checkout_for_branch_sync(branch)
                if checkout is not None:
                    self.worktree_manager._remove_worktree_sync(checkout)
                self.worktree_manager._delete_branch_sync(branch)

    async def _escalate_to_human(
        self,
        ticket: Ticket,
        *,
        kind: Literal["approval", "conflict"],
        blocked_reason: str,
        summary: str,
        detail: str,
        verification_summary: str | None = None,
        metadata_update: dict[str, Any] | None = None,
    ) -> Ticket:
        updated = self.ledger.transition_ticket(
            ticket.ticket_id,
            TicketState.BLOCKED_ON_HUMAN,
            blocked_reason=blocked_reason,
            branch="",
            verification_summary=verification_summary,
            metadata_update=metadata_update,
        )
        inbox_item = self.attention_budget.enqueue_escalation(
            self.ledger,
            kind=kind,
            ticket_ids=[updated.ticket_id],
            summary=summary,
            detail=detail,
        )
        await self._emit_ticket_state(
            EventType.SUPER_TICKET_UPDATED, updated, {"reason": blocked_reason}
        )
        await self._publish(
            EventType.SUPER_INBOX_UPDATED,
            updated.parent_input_id,
            {
                "item_id": inbox_item.item_id,
                "ticket_ids": inbox_item.ticket_ids,
                "kind": inbox_item.kind,
            },
        )
        return updated

    async def _execute_read_only_ticket(self, ticket: Ticket) -> TicketDispatchOutcome:
        attempt = ticket.attempts + 1
        dispatched = self.ledger.transition_ticket(
            ticket.ticket_id,
            TicketState.DISPATCHED,
            attempts=attempt,
        )
        await self._emit_ticket_state(
            EventType.SUPER_TICKET_DISPATCHED, dispatched, {"attempt": attempt}
        )

        base_rev = (
            _git_sync(self.repo_root, "rev-parse", "--verify", self._target_branch or "HEAD")
            .stdout.decode(errors="replace")
            .strip()
        )
        ctx = TicketExecutionContext(
            ticket=dispatched,
            worktree_path=self.repo_root,
            branch="",
            base_revision=base_rev,
            attempt=attempt,
            approval_gate=ApprovalGate(mode="auto"),
        )
        try:
            worker_res = await self._worker_fn(ctx)
        except asyncio.CancelledError:
            self._mark_cancelled_sync(ticket.ticket_id)
            raise
        except Exception as exc:  # noqa: BLE001
            failed = self.ledger.transition_ticket(
                ticket.ticket_id,
                TicketState.FAILED,
                blocked_reason=str(exc),
            )
            await self._emit_ticket_state(
                EventType.SUPER_TICKET_FAILED, failed, {"error": str(exc)}
            )
            return TicketDispatchOutcome(ticket=failed)

        if not worker_res.success:
            failed = self.ledger.transition_ticket(
                ticket.ticket_id,
                TicketState.FAILED,
                blocked_reason=worker_res.error or "Read-only worker failed.",
            )
            await self._emit_ticket_state(
                EventType.SUPER_TICKET_FAILED, failed, {"error": worker_res.error}
            )
            return TicketDispatchOutcome(ticket=failed, worker_result=worker_res)

        verifying = self.ledger.transition_ticket(ticket.ticket_id, TicketState.VERIFYING)
        verification = TicketVerificationResult(
            passed=True,
            summary="Read-only research/review task completed cleanly.",
            reason="read_only_ok",
        )
        ready = self.ledger.transition_ticket(
            verifying.ticket_id,
            TicketState.READY_FOR_REVIEW,
            verification_summary=verification.summary,
            metadata_update={"worker_session_id": worker_res.session_id},
        )
        await self._emit_ticket_state(EventType.SUPER_TICKET_VERIFIED, ready)
        return TicketDispatchOutcome(
            ticket=ready, worker_result=worker_res, verification=verification
        )

    async def _run_in_isolated_worktree(
        self,
        ticket: Ticket,
        *,
        run_id: str,
        attempt: int,
        prior_feedback: str = "",
        conflict_context: ConflictContext | None = None,
        worker_callable: TicketWorkerFn | None = None,
    ) -> tuple[WorkerExecutionResult, Any]:
        fn = worker_callable or self._worker_fn
        self._active_worktree_runs.add(run_id)
        try:
            lease = await self.worktree_manager.create(run_id, base_ref=self._target_branch)
            await self._publish(
                EventType.WORKTREE_CREATED,
                ticket.ticket_id,
                {"branch": lease.branch, "worktree_path": str(lease.worktree_path)},
            )
            ctx = TicketExecutionContext(
                ticket=ticket,
                worktree_path=lease.worktree_path,
                branch=lease.branch,
                base_revision=lease.base_revision,
                attempt=attempt,
                approval_gate=self.approval_gate,
                prior_verification_feedback=prior_feedback,
                conflict_context=conflict_context,
            )
            worker_result = await fn(ctx)
        except BaseException:
            # Run synchronous abort immediately so cancellation never leaks worktrees.
            self.worktree_manager._abort_sync(run_id)
            self._active_worktree_runs.discard(run_id)
            raise

        if not worker_result.success:
            try:
                await self.worktree_manager.finalize(run_id, abort=True)
            except BaseException:
                self.worktree_manager._abort_sync(run_id)
                self._active_worktree_runs.discard(run_id)
                raise
            self._active_worktree_runs.discard(run_id)
            await self._publish(
                EventType.WORKTREE_CLEANED,
                ticket.ticket_id,
                {"branch": lease.branch, "state": "removed"},
            )
            return worker_result, None

        try:
            finalized = await self.worktree_manager.finalize(
                run_id,
                commit_message=f"super-agent({ticket.ticket_id}): {ticket.title[:60]}",
                keep_changed_worktree=False,
                auto_remove_unchanged=True,
            )
        except BaseException:
            self.worktree_manager._abort_sync(run_id)
            self._active_worktree_runs.discard(run_id)
            raise
        self._active_worktree_runs.discard(run_id)
        await self._publish(
            EventType.WORKTREE_CLEANED,
            ticket.ticket_id,
            {"branch": finalized.branch, "state": finalized.state.value},
        )
        return worker_result, finalized

    async def _execute_mutating_ticket(self, initial_ticket: Ticket) -> TicketDispatchOutcome:
        ticket = initial_ticket
        prior_feedback = ""
        last_worker_result: WorkerExecutionResult | None = None
        last_verification: TicketVerificationResult | None = None

        for attempt_idx in range(self._max_verification_retries + 1):
            attempt = ticket.attempts + 1
            run_id = f"{ticket.ticket_id}-a{attempt}"
            dispatched = self.ledger.transition_ticket(
                ticket.ticket_id,
                TicketState.DISPATCHED,
                attempts=attempt,
            )
            await self._emit_ticket_state(
                EventType.SUPER_TICKET_DISPATCHED, dispatched, {"attempt": attempt}
            )

            try:
                worker_result, finalized = await self._run_in_isolated_worktree(
                    dispatched,
                    run_id=run_id,
                    attempt=attempt,
                    prior_feedback=prior_feedback,
                )
            except asyncio.CancelledError:
                self._mark_cancelled_sync(ticket.ticket_id)
                raise
            except Exception as exc:  # noqa: BLE001
                failed = self.ledger.transition_ticket(
                    ticket.ticket_id,
                    TicketState.FAILED,
                    blocked_reason=str(exc),
                )
                await self._emit_ticket_state(
                    EventType.SUPER_TICKET_FAILED, failed, {"error": str(exc)}
                )
                return TicketDispatchOutcome(ticket=failed, worker_result=last_worker_result)

            last_worker_result = worker_result
            if not worker_result.success or finalized is None:
                failed = self.ledger.transition_ticket(
                    ticket.ticket_id,
                    TicketState.FAILED,
                    blocked_reason=worker_result.error or "Worker execution failed.",
                )
                await self._emit_ticket_state(
                    EventType.SUPER_TICKET_FAILED,
                    failed,
                    {"error": worker_result.error or "worker_failed"},
                )
                return TicketDispatchOutcome(ticket=failed, worker_result=worker_result)

            candidate_branch = finalized.branch if finalized.has_changes else ""
            verifying = self.ledger.transition_ticket(
                dispatched.ticket_id,
                TicketState.VERIFYING,
                branch=candidate_branch,
            )

            if not finalized.has_changes or not finalized.patch.strip():
                verification = TicketVerificationResult(
                    passed=False,
                    summary="Worker produced an empty patch.",
                    reason="empty_patch",
                )
            else:
                try:
                    verification = await verify_patch_independently(
                        repo_root=self.repo_root,
                        base_revision=finalized.base_revision,
                        patch=finalized.patch,
                        ticket=verifying,
                        verifier_fn=self._verifier_fn,
                        verify_command=self._verify_command,
                        sandbox_config=self._sandbox_config,
                        sandbox_factory=self._sandbox_factory,
                        active_sandboxes=self._active_verification_sandboxes,
                    )
                except asyncio.CancelledError:
                    await self.worktree_manager.remove(run_id, delete_branch=True)
                    self._mark_cancelled_sync(ticket.ticket_id)
                    raise
                except Exception as exc:  # noqa: BLE001
                    verification = TicketVerificationResult(
                        passed=False,
                        summary=f"Verification raised an exception: {exc}",
                        stderr=str(exc),
                        reason="verification_error",
                    )

            last_verification = verification
            if verification.passed:
                self._ticket_run_ids[ticket.ticket_id] = run_id
                self._ticket_patches[ticket.ticket_id] = finalized.patch
                ready = self.ledger.transition_ticket(
                    verifying.ticket_id,
                    TicketState.READY_FOR_REVIEW,
                    branch=finalized.branch,
                    verification_summary=verification.summary,
                    metadata_update={
                        "commit_sha": finalized.commit_sha,
                        "verification_reason": verification.reason,
                        "verification_exit_code": verification.exit_code,
                        "worker_session_id": worker_result.session_id,
                        "worktree_run_id": run_id,
                    },
                )
                await self._emit_ticket_state(
                    EventType.SUPER_TICKET_VERIFIED,
                    ready,
                    {"verification_reason": verification.reason},
                )
                return TicketDispatchOutcome(
                    ticket=ready,
                    worker_result=worker_result,
                    verification=verification,
                )

            await self.worktree_manager.remove(run_id, delete_branch=True)
            ticket = verifying
            if attempt_idx < self._max_verification_retries:
                prior_feedback = "\n".join(
                    part
                    for part in (verification.summary, verification.stderr, verification.stdout)
                    if part
                )
                continue

            reason_msg = f"Verification failed after {attempt} attempt(s): {verification.summary}"
            blocked = await self._escalate_to_human(
                verifying,
                kind="approval",
                blocked_reason=reason_msg,
                summary=f"{verifying.title}: verification failed",
                detail=verification.stderr or verification.summary,
                verification_summary=verification.summary,
                metadata_update={"verification_reason": verification.reason},
            )
            return TicketDispatchOutcome(
                ticket=blocked,
                worker_result=worker_result,
                verification=verification,
            )

        # Defensive fallback (loop always returns inside branches above).
        return TicketDispatchOutcome(  # pragma: no cover
            ticket=self.ledger.get_ticket(ticket.ticket_id),
            worker_result=last_worker_result,
            verification=last_verification,
        )

    async def _execute_ticket(self, ticket: Ticket) -> TicketDispatchOutcome:
        if ticket.kind in _READ_ONLY_KINDS:
            return await self._execute_read_only_ticket(ticket)
        return await self._execute_mutating_ticket(ticket)

    async def _attempt_conflict_remediation(
        self,
        conflict_ticket: Ticket,
        initial_result: IntegrationResult,
        orig_run_id: str | None,
    ) -> tuple[Ticket, IntegrationResult, TicketVerificationResult | None, int]:
        remediator = self._remediator_fn or (
            self._worker_fn if self._allow_worker_remediation else None
        )
        if remediator is None or self._max_conflict_remediations <= 0:
            return conflict_ticket, initial_result, None, 0

        source_patch = self._ticket_patches.get(conflict_ticket.ticket_id, "")
        conflict_ctx = ConflictContext(
            target_branch=initial_result.target_branch,
            source_branch=initial_result.branch,
            conflicted_files=tuple(initial_result.conflicted_files),
            detail=initial_result.detail,
            source_patch=source_patch,
        )

        last_result = initial_result
        last_verification: TicketVerificationResult | None = None
        attempts_made = 0

        for rem_idx in range(1, self._max_conflict_remediations + 1):
            attempts_made = rem_idx
            rem_run_id = f"{conflict_ticket.ticket_id}-remed-{rem_idx}"
            try:
                worker_res, finalized = await self._run_in_isolated_worktree(
                    conflict_ticket,
                    run_id=rem_run_id,
                    attempt=conflict_ticket.attempts + rem_idx,
                    conflict_context=conflict_ctx,
                    worker_callable=remediator,
                )
            except asyncio.CancelledError:
                self._delete_branch_safely(orig_run_id, conflict_ticket.branch)
                self._mark_cancelled_sync(conflict_ticket.ticket_id)
                raise
            except Exception as exc:  # noqa: BLE001
                last_result = IntegrationResult(
                    run_id=rem_run_id,
                    branch="",
                    target_branch=initial_result.target_branch,
                    strategy=self._merge_strategy,
                    status="error",
                    conflicted_files=list(initial_result.conflicted_files),
                    detail=f"Conflict remediation raised error: {exc}",
                )
                continue

            if not worker_res.success or finalized is None or not finalized.has_changes:
                await self.worktree_manager.remove(rem_run_id, delete_branch=True)
                last_result = IntegrationResult(
                    run_id=rem_run_id,
                    branch="",
                    target_branch=initial_result.target_branch,
                    strategy=self._merge_strategy,
                    status="conflict",
                    conflicted_files=list(initial_result.conflicted_files),
                    detail=worker_res.error or "Conflict remediation produced no changes.",
                )
                continue

            verification = await verify_patch_independently(
                repo_root=self.repo_root,
                base_revision=finalized.base_revision,
                patch=finalized.patch,
                ticket=conflict_ticket,
                verifier_fn=self._verifier_fn,
                verify_command=self._verify_command,
                sandbox_config=self._sandbox_config,
                sandbox_factory=self._sandbox_factory,
                active_sandboxes=self._active_verification_sandboxes,
            )
            last_verification = verification
            if not verification.passed:
                await self.worktree_manager.remove(rem_run_id, delete_branch=True)
                last_result = IntegrationResult(
                    run_id=rem_run_id,
                    branch="",
                    target_branch=initial_result.target_branch,
                    strategy=self._merge_strategy,
                    status="conflict",
                    conflicted_files=list(initial_result.conflicted_files),
                    detail=f"Remediation verification failed: {verification.summary}",
                )
                continue

            rem_integration = await self.worktree_manager.integrate(
                rem_run_id,
                target_branch=self._target_branch,
                strategy=self._merge_strategy,
                delete_branch_on_success=True,
            )
            if rem_integration.status in {"merged", "no_changes"}:
                self._delete_branch_safely(orig_run_id, conflict_ticket.branch)
                integrated = self.ledger.transition_ticket(
                    conflict_ticket.ticket_id,
                    TicketState.INTEGRATED,
                    branch="",
                    verification_summary=verification.summary,
                    metadata_update={
                        "merged_revision": rem_integration.merged_revision,
                        "remediated": True,
                        "remediation_attempts": rem_idx,
                    },
                )
                await self._publish(
                    EventType.WORKTREE_INTEGRATED,
                    rem_run_id,
                    {"branch": rem_integration.branch, "revision": rem_integration.merged_revision},
                )
                await self._emit_ticket_state(
                    EventType.SUPER_TICKET_INTEGRATED,
                    integrated,
                    {"merged_revision": rem_integration.merged_revision, "remediated": True},
                )
                return integrated, rem_integration, verification, rem_idx

            await self.worktree_manager.remove(rem_run_id, delete_branch=True)
            last_result = rem_integration

        return conflict_ticket, last_result, last_verification, attempts_made

    async def integrate_ready_tickets(
        self,
        *,
        parent_input_id: str | None = None,
    ) -> list[TicketDispatchOutcome]:
        """Integrate verified ``READY_FOR_REVIEW`` tickets in topological dependency order."""
        async with self._integration_lock:
            ordered = self.ledger.topological_order()
            outcomes: list[TicketDispatchOutcome] = []
            for ticket in ordered:
                if parent_input_id is not None and ticket.parent_input_id != parent_input_id:
                    continue
                if ticket.state != TicketState.READY_FOR_REVIEW:
                    continue
                if ticket.kind in _READ_ONLY_KINDS:
                    continue
                if not all(self.ledger._dependency_satisfied(dep) for dep in ticket.depends_on):
                    continue

                run_id = self._ticket_run_ids.get(ticket.ticket_id) or str(
                    ticket.metadata.get("worktree_run_id", "")
                )
                if run_id and self.worktree_manager.get_lease(run_id) is not None:
                    int_result = await self.worktree_manager.integrate(
                        run_id,
                        target_branch=self._target_branch,
                        strategy=self._merge_strategy,
                        delete_branch_on_success=True,
                    )
                else:
                    int_result = await self.worktree_manager.integrate_branch(
                        ticket.branch,
                        run_id=ticket.ticket_id,
                        target_branch=self._target_branch,
                        strategy=self._merge_strategy,
                        delete_branch_on_success=True,
                    )

                if int_result.status in {"merged", "no_changes"}:
                    integrated = self.ledger.transition_ticket(
                        ticket.ticket_id,
                        TicketState.INTEGRATED,
                        branch="",
                        metadata_update={
                            "merged_revision": int_result.merged_revision,
                            "integration_status": int_result.status,
                        },
                    )
                    await self._publish(
                        EventType.WORKTREE_INTEGRATED,
                        run_id or ticket.ticket_id,
                        {"branch": int_result.branch, "revision": int_result.merged_revision},
                    )
                    await self._emit_ticket_state(
                        EventType.SUPER_TICKET_INTEGRATED,
                        integrated,
                        {"merged_revision": int_result.merged_revision},
                    )
                    outcomes.append(
                        TicketDispatchOutcome(ticket=integrated, integration=int_result)
                    )
                    continue

                if int_result.status == "conflict":
                    conflict_ticket = self.ledger.transition_ticket(
                        ticket.ticket_id,
                        TicketState.CONFLICT,
                        metadata_update={
                            "conflict_files": list(int_result.conflicted_files),
                            "conflict_detail": int_result.detail,
                        },
                    )
                    await self._publish(
                        EventType.WORKTREE_CONFLICT,
                        run_id or ticket.ticket_id,
                        {
                            "branch": int_result.branch,
                            "conflicted_files": int_result.conflicted_files,
                        },
                    )
                    await self._emit_ticket_state(
                        EventType.SUPER_TICKET_CONFLICT,
                        conflict_ticket,
                        {"conflicted_files": int_result.conflicted_files},
                    )

                    (
                        final_ticket,
                        final_int,
                        rem_verif,
                        rem_attempts,
                    ) = await self._attempt_conflict_remediation(
                        conflict_ticket, int_result, run_id or None
                    )
                    if final_ticket.state == TicketState.INTEGRATED:
                        outcomes.append(
                            TicketDispatchOutcome(
                                ticket=final_ticket,
                                verification=rem_verif,
                                integration=final_int,
                                remediation_attempts=rem_attempts,
                            )
                        )
                        continue

                    self._delete_branch_safely(run_id or None, conflict_ticket.branch)
                    files_str = ", ".join(int_result.conflicted_files) or conflict_ticket.branch
                    patch_preview = self._ticket_patches.get(ticket.ticket_id, "")[:1200]
                    blocked = await self._escalate_to_human(
                        conflict_ticket,
                        kind="conflict",
                        blocked_reason=f"Merge conflict on {files_str}",
                        summary=f"{conflict_ticket.title}: merge conflict on {files_str}",
                        detail="\n".join(
                            part
                            for part in (final_int.detail or int_result.detail, patch_preview)
                            if part
                        ),
                        metadata_update={
                            "conflict_files": list(int_result.conflicted_files),
                            "remediation_attempts": rem_attempts,
                        },
                    )
                    outcomes.append(
                        TicketDispatchOutcome(
                            ticket=blocked,
                            verification=rem_verif,
                            integration=final_int,
                            remediation_attempts=rem_attempts,
                        )
                    )
                    continue

                self._delete_branch_safely(run_id or None, ticket.branch)
                blocked = await self._escalate_to_human(
                    ticket,
                    kind="approval",
                    blocked_reason=int_result.detail or "Integration error",
                    summary=f"{ticket.title}: integration failed",
                    detail=int_result.detail,
                )
                outcomes.append(TicketDispatchOutcome(ticket=blocked, integration=int_result))
            return outcomes

    def _select_dispatchable_tickets(
        self,
        *,
        parent_input_id: str | None,
        active_tasks: dict[asyncio.Task[TicketDispatchOutcome], Ticket],
    ) -> list[Ticket]:
        active_ids = {t.ticket_id for t in active_tasks.values()}
        active_mutating_scopes = [
            t.scope_globs for t in active_tasks.values() if t.kind not in _READ_ONLY_KINDS
        ]
        slots = max(0, self._max_concurrent_workers - len(active_tasks))
        if slots <= 0:
            return []

        selected: list[Ticket] = []
        for candidate in self.ledger.ready_to_dispatch():
            if parent_input_id is not None and candidate.parent_input_id != parent_input_id:
                continue
            if candidate.ticket_id in active_ids:
                continue
            if candidate.kind not in _READ_ONLY_KINDS and any(
                scopes_overlap(candidate.scope_globs, active_globs)
                for active_globs in active_mutating_scopes
            ):
                continue
            selected.append(candidate)
            if candidate.kind not in _READ_ONLY_KINDS:
                active_mutating_scopes.append(candidate.scope_globs)
            if len(selected) >= slots:
                break
        return selected

    async def _cleanup_in_flight(
        self,
        active_tasks: dict[asyncio.Task[TicketDispatchOutcome], Ticket],
    ) -> None:
        for task in list(active_tasks):
            if not task.done():
                task.cancel()
        if active_tasks:
            with contextlib.suppress(BaseException):
                await asyncio.gather(*active_tasks.keys(), return_exceptions=True)
        for rid in list(self._active_worktree_runs):
            self.worktree_manager._abort_sync(rid)
            self._active_worktree_runs.discard(rid)
        for sb in list(self._active_verification_sandboxes):
            with contextlib.suppress(Exception):
                await sb.stop()
            self._active_verification_sandboxes.discard(sb)
        cleanup_fn = getattr(self._worker_fn, "cleanup_active_sandboxes", None)
        if callable(cleanup_fn):
            with contextlib.suppress(Exception):
                await cleanup_fn()

    async def run_until_idle(
        self,
        *,
        parent_input_id: str | None = None,
    ) -> DispatchBatchResult:
        """Dispatch, verify, and integrate ready tickets until no runnable work remains."""
        active_tasks: dict[asyncio.Task[TicketDispatchOutcome], Ticket] = {}
        outcomes_by_id: dict[str, TicketDispatchOutcome] = {}

        try:
            while True:
                to_launch = self._select_dispatchable_tickets(
                    parent_input_id=parent_input_id,
                    active_tasks=active_tasks,
                )
                for ticket in to_launch:
                    task = asyncio.create_task(self._execute_ticket(ticket))
                    active_tasks[task] = ticket

                if not active_tasks:
                    break

                done, _ = await asyncio.wait(
                    active_tasks.keys(),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for done_task in done:
                    ticket = active_tasks.pop(done_task)
                    outcome = done_task.result()
                    outcomes_by_id[ticket.ticket_id] = outcome

                if self._auto_integrate:
                    integrated_outcomes = await self.integrate_ready_tickets(
                        parent_input_id=parent_input_id
                    )
                    for int_outcome in integrated_outcomes:
                        prev = outcomes_by_id.get(int_outcome.ticket.ticket_id)
                        outcomes_by_id[int_outcome.ticket.ticket_id] = TicketDispatchOutcome(
                            ticket=int_outcome.ticket,
                            worker_result=prev.worker_result if prev else None,
                            verification=int_outcome.verification
                            or (prev.verification if prev else None),
                            integration=int_outcome.integration,
                            remediation_attempts=int_outcome.remediation_attempts,
                        )
        except BaseException:
            await self._cleanup_in_flight(active_tasks)
            raise

        if not self._auto_integrate:
            ready_tickets = [
                t
                for t in self.ledger.list_tickets(
                    state=TicketState.READY_FOR_REVIEW, parent_input_id=parent_input_id
                )
            ]
            if ready_tickets:
                summary_lines = [
                    f"- {t.ticket_id} ({t.title}): {t.verification_summary or 'verified'}"
                    for t in ready_tickets
                ]
                bundle_item = self.attention_budget.enqueue_escalation(
                    self.ledger,
                    kind="review_bundle",
                    ticket_ids=[t.ticket_id for t in ready_tickets],
                    summary=f"{len(ready_tickets)} verified ticket(s) ready for integration review",
                    detail="\n".join(summary_lines),
                )
                await self._publish(
                    EventType.SUPER_INBOX_UPDATED,
                    parent_input_id or ready_tickets[0].parent_input_id,
                    {
                        "item_id": bundle_item.item_id,
                        "ticket_ids": bundle_item.ticket_ids,
                        "kind": "review_bundle",
                    },
                )

        ordered_outcomes = tuple(
            outcomes_by_id[t.ticket_id]
            for t in self.ledger.list_tickets(parent_input_id=parent_input_id)
            if t.ticket_id in outcomes_by_id
        )
        integrated_ids = tuple(
            o.ticket.ticket_id for o in ordered_outcomes if o.ticket.state == TicketState.INTEGRATED
        )
        ready_ids = tuple(
            o.ticket.ticket_id
            for o in ordered_outcomes
            if o.ticket.state == TicketState.READY_FOR_REVIEW
        )
        blocked_ids = tuple(
            o.ticket.ticket_id
            for o in ordered_outcomes
            if o.ticket.state == TicketState.BLOCKED_ON_HUMAN
        )
        failed_ids = tuple(
            o.ticket.ticket_id for o in ordered_outcomes if o.ticket.state == TicketState.FAILED
        )

        await self._publish(
            EventType.SUPER_RUN_COMPLETED,
            parent_input_id or "super-agent",
            {
                "integrated": len(integrated_ids),
                "ready_for_review": len(ready_ids),
                "blocked": len(blocked_ids),
                "failed": len(failed_ids),
            },
        )
        return DispatchBatchResult(
            outcomes=ordered_outcomes,
            integrated_ids=integrated_ids,
            ready_for_review_ids=ready_ids,
            blocked_ids=blocked_ids,
            failed_ids=failed_ids,
            inbox_items=tuple(self.ledger.list_inbox(unresolved_only=True)),
        )


__all__ = [
    "ConflictContext",
    "ConflictRemediatorFn",
    "DispatchBatchResult",
    "SubagentTicketWorker",
    "SuperAgentDispatcher",
    "TicketDispatchOutcome",
    "TicketExecutionContext",
    "TicketVerificationResult",
    "TicketVerifierFn",
    "TicketWorkerFn",
    "WorkerExecutionResult",
    "verify_patch_independently",
]
