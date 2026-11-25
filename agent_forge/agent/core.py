"""ReAct loop implementation — the core reasoning + acting cycle."""

from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_forge.agent.approvals import ApprovalGate
from agent_forge.agent.budget import BudgetPolicy, CancellationToken
from agent_forge.agent.context import ContextEngine, estimate_messages_tokens
from agent_forge.agent.contracts import (
    CompletionContract,
    ContractContext,
    coding_fix_contract,
)
from agent_forge.agent.diagnostics import maybe_attach_post_edit_diagnostics
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
from agent_forge.llm.base import LLMConfig, LLMResponse, Message, Role, TokenUsage, ToolCall
from agent_forge.llm.capabilities import ProviderCapabilities, query_provider_capabilities
from agent_forge.llm.errors import ToolExecutionError
from agent_forge.llm.router import RoutingDecision, TurnRoutingContext
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
    if message.response_id:
        payload["response_id"] = message.response_id
    if message.reasoning_blocks:
        payload["reasoning_blocks"] = message.reasoning_blocks
    return payload


def _record_message(run: AgentRun, message: Message, journal: RunJournal) -> None:
    index = len(run.messages)
    run.messages.append(message)
    journal.append(
        "message_recorded",
        _serialize_message_payload(message, index=index),
        iteration=run.iterations,
    )


def _build_token_usage_payload(response: LLMResponse) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "prompt_tokens": response.usage.prompt_tokens,
        "completion_tokens": response.usage.completion_tokens,
        "total_tokens": response.usage.total_tokens,
        "model": response.model,
    }
    if response.reported_model:
        payload["reported_model"] = response.reported_model
    if response.upstream_provider:
        payload["upstream_provider"] = response.upstream_provider
    if response.usage.reported_cost_usd is not None:
        payload["reported_cost_usd"] = response.usage.reported_cost_usd
    return payload


