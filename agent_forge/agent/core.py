"""ReAct loop implementation — the core reasoning + acting cycle."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from agent_forge.agent.models import AgentRun, RunState, ToolInvocation
from agent_forge.agent.persistence import save_run
from agent_forge.agent.prompts import build_system_prompt
from agent_forge.agent.state import transition
from agent_forge.llm.base import LLMConfig, Message, Role
from agent_forge.observability import get_logger, set_trace_context, update_iteration

if TYPE_CHECKING:
    from agent_forge.llm.base import LLMProvider
    from agent_forge.sandbox.base import Sandbox
    from agent_forge.tools.base import ToolRegistry

logger = get_logger("agent_core")


async def react_loop(
    run: AgentRun,
    llm: LLMProvider,
    tools: ToolRegistry,
    sandbox: Sandbox,
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

    try:
        while run.iterations < run.config.max_iterations:
            run.iterations += 1
            update_iteration(run.iterations)
            logger.info(
                "iteration_started",
                iteration=run.iterations,
                max_iterations=run.config.max_iterations,
            )

            # 1. REASON — ask the LLM what to do next
            response = await llm.complete(
                messages=run.messages,
                tools=tool_definitions,
                config=llm_config,
            )
            run.total_tokens = run.total_tokens + response.usage

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
                await _execute_tool_call(run, tools, sandbox, tool_call)

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

    run.completed_at = datetime.now(UTC)
    save_run(run)
    return run


async def _execute_tool_call(
    run: AgentRun,
    tools: ToolRegistry,
    sandbox: Sandbox,
    tool_call: object,
) -> None:
    """Execute a single tool call and append the result to conversation history."""
    from agent_forge.llm.base import ToolCall
    from agent_forge.tools.base import ToolResult

    assert isinstance(tool_call, ToolCall)  # noqa: S101

    try:
        tool = tools.get(tool_call.name)
    except KeyError:
        # Unknown tool — inject error and let LLM self-correct
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
        return

    result = await tool.execute(dict(tool_call.arguments), sandbox)

    run.tool_invocations.append(
        ToolInvocation(
            tool_name=tool_call.name,
            arguments=dict(tool_call.arguments),
            result=result,
            iteration=run.iterations,
            timestamp=datetime.now(UTC),
            duration_ms=result.execution_time_ms,
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
