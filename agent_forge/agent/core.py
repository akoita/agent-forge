"""ReAct loop implementation — the core reasoning + acting cycle."""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from agent_forge.agent.approvals import ApprovalGate
from agent_forge.agent.budget import BudgetPolicy, CancellationToken
from agent_forge.agent.journal import (
    RunJournal,
    compute_tool_idempotency_key,
    format_tool_output,
    is_side_effecting_tool,
)
from agent_forge.agent.models import AgentRun, RunState, ToolInvocation
from agent_forge.agent.persistence import save_run
from agent_forge.agent.prompts import build_system_prompt
from agent_forge.agent.state import transition
from agent_forge.llm.base import LLMConfig, LLMResponse, Message, Role, ToolCall
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
    from agent_forge.llm.base import LLMProvider
    from agent_forge.sandbox.base import Sandbox
    from agent_forge.tools.base import ToolRegistry, ToolResult

logger = get_logger("agent_core")

# Retry policy for tool execution (spec § 7.2)
_TOOL_MAX_RETRIES = 1
_TOOL_RETRY_DELAY_S = 2.0


def _transition_and_journal(
    run: AgentRun,
    new_state: RunState,
    journal: RunJournal,
    *,
    reason: str = "",
) -> None:
    from_state = run.state.value
    transition(run, new_state)
    journal.append(
        "state_transition",
        {
            "from_state": from_state,
            "to_state": new_state.value,
            "reason": reason,
        },
        iteration=run.iterations,
    )


def _serialize_message_payload(message: Message, *, index: int) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "index": index,
        "role": message.role.value,
        "content": message.content,
    }
    if message.tool_call_id:
        payload["tool_call_id"] = message.tool_call_id
    if message.tool_calls:
        payload["tool_calls"] = [asdict(tc) for tc in message.tool_calls]
    return payload


def _record_message(run: AgentRun, message: Message, journal: RunJournal) -> None:
    index = len(run.messages)
    run.messages.append(message)
    journal.append(
        "message_recorded",
        _serialize_message_payload(message, index=index),
        iteration=run.iterations,
    )