def _record_llm_response(response: LLMResponse, journal: RunJournal, iteration: int) -> None:
    usage_payload: dict[str, Any] = {
        "prompt_tokens": response.usage.prompt_tokens,
        "completion_tokens": response.usage.completion_tokens,
        "total_tokens": response.usage.total_tokens,
    }
    if response.usage.reported_cost_usd is not None:
        usage_payload["reported_cost_usd"] = response.usage.reported_cost_usd
    payload: dict[str, Any] = {
        "model": response.model,
        "reported_model": response.reported_model,
        "finish_reason": response.finish_reason,
        "content": response.content,
        "tool_calls": [asdict(tc) for tc in response.tool_calls],
        "usage": usage_payload,
    }
    if response.upstream_provider:
        payload["upstream_provider"] = response.upstream_provider
    if response.native_finish_reason:
        payload["native_finish_reason"] = response.native_finish_reason
    journal.append(
        "llm_response",
        payload,
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
    completion_contract: CompletionContract | None = None,
    context_engine: ContextEngine | None = None,
    stream_tokens: bool = False,
) -> AgentRun:
    """Execute the ReAct loop: Observe → Reason (LLM) → Act (Tool) → Repeat.

    Terminates when:
    - LLM stops and the completion contract passes (→ COMPLETED)
    - Completion contract fails after verification/remediation (→ VERIFICATION_FAILED)
    - Max iterations reached (→ TIMEOUT)
    - Token/cost/time budget exceeded (→ TIMEOUT or PAUSED)
    - Cooperative or task cancellation/interruption requested (→ CANCELLED or PAUSED)
    - Unrecoverable error (→ FAILED)
    """
    active_context = context_engine
    if active_context is None and run.config.context_enabled:
        active_context = ContextEngine(repo_path=run.repo_path, task=run.task)
    active_journal = journal if journal is not None else RunJournal(run.id)
    active_gate = _resolve_approval_gate(run, approval_gate)
    active_tools, code_bridge = _maybe_wrap_code_mode_tools(
        run,
        tools,
        sandbox=sandbox,
        journal=active_journal,
        approval_gate=active_gate,
        event_bus=event_bus,
    )
    completed_call_ids, completed_results_by_key = _prepare_run_start(
        run,
        active_tools,
        active_journal,
        base_revision=base_revision,
        verify_command=verify_command,
        context_engine=active_context,
    )
    if code_bridge is not None:
        code_bridge.set_completed_results(completed_results_by_key)
    set_trace_context(run.id)
    tracker = CostTracker(run_id=run.id, pricing_overrides=dict(run.config.pricing_overrides))
    loop_started = time.monotonic()

    llm_config = LLMConfig(
        model=run.config.model,
        temperature=run.config.temperature,
        reasoning_effort=run.config.reasoning_effort,
        thinking_mode=run.config.thinking_mode,
        thinking_budget_tokens=run.config.thinking_budget_tokens,
        thinking_level=run.config.thinking_level,
    )
    tool_definitions = active_tools.list_definitions()
    lifecycle_bus = event_bus if emit_lifecycle else None

    await _emit(
        lifecycle_bus,
        EventType.RUN_STARTED,
        run.id,
        {"task": run.task, "model": run.config.model},
    )

    try:
        resumed_stopped = await _resume_pending_tool_calls(
            run,
            active_tools,
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
                active_tools,
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
                completion_contract=completion_contract,
                context_engine=active_context,
                stream_tokens=stream_tokens,
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


def _prepare_turn_context(
    run: AgentRun,
    tool_definitions: list[Any],
    llm_config: LLMConfig,
    context_engine: ContextEngine | None,
    journal: RunJournal,
) -> tuple[list[Message], bool, int]:
    if context_engine is None:
        return run.messages, False, estimate_messages_tokens(run.messages)
    scoped_msg = context_engine.pop_scoped_instruction_message(run)
    if scoped_msg is not None:
        _record_message(run, scoped_msg, journal)
    assembly = context_engine.assemble(run, tool_definitions=tool_definitions)
    llm_config.cache_key = assembly.stable_prefix_hash[:32]
    llm_config.cache_breakpoint_index = assembly.cache_breakpoint_index
    if assembly.compaction_event is not None:
        journal.append(
            "context_compacted",
            assembly.compaction_event.to_dict(),
            iteration=run.iterations,
        )
    run.context_stats = context_engine.stats_dict(assembly)
    return assembly.messages, assembly.compacted_this_turn, assembly.assembled_prompt_tokens


def _select_turn_route(
    run: AgentRun,
    llm: LLMProvider,
    llm_config: LLMConfig,
    tracker: CostTracker,
    journal: RunJournal,
    *,
    estimated_tokens: int,
    consecutive_failures: int,
    remediation_attempts: int,
    last_response_id: str | None,
    active_route: str | None,
) -> tuple[ProviderCapabilities, str | None, str | None]:
    if inspect.getattr_static(llm, "select_for_turn", None) is not None:
        selector = getattr(llm, "select_for_turn", None)
        if callable(selector) and not inspect.iscoroutinefunction(selector):
            routing_ctx = TurnRoutingContext(
                task=run.task,
                iteration=run.iterations,
                estimated_prompt_tokens=estimated_tokens,
                consecutive_failures=consecutive_failures,
                is_remediation=remediation_attempts > 0,
                spent_cost_usd=tracker.total_cost(),
                max_cost_usd=run.config.max_cost_usd,
                spent_tokens=run.total_tokens.total_tokens,
                max_tokens_per_run=run.config.max_tokens_per_run,
            )
            decision = selector(routing_ctx)
            if isinstance(decision, RoutingDecision):
                llm_config.model = decision.candidate.model
                if active_route is not None and active_route != decision.candidate.profile_name:
                    last_response_id = None
                active_route = decision.candidate.profile_name
                journal.append("route_selected", decision.to_dict(), iteration=run.iterations)
                return decision.candidate.capabilities, last_response_id, active_route
    return query_provider_capabilities(llm, llm_config.model), last_response_id, active_route


def _update_consecutive_failures(
    run: AgentRun,
    call_count: int,
    previous_failures: int,
) -> int:
    if call_count <= 0:
        return previous_failures
    recent = run.tool_invocations[-call_count:]
    if any(inv.result.exit_code != 0 for inv in recent):
        return previous_failures + 1
    return 0


def _sync_turn_context_window(
    context_engine: ContextEngine | None,
    provider_caps: ProviderCapabilities,
) -> None:
    if context_engine is not None:
        context_engine.sync_context_window(provider_caps.max_context_tokens)


def _merge_stream_tool_call(acc: list[ToolCall], tc: ToolCall) -> None:
    target = next((item for item in acc if tc.id and item.id == tc.id), None)
    if target is None and not tc.name and acc:
        target = acc[-1]
    if target is None:
        if tc.name:
            acc.append(
                ToolCall(
                    id=tc.id,
                    name=tc.name,
                    arguments=dict(tc.arguments),
                    thought_signature=tc.thought_signature,
                )
            )
        return
    if tc.name:
        target.name = tc.name
    if tc.arguments:
        target.arguments.update(tc.arguments)
    if tc.thought_signature is not None:
        target.thought_signature = tc.thought_signature


def _has_usage(usage: TokenUsage) -> bool:
    return bool(
        usage.total_tokens
        or usage.prompt_tokens
        or usage.completion_tokens
        or usage.reported_cost_usd is not None
    )


class _StreamAccumulator:
    """Assemble streamed :class:`LLMResponse` chunks into a single turn response."""

    def __init__(self, default_model: str) -> None:
        self.model = default_model
        self.content_parts: list[str] = []
        self.tool_calls: list[ToolCall] = []
        self.reasoning_blocks: list[dict[str, Any]] = []
        self.usage = TokenUsage(0, 0, 0)
        self.finish_reason = ""
        self.reported_model: str | None = None
        self.upstream_provider: str | None = None
        self.native_finish_reason: str | None = None
        self.response_id: str | None = None
        self.saw_chunk = False

    def ingest(self, chunk: LLMResponse) -> str | None:
        self.saw_chunk = True
        self.model = chunk.model or self.model
        self.reported_model = chunk.reported_model or self.reported_model
        self.upstream_provider = chunk.upstream_provider or self.upstream_provider
        self.native_finish_reason = chunk.native_finish_reason or self.native_finish_reason
        self.response_id = chunk.response_id or self.response_id
        self.finish_reason = chunk.finish_reason or self.finish_reason
        for tc in chunk.tool_calls:
            _merge_stream_tool_call(self.tool_calls, tc)
        if chunk.reasoning_blocks:
            self.reasoning_blocks.extend(chunk.reasoning_blocks)
        if _has_usage(chunk.usage):
            self.usage = chunk.usage
        if chunk.content:
            self.content_parts.append(chunk.content)
            return chunk.content
        return None

    def build(self) -> LLMResponse:
        resolved_finish = "tool_calls" if self.tool_calls else (self.finish_reason or "stop")
        return LLMResponse(
            content="".join(self.content_parts) if self.content_parts else None,
            tool_calls=self.tool_calls,
            usage=self.usage,
            model=self.model,
            finish_reason=resolved_finish,
            reported_model=self.reported_model,
            upstream_provider=self.upstream_provider,
            native_finish_reason=self.native_finish_reason,
            response_id=self.response_id,
            reasoning_blocks=self.reasoning_blocks,
        )


async def _invoke_llm(
    llm: LLMProvider,
    prompt_messages: list[Message],
    tool_definitions: list[Any],
    llm_config: LLMConfig,
    *,
    stream_tokens: bool,
    event_bus: EventBus | None,
    run_id: str,
    iteration: int,
) -> LLMResponse:
    if not stream_tokens:
        return await llm.complete(
            messages=prompt_messages,
            tools=tool_definitions,
            config=llm_config,
        )

    acc = _StreamAccumulator(llm_config.model)
    try:
        async for chunk in llm.stream(prompt_messages, tools=tool_definitions, config=llm_config):
            delta = acc.ingest(chunk)
            if delta:
                await _emit(
                    event_bus,
                    EventType.ASSISTANT_DELTA,
                    run_id,
                    {"delta": delta, "iteration": iteration},
                )
    except NotImplementedError:
        acc.saw_chunk = False

    if not acc.saw_chunk:
        fallback = await llm.complete(
            messages=prompt_messages,
            tools=tool_definitions,
            config=llm_config,
        )
        if fallback.content:
            await _emit(
                event_bus,
                EventType.ASSISTANT_DELTA,
                run_id,
                {"delta": fallback.content, "iteration": iteration},
            )
        return fallback

    return acc.build()


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
    completion_contract: CompletionContract | None = None,
    context_engine: ContextEngine | None = None,
    stream_tokens: bool = False,
) -> None:
    remediation_attempts = 0
    consecutive_failures = 0
    last_response_id: str | None = None
    active_route: str | None = None
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

        # 1. REASON — prepare context, negotiate capabilities/routing, call LLM
        prompt_messages, compacted_now, est_tokens = _prepare_turn_context(
            run,
            tool_definitions,
            llm_config,
            context_engine,
            journal,
        )
        provider_caps, last_response_id, active_route = _select_turn_route(
            run,
            llm,
            llm_config,
            tracker,
            journal,
            estimated_tokens=est_tokens,
            consecutive_failures=consecutive_failures,
            remediation_attempts=remediation_attempts,
            last_response_id=last_response_id,
            active_route=active_route,
        )
        _sync_turn_context_window(context_engine, provider_caps)
        if compacted_now or not provider_caps.stateful_response_ids:
            last_response_id = None
        llm_config.parallel_tool_calls = provider_caps.parallel_tool_calls
        llm_config.previous_response_id = (
            last_response_id if provider_caps.stateful_response_ids else None
        )
        response = await _invoke_llm(
            llm,
            prompt_messages,
            tool_definitions,
            llm_config,
            stream_tokens=stream_tokens,
            event_bus=event_bus,
            run_id=run.id,
            iteration=run.iterations,
        )
        run.total_tokens = run.total_tokens + response.usage
        tracker.record(
            response.usage,
            response.model or llm_config.model,
            reported_model=response.reported_model,
            upstream_provider=response.upstream_provider,
        )
        _record_llm_response(response, journal, run.iterations)
        last_response_id = (
            response.response_id
            if provider_caps.stateful_response_ids and response.response_id
            else None
        )

        await _emit(
            event_bus,
            EventType.TOKEN_USAGE,
            run.id,
            _build_token_usage_payload(response),
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
                run,
                Message(
                    role=Role.ASSISTANT,
                    content=response.content or "",
                    reasoning_blocks=response.reasoning_blocks or None,
                    response_id=response.response_id,
                ),
                journal,
            )
            terminal = await _finish_run(
                run,
                verify,
                lifecycle_bus,
                journal,
                sandbox=sandbox,
                completion_contract=completion_contract,
                remediation_attempts=remediation_attempts,
            )
            if terminal:
                return
            remediation_attempts += 1
            continue

        # 4. ACT — record message then execute tool calls with per-step journaling
        _record_message(
            run,
            Message(
                role=Role.ASSISTANT,
                content=response.content or "",
                tool_calls=response.tool_calls,
                reasoning_blocks=response.reasoning_blocks or None,
                response_id=response.response_id,
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
            allow_parallel_tools=provider_caps.parallel_tool_calls,
        )
        if stopped:
            return
        consecutive_failures = _update_consecutive_failures(
            run,
            len(response.tool_calls),
            consecutive_failures,
        )

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
    *,
    sandbox: Sandbox,
    completion_contract: CompletionContract | None,
    remediation_attempts: int,
) -> bool:
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

    active_contract = completion_contract or (
        coding_fix_contract(has_verifier=True) if verify is not None else None
    )
    if active_contract is None:
        _transition_and_journal(run, RunState.COMPLETED, journal, reason="completed")
        return True

    evaluation = await active_contract.evaluate(
        ContractContext(
            run=run,
            sandbox=sandbox,
            workspace_path=Path(run.repo_path) if run.repo_path else None,
            verification=run.verification,
        ),
        attempt=remediation_attempts + 1,
    )
    run.contract_evaluation = evaluation
    journal.append(
        "contract_evaluated", evaluation.model_dump(mode="json"), iteration=run.iterations
    )

    if evaluation.passed:
        _transition_and_journal(run, RunState.COMPLETED, journal, reason="completed")
        return True

    if (
        remediation_attempts < active_contract.max_remediation_attempts
        and run.iterations < run.config.max_iterations
    ):
        _record_message(
            run,
            Message(role=Role.USER, content=active_contract.build_remediation_message(evaluation)),
            journal,
        )
        return False

    run.error = (
        run.verification.reason
        if run.verification is not None and not run.verification.passed
        else evaluation.summary_reason
    )
    _transition_and_journal(run, RunState.VERIFICATION_FAILED, journal, reason=run.error)
    await _emit(
        event_bus,
        EventType.RUN_FAILED,
        run.id,
        {
            "state": RunState.VERIFICATION_FAILED.value,
            "error": run.error,
            "contract_id": evaluation.contract_id,
            "unmet_clauses": [
                clause.model_dump(mode="json") for clause in evaluation.unmet_clauses
            ],
        },
    )
    return True


