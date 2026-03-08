"""ReAct loop implementation — the core reasoning + acting cycle."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

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
    from agent_forge.llm.base import LLMProvider
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
) -> AgentRun:
    """Execute the ReAct loop: Observe → Reason (LLM) → Act (Tool) → Repeat.

    Terminates when:
    - LLM returns no tool calls (task complete → COMPLETED)
    - Max iterations reached (→ TIMEOUT)
    - Token budget exceeded (→ TIMEOUT)
    - Unrecoverable error (→ FAILED)
    """
    transition(run, RunState.RUNNING)
    set_trace_context(run.id)
    tracker = CostTracker(run_id=run.id)

    # Build system prompt from task + tool definitions
    system_content = run.config.system_prompt or build_system_prompt(
        run.task, tools.list_definitions()
    )
    run.messages.append(Message(role=Role.SYSTEM, content=system_content))
    run.messages.append(Message(role=Role.USER, content=run.task))

    llm_config = LLMConfig(
        model=run.config.model,
        temperature=run.config.temperature,
    )
    tool_definitions = tools.list_definitions()

    await _emit(event_bus, EventType.RUN_STARTED, run.id, {
        "task": run.task,
        "model": run.config.model,
    })

    try:
        while run.iterations < run.config.max_iterations:
            run.iterations += 1
            update_iteration(run.iterations)
            logger.info(
                "iteration_started",
                iteration=run.iterations,
                max_iterations=run.config.max_iterations,
            )
            await _emit(event_bus, EventType.ITERATION_STARTED, run.id, {
                "iteration": run.iterations,
            })

            # 1. REASON — ask the LLM what to do next
            response = await llm.complete(
                messages=run.messages,
                tools=tool_definitions,
                config=llm_config,
            )
            run.total_tokens = run.total_tokens + response.usage
            tracker.record(response.usage, response.model)

            await _emit(event_bus, EventType.TOKEN_USAGE, run.id, {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
                "model": response.model,
            })

            # 2. CHECK BUDGET
            if run.total_tokens.total_tokens > run.config.max_tokens_per_run:
                logger.warning(
                    "token_budget_exceeded",
                    total_tokens=run.total_tokens.total_tokens,
                    budget=run.config.max_tokens_per_run,
                )
                transition(run, RunState.TIMEOUT)
                run.error = "Token budget exceeded"
                break

            # 3. CHECK COMPLETION
            if response.finish_reason == "stop" and not response.tool_calls:
                run.messages.append(Message(role=Role.ASSISTANT, content=response.content or ""))
                transition(run, RunState.COMPLETED)
                break

            # 4. ACT — execute each tool call
            run.messages.append(
                Message(
                    role=Role.ASSISTANT,
                    content=response.content or "",
                    tool_calls=response.tool_calls,
                )
            )

            for tool_call in response.tool_calls:
                await _execute_tool_call(
                    run, tools, sandbox, tool_call, event_bus=event_bus,
                )

            await _emit(event_bus, EventType.ITERATION_COMPLETED, run.id, {
                "iteration": run.iterations,
            })

        else:
            # Loop exhausted without breaking → max iterations
            logger.warning(
                "max_iterations_reached",
                max_iterations=run.config.max_iterations,
            )
            transition(run, RunState.TIMEOUT)
            run.error = "Max iterations reached"

    except Exception as exc:
        logger.exception("unrecoverable_error", exc_info=exc)
        transition(run, RunState.FAILED)
        run.error = str(exc)
        await _emit(event_bus, EventType.RUN_FAILED, run.id, {
            "error": str(exc),
        })

    run.completed_at = datetime.now(UTC)

    if run.state == RunState.COMPLETED:
        await _emit(event_bus, EventType.RUN_COMPLETED, run.id, {
            "iterations": run.iterations,
            "total_tokens": run.total_tokens.total_tokens,
        })

    print_run_summary(run, tracker)
    save_summary(run, tracker)
    save_run(run)
    return run


async def _execute_tool_call(
    run: AgentRun,
    tools: ToolRegistry,
    sandbox: Sandbox,
    tool_call: object,
    *,
    event_bus: EventBus | None = None,
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

    await _emit(event_bus, EventType.TOOL_CALLED, run.id, {
        "tool_name": tool_call.name,
        "arguments": dict(tool_call.arguments),
        "iteration": run.iterations,
    })

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
        await _emit(event_bus, EventType.TOOL_COMPLETED, run.id, {
            "tool_name": tool_call.name,
            "error": error_result.error,
            "duration_ms": 0,
        })
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

    await _emit(event_bus, EventType.TOOL_COMPLETED, run.id, {
        "tool_name": tool_call.name,
        "duration_ms": duration_ms,
        "has_error": result.error is not None,
    })


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
                        "sandbox_restart_failed", task_id=run.id,
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
        output="", error="Unexpected retry exhaustion", exit_code=1,
    )


async def _emit(
    bus: EventBus | None,
    event_type: EventType,
    run_id: str,
    data: dict[str, object],
) -> None:
    """Publish an event if an event bus is available."""
    if bus is not None:
        await bus.publish(Event(
            type=event_type,
            run_id=run_id,
            timestamp=datetime.now(UTC),
            data=data,
        ))