def _record_llm_response(response: LLMResponse, journal: RunJournal, iteration: int) -> None:
    journal.append(
        "llm_response",
        {
            "model": response.model,
            "reported_model": response.reported_model,
            "finish_reason": response.finish_reason,
            "content": response.content,
            "tool_calls": [asdict(tc) for tc in response.tool_calls],
            "usage": {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            },
        },
        iteration=iteration,
    )


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
    journal: RunJournal | None = None,
    raise_on_cancel: bool = True,
    base_revision: str = "",
    verify_command: str | None = None,
) -> AgentRun:
    """Execute the ReAct loop: Observe → Reason (LLM) → Act (Tool) → Repeat.

    Terminates when:
    - LLM stops and any configured verification passes (→ COMPLETED)
    - Max iterations reached (→ TIMEOUT)
    - Token/cost/time budget exceeded (→ TIMEOUT or PAUSED)
    - Cooperative or task cancellation/interruption requested (→ CANCELLED or PAUSED)
    - Unrecoverable error (→ FAILED)
    """
    active_journal = journal if journal is not None else RunJournal(run.id)
    completed_call_ids, completed_results_by_key = _prepare_run_start(
        run,
        tools,
        active_journal,
        base_revision=base_revision,
        verify_command=verify_command,
    )
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
        {"task": run.task, "model": run.config.model},
    )

    try:
        resumed_stopped = await _resume_pending_tool_calls(
            run,
            tools,
            sandbox,
            completed_call_ids,
            completed_results_by_key,
            journal=active_journal,
            event_bus=event_bus,
            lifecycle_bus=lifecycle_bus,
            approval_gate=active_gate,
            on_checkpoint=on_checkpoint,
            cancellation_token=cancellation_token,
        )
        if not resumed_stopped:
            await _run_loop_iterations(
                run,
                llm,
                tools,
                sandbox,
                tool_definitions=tool_definitions,
                llm_config=llm_config,
                tracker=tracker,
                loop_started=loop_started,
                completed_call_ids=completed_call_ids,
                completed_results_by_key=completed_results_by_key,
                journal=active_journal,
                event_bus=event_bus,
                lifecycle_bus=lifecycle_bus,
                verify=verify,
                approval_gate=active_gate,
                on_checkpoint=on_checkpoint,
                cancellation_token=cancellation_token,
            )

    except asyncio.CancelledError:
        await _handle_cancellation(
            run,
            on_checkpoint,
            lifecycle_bus,
            active_journal,
            cancellation_token,
        )
        interrupted = cancellation_token is not None and cancellation_token.is_interrupted
        if raise_on_cancel and not interrupted:
            raise

    except Exception as exc:
        logger.exception("unrecoverable_error", exc_info=exc)
        _transition_and_journal(run, RunState.FAILED, active_journal, reason=str(exc))
        run.error = str(exc)
        await _emit(
            lifecycle_bus,
            EventType.RUN_FAILED,
            run.id,
            {"error": str(exc)},
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


async def _run_loop_iterations(
    run: AgentRun,
    llm: LLMProvider,
    tools: ToolRegistry,
    sandbox: Sandbox,
    *,
    tool_definitions: list[Any],
    llm_config: LLMConfig,
    tracker: CostTracker,
    loop_started: float,
    completed_call_ids: set[str],
    completed_results_by_key: dict[str, ToolResult],
    journal: RunJournal,
    event_bus: EventBus | None,
    lifecycle_bus: EventBus | None,
    verify: Callable[[], Awaitable[VerificationResult]] | None,
    approval_gate: ApprovalGate | None,
    on_checkpoint: Callable[[AgentRun], Awaitable[None]] | None,
    cancellation_token: CancellationToken | None,
) -> None:
    while run.iterations < run.config.max_iterations:
        if await _check_cooperative_cancel(
            run,
            cancellation_token,
            on_checkpoint,
            lifecycle_bus,
            journal=journal,
        ):
            return
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
            {"iteration": run.iterations},
        )

        # 1. REASON — ask the LLM what to do next
        response = await llm.complete(
            messages=run.messages,
            tools=tool_definitions,
            config=llm_config,
        )
        run.total_tokens = run.total_tokens + response.usage
        tracker.record(response.usage, response.model)
        _record_llm_response(response, journal, run.iterations)

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

        # 2. CHECK BUDGET & INTERRUPT
        if await _check_budget(
            run,
            tracker,
            time.monotonic() - loop_started,
            on_checkpoint,
            event_bus,
            lifecycle_bus,
            journal=journal,
        ):
            return
        if await _check_cooperative_cancel(
            run,
            cancellation_token,
            on_checkpoint,
            lifecycle_bus,
            journal=journal,
        ):
            return

        # 3. CHECK COMPLETION
        if response.finish_reason == "stop" and not response.tool_calls:
            _record_message(
                run, Message(role=Role.ASSISTANT, content=response.content or ""), journal
            )
            await _finish_run(run, verify, lifecycle_bus, journal)
            return

        # 4. ACT — record message then execute each tool call with per-step journaling
        _record_message(
            run,
            Message(
                role=Role.ASSISTANT,
                content=response.content or "",
                tool_calls=response.tool_calls,
            ),
            journal,
        )

        stopped = await _execute_tool_batch(
            run,
            tools,
            sandbox,
            list(enumerate(response.tool_calls)),
            completed_call_ids,
            completed_results_by_key,
            journal=journal,
            event_bus=event_bus,
            lifecycle_bus=lifecycle_bus,
            approval_gate=approval_gate,
            on_checkpoint=on_checkpoint,
            cancellation_token=cancellation_token,
        )
        if stopped:
            return

        await _emit(
            event_bus,
            EventType.ITERATION_COMPLETED,
            run.id,
            {"iteration": run.iterations},
        )
        if not response.tool_calls:
            await _emit_checkpoint(run, on_checkpoint, event_bus, journal=journal)

    # Loop exhausted without breaking → max iterations
    logger.warning(
        "max_iterations_reached",
        max_iterations=run.config.max_iterations,
    )
    _transition_and_journal(run, RunState.TIMEOUT, journal, reason="Max iterations reached")
    run.error = "Max iterations reached"