def _build_initial_system_prompt(
    run: AgentRun,
    tools: ToolRegistry,
    context_engine: ContextEngine | None,
) -> str:
    if run.config.system_prompt:
        return run.config.system_prompt
    tool_defs = tools.list_definitions()
    if context_engine is None:
        return build_system_prompt(run.task, tool_defs)
    instructions, repo_map = context_engine.prepare_initial_context()
    return build_system_prompt(
        run.task,
        tool_defs,
        repo_instructions=instructions.rendered or None,
        repo_map=repo_map.rendered or None,
        scoped_instructions=context_engine.task_scoped_instructions_rendered or None,
    )


def _prepare_run_start(
    run: AgentRun,
    tools: ToolRegistry,
    journal: RunJournal,
    *,
    base_revision: str = "",
    verify_command: str | None = None,
    context_engine: ContextEngine | None = None,
) -> tuple[set[str], dict[str, ToolResult]]:
    initial_state = run.state
    if initial_state == RunState.PENDING:
        journal.reset()
        prior_entries = False
    else:
        prior_entries = journal.entry_count > 0
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
        system_content = _build_initial_system_prompt(run, tools, context_engine)
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
    by_key, _ = (
        journal.completed_action_results() if initial_state != RunState.PENDING else ({}, {})
    )
    completed_call_ids = {
        msg.tool_call_id for msg in run.messages if msg.role == Role.TOOL and msg.tool_call_id
    }
    return completed_call_ids, by_key


