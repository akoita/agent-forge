"""Tool wrapper delegating focused subtasks to bounded child subagents."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Literal, cast

from agent_forge.tools.base import Tool, ToolResult

if TYPE_CHECKING:
    from agent_forge.agent.subagent import SubagentRunner
    from agent_forge.sandbox.base import Sandbox


class SpawnSubagentTool(Tool):
    """Delegate a focused research or worktree-isolated coding subtask to a subagent."""

    def __init__(self, runner: SubagentRunner) -> None:
        self._runner = runner

    def set_runner(self, runner: SubagentRunner) -> None:
        """Bind an updated ``SubagentRunner`` for the current run."""
        self._runner = runner

    @property
    def name(self) -> str:
        return "spawn_subagent"

    @property
    def description(self) -> str:
        return (
            "Delegate a focused subtask to a bounded child subagent. "
            "Use mode='research' (default, read-only) for codebase exploration without bloating "
            "parent context, or mode='worker' for isolated file edits in a dedicated Git worktree."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Clear, self-contained instruction for the child subagent.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["research", "worker"],
                    "description": "Execution mode: read-only 'research' or isolated 'worker'.",
                    "default": "research",
                },
                "max_iterations": {
                    "type": "integer",
                    "description": "Optional iteration cap clamped to harness subagent limits.",
                    "minimum": 1,
                },
            },
            "required": ["task"],
        }

    async def execute(self, arguments: dict[str, Any], sandbox: Sandbox) -> ToolResult:
        """Execute a delegated subtask via :class:`~agent_forge.agent.subagent.SubagentRunner`."""
        started = time.monotonic()
        raw_task = arguments.get("task")
        if not isinstance(raw_task, str) or not raw_task.strip():
            return ToolResult(output="", error="Missing required argument: task", exit_code=1)

        raw_mode = str(arguments.get("mode", "research")).strip().lower()
        if raw_mode not in {"research", "worker"}:
            return ToolResult(
                output="",
                error=f"Invalid mode '{raw_mode}': expected 'research' or 'worker'.",
                exit_code=1,
            )
        mode = cast("Literal['research', 'worker']", raw_mode)

        raw_max_iter = arguments.get("max_iterations")
        max_iter = (
            raw_max_iter
            if isinstance(raw_max_iter, int) and not isinstance(raw_max_iter, bool)
            else None
        )

        result = await self._runner.delegate(
            raw_task,
            mode=mode,
            parent_sandbox=sandbox if mode == "research" else None,
            max_iterations=max_iter,
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return ToolResult(
            output=result.to_tool_output(self._runner.limits.max_result_chars),
            error=result.error,
            exit_code=0 if result.state == "completed" else 1,
            execution_time_ms=elapsed_ms,
            metadata={
                "subagent_id": result.subagent_id,
                "mode": result.mode,
                "state": result.state,
                "iterations": result.iterations,
                "total_tokens": result.tokens.total_tokens,
                "files_modified": list(result.files_modified),
                "branch": result.branch,
            },
        )
