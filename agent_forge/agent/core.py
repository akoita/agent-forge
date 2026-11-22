"""ReAct loop implementation — the core reasoning + acting cycle."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from agent_forge.agent.approvals import ApprovalGate
from agent_forge.agent.budget import BudgetPolicy, CancellationToken
from agent_forge.agent.models import AgentRun, RunState, ToolInvocation
from agent_forge.agent.persistence import save_run
from agent_forge.agent.prompts import build_system_prompt
from agent_forge.agent.state import transition
from agent_forge.llm.base import LLMConfig, Message, Role
from agent_forge.llm.errors import ToolExecutionError
from agent_forge.observability import (
    CostTracker,
    get_logger,
    print_run_summary,
    save_summary,
    set_trace_context,
    update_iteration,
)
from agent_forge.orchestration.events import Event, EventBus, EventType

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from agent_forge.agent.verification import VerificationResult
    from agent_forge.llm.base import LLMProvider, ToolCall
    from agent_forge.sandbox.base import Sandbox
    from agent_forge.tools.base import ToolRegistry, ToolResult

logger = get_logger("agent_core")

# Retry policy for tool execution (spec § 7.2)
_TOOL_MAX_RETRIES = 1
_TOOL_RETRY_DELAY_S = 2.0


async def react_loop(
    run: AgentRun,
    llm: LLMProvider,
    tools: ToolRegistry,
    sandbox: Sandbox,
    *,
    event_bus: EventBus | None = None,
    verify: Callable[[], Awaitable[VerificationResult]] | None = None,
    emit_lifecycle: bool = True,
    approval_gate: ApprovalGate | None = None,
    on_checkpoint: Callable[[AgentRun], Awaitable[None]] | None = None,
    cancellation_token: CancellationToken | None = None,
) -> AgentRun:
    """Execute the ReAct loop: Observe → Reason (LLM) → Act (Tool) → Repeat.

    Terminates when:
    - LLM stops and any configured verification passes (→ COMPLETED)
    - Max iterations reached (→ TIMEOUT)
    - Token/cost/time budget exceeded (→ TIMEOUT or PAUSED)
    - Cooperative or task cancellation requested (→ CANCELLED or PAUSED)
    - Unrecoverable error (→ FAILED)
    """
    completed_call_ids = _prepare_run_start(run, tools)
    set_trace_context(run.id)
    tracker = CostTracker(run_id=run.id)
    loop_started = time.monotonic()

    llm_config = LLMConfig(
        model=run.config.model,
        temperature=run.config.temperature,
    )
    tool_definitions = tools.list_definitions()
    lifecycle_bus = event_bus if emit_lifecycle else None
    active_gate = _resolve_approval_gate(run, approval_gate)

    await _emit(
        lifecycle_bus,
        EventType.RUN_STARTED,
        run.id,
        {
            "task": run.task,
            "model": run.config.model,
        },
    )

    try:
        await _resume_pending_tool_calls(
            run,
            tools,
            sandbox,
            completed_call_ids,
            event_bus=event_bus,
            approval_gate=active_gate,
            on_checkpoint=on_checkpoint,
        )
        while run.iterations < run.config.max_iterations:
            if await _check_cooperative_cancel(
                run, cancellation_token, on_checkpoint, lifecycle_bus
            ):
                break
            run.iterations += 1
            update_iteration(run.iterations)
            logger.info(
                "iteration_started",
                iteration=run.iterations,
                max_iterations=run.config.max_iterations,
            )
            await _emit(
                event_bus,
                EventType.ITERATION_STARTED,
                run.id,
                {
                    "iteration": run.iterations,
                },
            )

            # 1. REASON — ask the LLM what to do next
            response = await llm.complete(
                messages=run.messages,
                tools=tool_definitions,
                config=llm_config,
            )
            run.total_tokens = run.total_tokens + response.usage
            tracker.record(response.usage, response.model)

            await _emit(
                event_bus,
                EventType.TOKEN_USAGE,
                run.id,
                {
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                    "total_tokens": response.usage.total_tokens,
                    "model": response.model,
                },
            )

            # 2. CHECK BUDGET
            if await _check_budget(
                run,
                tracker,
                time.monotonic() - loop_started,
                on_checkpoint,
                event_bus,
                lifecycle_bus,
            ):
                break

            # 3. CHECK COMPLETION
            if response.finish_reason == "stop" and not response.tool_calls:
                run.messages.append(Message(role=Role.ASSISTANT, content=response.content or ""))
                await _finish_run(run, verify, lifecycle_bus)
                break

            # 4. ACT — execute each tool call
            run.messages.append(
                Message(
                    role=Role.ASSISTANT,
                    content=response.content or "",
                    tool_calls=response.tool_calls,
                )
            )

            await _execute_tool_batch(
                run,
                tools,
                sandbox,
                response.tool_calls,
                completed_call_ids,
                event_bus=event_bus,
                approval_gate=active_gate,
            )

            await _emit(
                event_bus,
                EventType.ITERATION_COMPLETED,
                run.id,
                {
                    "iteration": run.iterations,
                },
            )
            await _emit_checkpoint(run, on_checkpoint, event_bus)

        else:
            # Loop exhausted without breaking → max iterations
            logger.warning(
                "max_iterations_reached",
                max_iterations=run.config.max_iterations,
            )
            transition(run, RunState.TIMEOUT)
            run.error = "Max iterations reached"

    except asyncio.CancelledError:
        await _handle_cancellation(run, on_checkpoint, lifecycle_bus)
        raise

    except Exception as exc:
        logger.exception("unrecoverable_error", exc_info=exc)
        transition(run, RunState.FAILED)
        run.error = str(exc)
        await _emit(
            lifecycle_bus,
            EventType.RUN_FAILED,
            run.id,
            {
                "error": str(exc),
            },
        )

    if run.state != RunState.PAUSED:
        run.completed_at = datetime.now(UTC)

    if run.state == RunState.COMPLETED:
        await _emit(
            lifecycle_bus,
            EventType.RUN_COMPLETED,
            run.id,
            {
                "iterations": run.iterations,
                "total_tokens": run.total_tokens.total_tokens,
            },
        )

    print_run_summary(run, tracker)
    save_summary(run, tracker)
    save_run(run)
    return run


async def _finish_run(
    run: AgentRun,
    verify: Callable[[], Awaitable[VerificationResult]] | None,
    event_bus: EventBus | None,
) -> None:
    if verify is not None:
        run.verification = await verify()
        if not run.verification.passed:
            run.error = run.verification.reason
            transition(run, RunState.FAILED)
            await _emit(event_bus, EventType.RUN_FAILED, run.id, {"error": run.error})
            return
    transition(run, RunState.COMPLETED)


def _prepare_run_start(run: AgentRun, tools: ToolRegistry) -> set[str]:
    if run.state != RunState.RUNNING:
        transition(run, RunState.RUNNING)
    run.error = None
    if not run.messages:
        system_content = run.config.system_prompt or build_system_prompt(
            run.task, tools.list_definitions()
        )
        run.messages.append(Message(role=Role.SYSTEM, content=system_content))
        run.messages.append(Message(role=Role.USER, content=run.task))
    return {msg.tool_call_id for msg in run.messages if msg.role == Role.TOOL and msg.tool_call_id}


async def _execute_tool_batch(
    run: AgentRun,
    tools: ToolRegistry,
    sandbox: Sandbox,
    tool_calls: list[ToolCall],
    completed_call_ids: set[str],
    *,
    event_bus: EventBus | None,
    approval_gate: ApprovalGate | None,
) -> None:
    for tool_call in tool_calls:
        if tool_call.id and tool_call.id in completed_call_ids:
            continue
        await _execute_tool_call(
            run,
            tools,
            sandbox,
            tool_call,
            event_bus=event_bus,
            approval_gate=approval_gate,
        )
        if tool_call.id:
            completed_call_ids.add(tool_call.id)


async def _resume_pending_tool_calls(
    run: AgentRun,
    tools: ToolRegistry,
    sandbox: Sandbox,
    completed_call_ids: set[str],
    *,
    event_bus: EventBus | None,
    approval_gate: ApprovalGate | None,
    on_checkpoint: Callable[[AgentRun], Awaitable[None]] | None,
) -> None:
    last_assistant = next(
        (m for m in reversed(run.messages) if m.role == Role.ASSISTANT),
        None,
    )
    if last_assistant is None or not last_assistant.tool_calls:
        return
    pending = [tc for tc in last_assistant.tool_calls if tc.id not in completed_call_ids]
    if not pending:
        return
    await _execute_tool_batch(
        run,
        tools,
        sandbox,
        pending,
        completed_call_ids,
        event_bus=event_bus,
        approval_gate=approval_gate,
    )
    await _emit_checkpoint(run, on_checkpoint, event_bus)


async def _emit_checkpoint(
    run: AgentRun,
    on_checkpoint: Callable[[AgentRun], Awaitable[None]] | None,
    event_bus: EventBus | None,
) -> None:
    if on_checkpoint is None:
        return
    await on_checkpoint(run)
    await _emit(
        event_bus,
        EventType.CHECKPOINT_SAVED,
        run.id,
        {"iteration": run.iterations},
    )


async def _handle_cancellation(
    run: AgentRun,
    on_checkpoint: Callable[[AgentRun], Awaitable[None]] | None,
    lifecycle_bus: EventBus | None,
) -> None:
    if run.state == RunState.RUNNING:
        transition(run, RunState.PAUSED)
    run.error = "Run paused due to cancellation"
    if on_checkpoint is not None:
        await on_checkpoint(run)
    else:
        save_run(run)
    await _emit(
        lifecycle_bus,
        EventType.RUN_PAUSED,
        run.id,
        {"iteration": run.iterations},
    )


async def _check_cooperative_cancel(
    run: AgentRun,
    cancellation_token: CancellationToken | None,
    on_checkpoint: Callable[[AgentRun], Awaitable[None]] | None,
    lifecycle_bus: EventBus | None,
) -> bool:
    if cancellation_token is None or not cancellation_token.is_cancelled:
        return False
    transition(run, RunState.CANCELLED)
    run.error = cancellation_token.reason
    if on_checkpoint is not None:
        await on_checkpoint(run)
    await _emit(
        lifecycle_bus,
        EventType.RUN_CANCELLED,
        run.id,
        {"reason": cancellation_token.reason, "iteration": run.iterations},
    )
    return True


async def _check_budget(
    run: AgentRun,
    tracker: CostTracker,
    elapsed_seconds: float,
    on_checkpoint: Callable[[AgentRun], Awaitable[None]] | None,
    event_bus: EventBus | None,
    lifecycle_bus: EventBus | None,
) -> bool:
    policy = BudgetPolicy(
        max_tokens=run.config.max_tokens_per_run,
        max_cost_usd=run.config.max_cost_usd,
        max_duration_seconds=run.config.max_duration_seconds,
        on_exceed=run.config.budget_action,
    )
    decision = policy.evaluate(
        total_tokens=run.total_tokens.total_tokens,
        total_cost_usd=tracker.total_cost(),
        elapsed_seconds=elapsed_seconds,
    )
    if not decision.exceeded:
        return False

    logger.warning(
        "budget_exceeded",
        dimension=decision.dimension,
        current=decision.current,
        limit=decision.limit,
        action=decision.action,
    )
    await _emit(
        event_bus,
        EventType.BUDGET_EXCEEDED,
        run.id,
        {
            "dimension": decision.dimension,
            "current": decision.current,
            "limit": decision.limit,
            "action": decision.action,
            "reason": decision.reason,
            "iteration": run.iterations,
        },
    )
    if decision.action == "pause":
        transition(run, RunState.PAUSED)
        run.error = decision.reason
        await _emit_checkpoint(run, on_checkpoint, event_bus)
        await _emit(
            lifecycle_bus,
            EventType.RUN_PAUSED,
            run.id,
            {"iteration": run.iterations, "reason": decision.reason},
        )
        return True

    transition(run, RunState.TIMEOUT)
    run.error = decision.reason
    return True


def _resolve_approval_gate(run: AgentRun, gate: ApprovalGate | None) -> ApprovalGate | None:
    if gate is not None:
        return gate
    if run.config.approval_mode != "auto":
        return ApprovalGate(mode=run.config.approval_mode)
    return None


async def _check_approval(
    run: AgentRun,
    tool_call: ToolCall,
    approval_gate: ApprovalGate | None,
    event_bus: EventBus | None,
) -> bool:
    """Evaluate destructive-operation approval; record denial if rejected."""
    from agent_forge.tools.base import ToolResult

    if approval_gate is None:
        return True

    request, decision = await approval_gate.evaluate(tool_call.name, dict(tool_call.arguments))
    if request is None or decision is None:
        return True

    await _emit(
        event_bus,
        EventType.APPROVAL_REQUESTED,
        run.id,
        {
            "tool_name": request.tool_name,
            "category": request.category,
            "reason": request.reason,
            "iteration": run.iterations,
        },
    )
    await _emit(
        event_bus,
        EventType.APPROVAL_DECIDED,
        run.id,
        {
            "tool_name": request.tool_name,
            "category": request.category,
            "approved": decision.approved,
            "decided_by": decision.decided_by,
            "reason": decision.reason,
            "iteration": run.iterations,
        },
    )
    if decision.approved:
        return True

    denial_msg = f"Approval denied ({request.category}): {request.reason}" + (
        f" — {decision.reason}" if decision.reason else ""
    )
    denied_result = ToolResult(
        output="",
        error=denial_msg,
        exit_code=1,
        metadata={
            "approval_denied": True,
            "category": request.category,
            "decided_by": decision.decided_by,
        },
    )
    run.tool_invocations.append(
        ToolInvocation(
            tool_name=tool_call.name,
            arguments=dict(tool_call.arguments),
            result=denied_result,
            iteration=run.iterations,
            timestamp=datetime.now(UTC),
            duration_ms=0,
        )
    )
    run.messages.append(
        Message(
            role=Role.TOOL,
            content=f"[ERROR] {denial_msg}",
            tool_call_id=tool_call.id,
        )
    )
    await _emit(
        event_bus,
        EventType.TOOL_COMPLETED,
        run.id,
        {
            "tool_name": tool_call.name,
            "error": denial_msg,
            "duration_ms": 0,
            "has_error": True,
        },
    )
    return False


async def _execute_tool_call(
    run: AgentRun,
    tools: ToolRegistry,
    sandbox: Sandbox,
    tool_call: object,
    *,
    event_bus: EventBus | None = None,
    approval_gate: ApprovalGate | None = None,
) -> None:
    """Execute a single tool call and append the result to conversation history.

    Implements retry policies from spec § 7.2 and § 7.3:
    - Unknown tool → inject error listing available tools (no retry)
    - Sandbox transient failure → retry once after 2s
    - Sandbox dies mid-run → attempt one restart
    """
    from agent_forge.llm.base import ToolCall
    from agent_forge.tools.base import ToolResult

    assert isinstance(tool_call, ToolCall)  # noqa: S101

    await _emit(
        event_bus,
        EventType.TOOL_CALLED,
        run.id,
        {
            "tool_name": tool_call.name,
            "arguments": dict(tool_call.arguments),
            "iteration": run.iterations,
        },
    )

    start_time = datetime.now(UTC)

    try:
        tool = tools.get(tool_call.name)
    except KeyError:
        # Unknown tool — inject error and let LLM self-correct (spec § 7.3)
        logger.warning("unknown_tool_requested", tool_name=tool_call.name)
        error_result = ToolResult(
            output="",
            error=f"Unknown tool: '{tool_call.name}'. Available tools: "
            + ", ".join(d.name for d in tools.list_definitions()),
            exit_code=1,
        )
        run.messages.append(
            Message(
                role=Role.TOOL,
                content=error_result.error or "",
                tool_call_id=tool_call.id,
            )
        )
        run.tool_invocations.append(
            ToolInvocation(
                tool_name=tool_call.name,
                arguments=dict(tool_call.arguments),
                result=error_result,
                iteration=run.iterations,
                timestamp=datetime.now(UTC),
                duration_ms=0,
            )
        )
        await _emit(
            event_bus,
            EventType.TOOL_COMPLETED,
            run.id,
            {
                "tool_name": tool_call.name,
                "error": error_result.error,
                "duration_ms": 0,
            },
        )
        return

    if not await _check_approval(run, tool_call, approval_gate, event_bus):
        return

    # Execute with retry on transient sandbox failures (spec § 7.2)
    result = await _execute_with_retry(tool, tool_call, sandbox, run, event_bus)
    duration_ms = result.execution_time_ms

    run.tool_invocations.append(
        ToolInvocation(
            tool_name=tool_call.name,
            arguments=dict(tool_call.arguments),
            result=result,
            iteration=run.iterations,
            timestamp=start_time,
            duration_ms=duration_ms,
        )
    )

    # Build tool message content
    content = result.output
    if result.error:
        content += f"\n[ERROR] {result.error}" if content else f"[ERROR] {result.error}"

    run.messages.append(
        Message(
            role=Role.TOOL,
            content=content,
            tool_call_id=tool_call.id,
        )
    )

    await _emit(
        event_bus,
        EventType.TOOL_COMPLETED,
        run.id,
        {
            "tool_name": tool_call.name,
            "duration_ms": duration_ms,
            "has_error": result.error is not None,
        },
    )

    if result.exit_code == 0 and tool_call.name == "apply_patch" and result.metadata:
        await _emit(
            event_bus,
            EventType.PATCH_APPLIED,
            run.id,
            {
                "changed_files": result.metadata.get("changed_files", []),
                "diff_preview": result.metadata.get("diff_preview", ""),
                "dry_run": bool(result.metadata.get("dry_run", False)),
                "iteration": run.iterations,
            },
        )


async def _execute_with_retry(
    tool: object,
    tool_call: object,
    sandbox: Sandbox,
    run: AgentRun,
    event_bus: EventBus | None,  # noqa: ARG001 — will be used when #80 wires events
) -> ToolResult:
    """Execute a tool with retry on transient sandbox failures.

    Retry policy (spec § 7.2): 1 retry, fixed 2s delay.
    Sandbox restart (spec § 7.3): if sandbox dies, attempt one restart.
    """
    from agent_forge.tools.base import ToolResult

    for attempt in range(_TOOL_MAX_RETRIES + 1):
        try:
            # Check if sandbox is alive; attempt restart if dead (spec § 7.3)
            if not await sandbox.is_alive():
                logger.warning("sandbox_dead_mid_run", task_id=run.id)
                try:
                    await sandbox.stop()
                    await sandbox.start(run.repo_path)
                    logger.info("sandbox_restarted", task_id=run.id)
                except Exception:
                    logger.exception(
                        "sandbox_restart_failed",
                        task_id=run.id,
                    )
                    return ToolResult(
                        output="",
                        error="Sandbox died and could not be restarted",
                        exit_code=1,
                    )

            return await tool.execute(  # type: ignore[attr-defined, no-any-return]
                dict(tool_call.arguments),  # type: ignore[attr-defined]
                sandbox,
            )

        except ToolExecutionError as exc:
            if attempt < _TOOL_MAX_RETRIES:
                logger.warning(
                    "tool_execution_retry",
                    tool_name=tool_call.name,  # type: ignore[attr-defined]
                    attempt=attempt + 1,
                    delay_s=_TOOL_RETRY_DELAY_S,
                    error=str(exc),
                )
                await asyncio.sleep(_TOOL_RETRY_DELAY_S)
            else:
                logger.exception(
                    "tool_execution_failed",
                    tool_name=tool_call.name,  # type: ignore[attr-defined]
                )
                return ToolResult(
                    output="",
                    error=f"Tool execution failed after retry: {exc}",
                    exit_code=1,
                )

    # Should not reach here, but satisfy type checker
    return ToolResult(  # pragma: no cover
        output="",
        error="Unexpected retry exhaustion",
        exit_code=1,
    )


async def _emit(
    bus: EventBus | None,
    event_type: EventType,
    run_id: str,
    data: dict[str, object],
) -> None:
    """Publish an event if an event bus is available."""
    if bus is not None:
        await bus.publish(
            Event(
                type=event_type,
                run_id=run_id,
                timestamp=datetime.now(UTC),
                data=data,
            )
        )