async def _finish_run(
    run: AgentRun,
    verify: Callable[[], Awaitable[VerificationResult]] | None,
    event_bus: EventBus | None,
    journal: RunJournal,
) -> None:
    if verify is not None:
        run.verification = await verify()
        journal.append(
            "verification_recorded",
            {
                "base_revision": run.verification.base_revision,
                "reason": run.verification.reason,
                "passed": run.verification.passed,
                "command": run.verification.command,
                "exit_code": run.verification.exit_code,
                "patch": run.verification.patch,
                "stdout": run.verification.stdout,
                "stderr": run.verification.stderr,
            },
            iteration=run.iterations,
        )
        if not run.verification.passed:
            run.error = run.verification.reason
            _transition_and_journal(run, RunState.FAILED, journal, reason=run.error)
            await _emit(event_bus, EventType.RUN_FAILED, run.id, {"error": run.error})
            return
    _transition_and_journal(run, RunState.COMPLETED, journal, reason="completed")


def _prepare_run_start(
    run: AgentRun,
    tools: ToolRegistry,
    journal: RunJournal,
    *,
    base_revision: str = "",
    verify_command: str | None = None,
) -> tuple[set[str], dict[str, ToolResult]]:
    prior_entries = journal.entry_count > 0
    initial_state = run.state
    start_data: dict[str, Any] = {
        "task": run.task,
        "model": run.config.model,
        "initial_state": initial_state.value,
        "resumed": initial_state != RunState.PENDING,
        "base_revision": base_revision,
        "verify_command": verify_command,
    }
    if initial_state == RunState.PENDING:
        journal.append("run_started", start_data, iteration=run.iterations)
        _transition_and_journal(run, RunState.RUNNING, journal, reason="run_started")
    elif initial_state == RunState.PAUSED:
        journal.append("run_started", start_data, iteration=run.iterations)
        _transition_and_journal(run, RunState.RUNNING, journal, reason="resumed")
    elif initial_state == RunState.RUNNING:
        journal.append("run_started", start_data, iteration=run.iterations)
        journal.append(
            "run_interrupted",
            {
                "source": "crash_recovery",
                "state": RunState.PAUSED.value,
                "reason": "Recovered after unexpected termination",
                "recoverable": True,
            },
            iteration=run.iterations,
        )
        journal.append(
            "state_transition",
            {
                "from_state": RunState.RUNNING.value,
                "to_state": RunState.PAUSED.value,
                "reason": "crash_recovery",
            },
            iteration=run.iterations,
        )
        journal.append(
            "state_transition",
            {
                "from_state": RunState.PAUSED.value,
                "to_state": RunState.RUNNING.value,
                "reason": "resumed",
            },
            iteration=run.iterations,
        )
    else:
        _transition_and_journal(run, RunState.RUNNING, journal, reason="invalid_transition")

    run.error = None
    if not run.messages:
        system_content = run.config.system_prompt or build_system_prompt(
            run.task, tools.list_definitions()
        )
        _record_message(run, Message(role=Role.SYSTEM, content=system_content), journal)
        _record_message(run, Message(role=Role.USER, content=run.task), journal)
    elif not prior_entries:
        for idx, msg in enumerate(run.messages):
            journal.append(
                "message_recorded",
                _serialize_message_payload(msg, index=idx),
                iteration=run.iterations,
            )

    save_run(run)
    by_key, _ = journal.completed_action_results()
    completed_call_ids = {
        msg.tool_call_id for msg in run.messages if msg.role == Role.TOOL and msg.tool_call_id
    }
    return completed_call_ids, by_key