def _pending_uncached_calls(
    indexed_calls: list[tuple[int, ToolCall]],
    run_id: str,
    iteration: int,
    completed_call_ids: set[str],
    completed_results_by_key: dict[str, ToolResult],
) -> list[tuple[int, ToolCall, str]]:
    pending: list[tuple[int, ToolCall, str]] = []
    for call_index, tool_call in indexed_calls:
        if tool_call.id and tool_call.id in completed_call_ids:
            continue
        key = compute_tool_idempotency_key(
            run_id,
            iteration,
            tool_call.id,
            tool_call.name,
            dict(tool_call.arguments),
            call_index=call_index,
        )
        if key not in completed_results_by_key:
            pending.append((call_index, tool_call, key))
    return pending


def _can_execute_batch_in_parallel(
    indexed_calls: list[tuple[int, ToolCall]],
    pending: list[tuple[int, ToolCall, str]],
    tools: ToolRegistry,
    *,
    allow_parallel_tools: bool,
    approval_gate: ApprovalGate | None,
) -> bool:
    if not allow_parallel_tools or len(pending) < 2 or len(pending) != len(indexed_calls):
        return False
    if approval_gate is not None and approval_gate.mode != "auto":
        return False
    for _, tool_call, _ in pending:
        try:
            tool_obj = tools.get(tool_call.name)
        except KeyError:
            return False
        if getattr(tool_obj, "is_mcp_tool", False) is True:
            return False
        if getattr(tool_obj, "requires_approval", False) is True:
            return False
        if tool_call.name == "spawn_subagent" and (
            str(tool_call.arguments.get("mode", "research")).strip().lower() == "research"
        ):
            continue
        if is_side_effecting_tool(tool_call.name, tool_obj):
            return False
    return True


