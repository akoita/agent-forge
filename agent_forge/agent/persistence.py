"""Run persistence — save and load AgentRun state to disk.

Persists runs under ~/.agent-forge/runs/<run_id>/:
  - run.json        — metadata (state, config, timestamps, tokens, error)
  - messages.jsonl  — conversation history (one JSON line per message)
  - events.jsonl    — tool invocations (one JSON line per invocation)
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

from agent_forge.agent.models import AgentConfig, AgentRun, RunState, ToolInvocation
from agent_forge.config import USER_CONFIG_DIR
from agent_forge.llm.base import Message, Role, TokenUsage, ToolCall


def _default_runs_dir() -> Path:
    """Return the default runs directory (~/.agent-forge/runs)."""
    return USER_CONFIG_DIR / "runs"


def save_run(run: AgentRun, *, base_dir: Path | None = None) -> Path:
    """Persist an AgentRun to disk.

    Returns the run directory path.
    """
    runs_dir = base_dir or _default_runs_dir()
    run_dir = runs_dir / run.id
    run_dir.mkdir(parents=True, exist_ok=True)

    # ── run.json ──────────────────────────────────────────────────
    run_meta = {
        "id": run.id,
        "task": run.task,
        "repo_path": run.repo_path,
        "state": run.state.value,
        "iterations": run.iterations,
        "total_tokens": {
            "prompt_tokens": run.total_tokens.prompt_tokens,
            "completion_tokens": run.total_tokens.completion_tokens,
            "total_tokens": run.total_tokens.total_tokens,
        },
        "config": asdict(run.config),
        "created_at": run.created_at.isoformat(),
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "error": run.error,
    }
    (run_dir / "run.json").write_text(json.dumps(run_meta, indent=2) + "\n")

    # ── messages.jsonl ────────────────────────────────────────────
    with (run_dir / "messages.jsonl").open("w") as f:
        for msg in run.messages:
            line: dict[str, Any] = {
                "role": msg.role.value,
                "content": msg.content,
            }
            if msg.tool_call_id:
                line["tool_call_id"] = msg.tool_call_id
            if msg.tool_calls:
                line["tool_calls"] = [asdict(tc) for tc in msg.tool_calls]
            f.write(json.dumps(line) + "\n")

    # ── events.jsonl ──────────────────────────────────────────────
    with (run_dir / "events.jsonl").open("w") as f:
        for inv in run.tool_invocations:
            line_ev: dict[str, Any] = {
                "tool_name": inv.tool_name,
                "arguments": inv.arguments,
                "result": {
                    "output": inv.result.output,
                    "error": inv.result.error,
                    "exit_code": inv.result.exit_code,
                    "execution_time_ms": inv.result.execution_time_ms,
                },
                "iteration": inv.iteration,
                "timestamp": inv.timestamp.isoformat(),
                "duration_ms": inv.duration_ms,
            }
            f.write(json.dumps(line_ev) + "\n")

    return run_dir


def load_run(run_id: str, *, base_dir: Path | None = None) -> AgentRun:
    """Load an AgentRun from disk.

    Raises FileNotFoundError if the run directory doesn't exist.
    """
    from datetime import datetime

    from agent_forge.tools.base import ToolResult

    runs_dir = base_dir or _default_runs_dir()
    run_dir = runs_dir / run_id

    if not run_dir.exists():
        msg = f"Run not found: {run_id}"
        raise FileNotFoundError(msg)

    # ── run.json ──────────────────────────────────────────────────
    meta = json.loads((run_dir / "run.json").read_text())

    # ── messages.jsonl ────────────────────────────────────────────
    messages: list[Message] = []
    messages_path = run_dir / "messages.jsonl"
    if messages_path.exists():
        for line in messages_path.read_text().strip().splitlines():
            data = json.loads(line)
            tool_calls = None
            if "tool_calls" in data:
                tool_calls = [
                    ToolCall(
                        id=tc["id"],
                        name=tc["name"],
                        arguments=tc["arguments"],
                    )
                    for tc in data["tool_calls"]
                ]
            messages.append(
                Message(
                    role=Role(data["role"]),
                    content=data["content"],
                    tool_call_id=data.get("tool_call_id"),
                    tool_calls=tool_calls,
                )
            )

    # ── events.jsonl ──────────────────────────────────────────────
    invocations: list[ToolInvocation] = []
    events_path = run_dir / "events.jsonl"
    if events_path.exists():
        for line in events_path.read_text().strip().splitlines():
            data = json.loads(line)
            invocations.append(
                ToolInvocation(
                    tool_name=data["tool_name"],
                    arguments=data["arguments"],
                    result=ToolResult(
                        output=data["result"]["output"],
                        error=data["result"].get("error"),
                        exit_code=data["result"]["exit_code"],
                        execution_time_ms=data["result"].get("execution_time_ms", 0),
                    ),
                    iteration=data["iteration"],
                    timestamp=datetime.fromisoformat(data["timestamp"]),
                    duration_ms=data["duration_ms"],
                )
            )

    tokens = meta["total_tokens"]
    completed_at = (
        datetime.fromisoformat(meta["completed_at"]) if meta["completed_at"] else None
    )

    return AgentRun(
        task=meta["task"],
        repo_path=meta["repo_path"],
        config=AgentConfig(**meta["config"]),
        id=meta["id"],
        state=RunState(meta["state"]),
        messages=messages,
        iterations=meta["iterations"],
        total_tokens=TokenUsage(
            prompt_tokens=tokens["prompt_tokens"],
            completion_tokens=tokens["completion_tokens"],
            total_tokens=tokens["total_tokens"],
        ),
        tool_invocations=invocations,
        created_at=datetime.fromisoformat(meta["created_at"]),
        completed_at=completed_at,
        error=meta.get("error"),
    )