async def _execute_tool_batch(
    run: AgentRun,
    tools: ToolRegistry,
    sandbox: Sandbox,
    indexed_calls: list[tuple[int, ToolCall]],
    completed_call_ids: set[str],
    completed_results_by_key: dict[str, ToolResult],
    *,
    journal: RunJournal,
    event_bus: EventBus | None,
    lifecycle_bus: EventBus | None,
    approval_gate: ApprovalGate | None,
    on_checkpoint: Callable[[AgentRun], Awaitable[None]] | None,
    cancellation_token: CancellationToken | None,
) -> bool:
    for call_index, tool_call in indexed_calls:
        if await _check_cooperative_cancel(
            run,
            cancellation_token,
            on_checkpoint,
            lifecycle_bus,
            journal=journal,
        ):
            return True
        if tool_call.id and tool_call.id in completed_call_ids:
            continue

        idempotency_key = compute_tool_idempotency_key(
            run.id,
            run.iterations,
            tool_call.id,
            tool_call.name,
            dict(tool_call.arguments),
            call_index=call_index,
        )
        if idempotency_key in completed_results_by_key:
            await _reuse_recorded_tool_result(
                run,
                tool_call,
                completed_results_by_key[idempotency_key],
                idempotency_key=idempotency_key,
                tools=tools,
                journal=journal,
                event_bus=event_bus,
            )
            if tool_call.id:
                completed_call_ids.add(tool_call.id)
            await _emit_checkpoint(run, on_checkpoint, event_bus, journal=journal)
            continue

        await _execute_tool_call(
            run,
            tools,
            sandbox,
            tool_call,
            idempotency_key=idempotency_key,
            journal=journal,
            event_bus=event_bus,
            approval_gate=approval_gate,
            completed_results_by_key=completed_results_by_key,
        )
        if tool_call.id:
            completed_call_ids.add(tool_call.id)
        await _emit_checkpoint(run, on_checkpoint, event_bus, journal=journal)
    return False


async def _resume_pending_tool_calls(
    run: AgentRun,
    tools: ToolRegistry,
    sandbox: Sandbox,
    completed_call_ids: set[str],
    completed_results_by_key: dict[str, ToolResult],
    *,
    journal: RunJournal,
    event_bus: EventBus | None,
    lifecycle_bus: EventBus | None,
    approval_gate: ApprovalGate | None,
    on_checkpoint: Callable[[AgentRun], Awaitable[None]] | None,
    cancellation_token: CancellationToken | None,
) -> bool:
    last_assistant = next(
        (m for m in reversed(run.messages) if m.role == Role.ASSISTANT),
        None,
    )
    if last_assistant is None or not last_assistant.tool_calls:
        return False
    pending = [
        (idx, tc)
        for idx, tc in enumerate(last_assistant.tool_calls)
        if tc.id not in completed_call_ids
    ]
    if not pending:
        return False
    return await _execute_tool_batch(
        run,
        tools,
        sandbox,
        pending,
        completed_call_ids,
        completed_results_by_key,
        journal=journal,
        event_bus=event_bus,
        lifecycle_bus=lifecycle_bus,
        approval_gate=approval_gate,
        on_checkpoint=on_checkpoint,
        cancellation_token=cancellation_token,
    )


async def _emit_checkpoint(
    run: AgentRun,
    on_checkpoint: Callable[[AgentRun], Awaitable[None]] | None,
    event_bus: EventBus | None,
    *,
    journal: RunJournal,
) -> None:
    if on_checkpoint is None:
        return
    await on_checkpoint(run)
    journal.append(
        "checkpoint_saved",
        {"iteration": run.iterations, "state": run.state.value},
        iteration=run.iterations,
    )
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
    journal: RunJournal,
    cancellation_token: CancellationToken | None,
) -> None:
    interrupted = cancellation_token is not None and cancellation_token.is_interrupted
    source = "signal_interrupt" if interrupted else "task_cancelled"
    reason = (
        cancellation_token.reason
        if interrupted and cancellation_token is not None and cancellation_token.reason
        else "Run paused due to cancellation"
    )
    journal.append(
        "run_interrupted",
        {
            "source": source,
            "state": RunState.PAUSED.value,
            "reason": reason,
            "recoverable": True,
        },
        iteration=run.iterations,
    )
    if run.state == RunState.RUNNING:
        _transition_and_journal(run, RunState.PAUSED, journal, reason=reason)
    run.error = reason
    if on_checkpoint is not None:
        await on_checkpoint(run)
        journal.append(
            "checkpoint_saved",
            {"iteration": run.iterations, "state": run.state.value},
            iteration=run.iterations,
        )
    else:
        save_run(run)
    await _emit(
        lifecycle_bus,
        EventType.RUN_PAUSED,
        run.id,
        {"iteration": run.iterations, "reason": reason},
    )