async def _execute_parallel_read_only_batch(
    run: AgentRun,
    tools: ToolRegistry,
    sandbox: Sandbox,
    pending: list[tuple[int, ToolCall, str]],
    completed_call_ids: set[str],
    completed_results_by_key: dict[str, ToolResult],
    *,
    journal: RunJournal,
    event_bus: EventBus | None,
    on_checkpoint: Callable[[AgentRun], Awaitable[None]] | None,
) -> None:
    coros: list[Awaitable[ToolResult]] = []
    start_time = datetime.now(UTC)
    for _, tool_call, idempotency_key in pending:
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
        coros.append(
            _execute_with_retry(tools.get(tool_call.name), tool_call, sandbox, run, event_bus)
        )

    results = await asyncio.gather(*coros)
    for (_, tool_call, idempotency_key), result in zip(pending, results, strict=True):
        await _record_tool_outcome(
            run,
            tool_call,
            result,
            idempotency_key=idempotency_key,
            side_effecting=False,
            reused=False,
            timestamp=start_time,
            journal=journal,
            event_bus=event_bus,
        )
        completed_results_by_key[idempotency_key] = result
        if tool_call.id:
            completed_call_ids.add(tool_call.id)
        await _emit_checkpoint(run, on_checkpoint, event_bus, journal=journal)


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
    allow_parallel_tools: bool = False,
) -> bool:
    if await _check_cooperative_cancel(
        run,
        cancellation_token,
        on_checkpoint,
        lifecycle_bus,
        journal=journal,
    ):
        return True
    pending = _pending_uncached_calls(
        indexed_calls,
        run.id,
        run.iterations,
        completed_call_ids,
        completed_results_by_key,
    )
    if _can_execute_batch_in_parallel(
        indexed_calls,
        pending,
        tools,
        allow_parallel_tools=allow_parallel_tools,
        approval_gate=approval_gate,
    ):
        await _execute_parallel_read_only_batch(
            run,
            tools,
            sandbox,
            pending,
            completed_call_ids,
            completed_results_by_key,
            journal=journal,
            event_bus=event_bus,
            on_checkpoint=on_checkpoint,
        )
        return False

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
    tool: object = None,
) -> bool:
    """Evaluate destructive-operation approval; record denial if rejected."""
    from agent_forge.agent.approvals import ApprovalGate as _ApprovalGate
    from agent_forge.tools.base import ToolResult

    active_gate = approval_gate
    if active_gate is None:
        tool_mode = getattr(tool, "approval_mode", "inherit")
        if isinstance(tool_mode, str) and tool_mode.strip().lower() in ("prompt", "deny"):
            active_gate = _ApprovalGate(mode=tool_mode)
        else:
            return True

    request, decision = await active_gate.evaluate(
        tool_call.name,
        dict(tool_call.arguments),
        tool=tool,
    )
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
        tool=tool,
    ):
        return

    result = await _execute_with_retry(tool, tool_call, sandbox, run, event_bus)
    result = await maybe_attach_post_edit_diagnostics(
        run,
        tool_call.name,
        dict(tool_call.arguments),
        result,
        sandbox,
    )
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

            call_args = dict(tool_call.arguments)  # type: ignore[attr-defined]
            if getattr(tool, "name", "") == "execute_code":
                call_args.setdefault("_tool_call_id", getattr(tool_call, "id", ""))
            return await tool.execute(  # type: ignore[attr-defined, no-any-return]
                call_args,
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


def _maybe_wrap_code_mode_tools(
    run: AgentRun,
    tools: ToolRegistry,
    *,
    sandbox: Sandbox,
    journal: RunJournal,
    approval_gate: ApprovalGate | None,
    event_bus: EventBus | None,
) -> tuple[ToolRegistry, Any | None]:
    """Wrap *tools* in the 2-tool Code Mode surface when ``execution_mode == 'code_mode'``."""
    mode = str(getattr(run.config, "execution_mode", "tool_calling")).strip().lower()
    if mode != "code_mode":
        return tools, None
    from agent_forge.agent.code_mode import build_code_mode_registry

    return build_code_mode_registry(
        tools,
        run=run,
        sandbox=sandbox,
        journal=journal,
        approval_gate=approval_gate,
        event_bus=event_bus,
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