async def _check_cooperative_cancel(
    run: AgentRun,
    cancellation_token: CancellationToken | None,
    on_checkpoint: Callable[[AgentRun], Awaitable[None]] | None,
    lifecycle_bus: EventBus | None,
    *,
    journal: RunJournal,
) -> bool:
    if cancellation_token is None or not cancellation_token.is_stop_requested:
        return False

    if cancellation_token.is_interrupted:
        journal.append(
            "run_interrupted",
            {
                "source": "cooperative_interrupt",
                "state": RunState.PAUSED.value,
                "reason": cancellation_token.reason,
                "recoverable": True,
            },
            iteration=run.iterations,
        )
        _transition_and_journal(run, RunState.PAUSED, journal, reason=cancellation_token.reason)
        run.error = cancellation_token.reason
        if on_checkpoint is not None:
            await on_checkpoint(run)
        else:
            save_run(run)
        await _emit(
            lifecycle_bus,
            EventType.RUN_PAUSED,
            run.id,
            {"reason": cancellation_token.reason, "iteration": run.iterations},
        )
        return True

    journal.append(
        "run_interrupted",
        {
            "source": "cooperative_cancel",
            "state": RunState.CANCELLED.value,
            "reason": cancellation_token.reason,
            "recoverable": False,
        },
        iteration=run.iterations,
    )
    _transition_and_journal(run, RunState.CANCELLED, journal, reason=cancellation_token.reason)
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
    *,
    journal: RunJournal,
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
        _transition_and_journal(run, RunState.PAUSED, journal, reason=decision.reason)
        run.error = decision.reason
        await _emit_checkpoint(run, on_checkpoint, event_bus, journal=journal)
        await _emit(
            lifecycle_bus,
            EventType.RUN_PAUSED,
            run.id,
            {"iteration": run.iterations, "reason": decision.reason},
        )
        return True

    _transition_and_journal(run, RunState.TIMEOUT, journal, reason=decision.reason)
    run.error = decision.reason
    return True


def _resolve_approval_gate(run: AgentRun, gate: ApprovalGate | None) -> ApprovalGate | None:
    if gate is not None:
        return gate
    if run.config.approval_mode != "auto":
        return ApprovalGate(mode=run.config.approval_mode)
    return None


async def _record_tool_outcome(
    run: AgentRun,
    tool_call: ToolCall,
    result: ToolResult,
    *,
    idempotency_key: str,
    side_effecting: bool,
    reused: bool,
    timestamp: datetime,
    journal: RunJournal,
    event_bus: EventBus | None,
) -> None:
    duration_ms = 0 if reused else result.execution_time_ms
    run.tool_invocations.append(
        ToolInvocation(
            tool_name=tool_call.name,
            arguments=dict(tool_call.arguments),
            result=result,
            iteration=run.iterations,
            timestamp=timestamp,
            duration_ms=duration_ms,
        )
    )
    journal.append(
        "action_result",
        {
            "tool_call_id": tool_call.id,
            "idempotency_key": idempotency_key,
            "tool_name": tool_call.name,
            "arguments": dict(tool_call.arguments),
            "side_effecting": side_effecting,
            "reused": reused,
            "duration_ms": duration_ms,
            "result": {
                "output": result.output,
                "error": result.error,
                "exit_code": result.exit_code,
                "execution_time_ms": result.execution_time_ms,
                "metadata": result.metadata,
            },
        },
        iteration=run.iterations,
    )
    _record_message(
        run,
        Message(
            role=Role.TOOL,
            content=format_tool_output(result),
            tool_call_id=tool_call.id,
        ),
        journal,
    )
    await _emit(
        event_bus,
        EventType.TOOL_COMPLETED,
        run.id,
        {
            "tool_name": tool_call.name,
            "duration_ms": duration_ms,
            "has_error": result.error is not None,
            "reused": reused,
        },
    )


async def _reuse_recorded_tool_result(
    run: AgentRun,
    tool_call: ToolCall,
    cached_result: ToolResult,
    *,
    idempotency_key: str,
    tools: ToolRegistry,
    journal: RunJournal,
    event_bus: EventBus | None,
) -> None:
    tool_obj: object = None
    try:
        tool_obj = tools.get(tool_call.name)
    except KeyError:
        tool_obj = None
    side_effecting = is_side_effecting_tool(tool_call.name, tool_obj)
    journal.append(
        "action_requested",
        {
            "tool_call_id": tool_call.id,
            "idempotency_key": idempotency_key,
            "tool_name": tool_call.name,
            "arguments": dict(tool_call.arguments),
            "side_effecting": side_effecting,
        },
        iteration=run.iterations,
    )
    await _record_tool_outcome(
        run,
        tool_call,
        cached_result,
        idempotency_key=idempotency_key,
        side_effecting=side_effecting,
        reused=True,
        timestamp=datetime.now(UTC),
        journal=journal,
        event_bus=event_bus,
    )


async def _check_approval(
    run: AgentRun,
    tool_call: ToolCall,
    approval_gate: ApprovalGate | None,
    event_bus: EventBus | None,
    *,
    idempotency_key: str,
    side_effecting: bool,
    journal: RunJournal,
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
    await _record_tool_outcome(
        run,
        tool_call,
        denied_result,
        idempotency_key=idempotency_key,
        side_effecting=side_effecting,
        reused=False,
        timestamp=datetime.now(UTC),
        journal=journal,
        event_bus=event_bus,
    )
    return False


async def _execute_tool_call(
    run: AgentRun,
    tools: ToolRegistry,
    sandbox: Sandbox,
    tool_call: ToolCall,
    *,
    idempotency_key: str,
    journal: RunJournal,
    event_bus: EventBus | None = None,
    approval_gate: ApprovalGate | None = None,
    completed_results_by_key: dict[str, ToolResult] | None = None,
) -> None:
    """Execute a single tool call, journal before and after, and record the outcome."""
    from agent_forge.tools.base import ToolResult

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
        tool: object = tools.get(tool_call.name)
    except KeyError:
        logger.warning("unknown_tool_requested", tool_name=tool_call.name)
        journal.append(
            "action_requested",
            {
                "tool_call_id": tool_call.id,
                "idempotency_key": idempotency_key,
                "tool_name": tool_call.name,
                "arguments": dict(tool_call.arguments),
                "side_effecting": False,
            },
            iteration=run.iterations,
        )
        error_result = ToolResult(
            output="",
            error=f"Unknown tool: '{tool_call.name}'. Available tools: "
            + ", ".join(d.name for d in tools.list_definitions()),
            exit_code=1,
        )
        await _record_tool_outcome(
            run,
            tool_call,
            error_result,
            idempotency_key=idempotency_key,
            side_effecting=False,
            reused=False,
            timestamp=datetime.now(UTC),
            journal=journal,
            event_bus=event_bus,
        )
        if completed_results_by_key is not None:
            completed_results_by_key[idempotency_key] = error_result
        return

    side_effecting = is_side_effecting_tool(tool_call.name, tool)
    journal.append(
        "action_requested",
        {
            "tool_call_id": tool_call.id,
            "idempotency_key": idempotency_key,
            "tool_name": tool_call.name,
            "arguments": dict(tool_call.arguments),
            "side_effecting": side_effecting,
        },
        iteration=run.iterations,
    )

    if not await _check_approval(
        run,
        tool_call,
        approval_gate,
        event_bus,
        idempotency_key=idempotency_key,
        side_effecting=side_effecting,
        journal=journal,
    ):
        return

    result = await _execute_with_retry(tool, tool_call, sandbox, run, event_bus)
    await _record_tool_outcome(
        run,
        tool_call,
        result,
        idempotency_key=idempotency_key,
        side_effecting=side_effecting,
        reused=False,
        timestamp=start_time,
        journal=journal,
        event_bus=event_bus,
    )
    if completed_results_by_key is not None:
        completed_results_by_key[idempotency_key] = result

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
