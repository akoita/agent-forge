"""Run persistence — save and load AgentRun state to disk.

Persists runs under ~/.agent-forge/runs/<run_id>/:
  - run.json        — metadata (state, config, timestamps, tokens, error)
  - messages.jsonl  — conversation history (one JSON line per message)
  - events.jsonl    — tool invocations (one JSON line per invocation)
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_forge.agent.models import AgentConfig, AgentRun, RunState, ToolInvocation
from agent_forge.config import USER_CONFIG_DIR
from agent_forge.llm.base import Message, Role, TokenUsage, ToolCall


def serialize_token_usage(usage: TokenUsage) -> dict[str, int]:
    """Serialize a :class:`TokenUsage` record to plain JSON integers."""
    return {
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
        "cached_prompt_tokens": usage.cached_prompt_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
    }


def deserialize_token_usage(data: dict[str, Any] | None) -> TokenUsage:
    """Restore a :class:`TokenUsage` record from a persisted dict."""
    if not data:
        return TokenUsage(0, 0, 0)
    return TokenUsage(
        prompt_tokens=int(data.get("prompt_tokens", 0)),
        completion_tokens=int(data.get("completion_tokens", 0)),
        total_tokens=int(data.get("total_tokens", 0)),
        cached_prompt_tokens=int(data.get("cached_prompt_tokens", 0)),
        reasoning_tokens=int(data.get("reasoning_tokens", 0)),
    )


def serialize_message(msg: Message) -> dict[str, Any]:
    """Serialize a chat :class:`Message` into a JSON-compatible dictionary."""
    line: dict[str, Any] = {
        "role": msg.role.value,
        "content": msg.content,
    }
    if msg.tool_call_id:
        line["tool_call_id"] = msg.tool_call_id
    if msg.tool_calls:
        line["tool_calls"] = [asdict(tc) for tc in msg.tool_calls]
    if msg.response_id:
        line["response_id"] = msg.response_id
    if msg.reasoning_blocks:
        line["reasoning_blocks"] = msg.reasoning_blocks
    return line


def deserialize_message(data: dict[str, Any]) -> Message:
    """Restore a :class:`Message` from a JSON dictionary."""
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
    return Message(
        role=Role(data["role"]),
        content=data["content"],
        tool_call_id=data.get("tool_call_id"),
        tool_calls=tool_calls,
        reasoning_blocks=data.get("reasoning_blocks"),
        response_id=data.get("response_id"),
    )


def serialize_tool_invocation(inv: ToolInvocation) -> dict[str, Any]:
    """Serialize a :class:`ToolInvocation` record into JSON-compatible data."""
    result_data: dict[str, Any] = {
        "output": inv.result.output,
        "error": inv.result.error,
        "exit_code": inv.result.exit_code,
        "execution_time_ms": inv.result.execution_time_ms,
    }
    if inv.result.metadata:
        result_data["metadata"] = inv.result.metadata
    return {
        "tool_name": inv.tool_name,
        "arguments": inv.arguments,
        "result": result_data,
        "iteration": inv.iteration,
        "timestamp": inv.timestamp.isoformat(),
        "duration_ms": inv.duration_ms,
    }


def deserialize_tool_invocation(data: dict[str, Any]) -> ToolInvocation:
    """Restore a :class:`ToolInvocation` from a JSON dictionary."""
    from agent_forge.tools.base import ToolResult

    return ToolInvocation(
        tool_name=data["tool_name"],
        arguments=data["arguments"],
        result=ToolResult(
            output=data["result"]["output"],
            error=data["result"].get("error"),
            exit_code=data["result"]["exit_code"],
            execution_time_ms=data["result"].get("execution_time_ms", 0),
            metadata=data["result"].get("metadata", {}),
        ),
        iteration=data["iteration"],
        timestamp=datetime.fromisoformat(data["timestamp"]),
        duration_ms=data["duration_ms"],
    )


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write *data* to a temp file, flush/fsync, and atomically replace *path*."""
    tmp_path = path.with_name(f".{path.name}.tmp")
    with tmp_path.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)


def _atomic_write_text(path: Path, text: str) -> None:
    """Atomically write UTF-8 *text* to *path* with fsync."""
    _atomic_write_bytes(path, text.encode("utf-8"))


def _default_runs_dir() -> Path:
    """Return the default runs directory (~/.agent-forge/runs)."""
    return USER_CONFIG_DIR / "runs"


def save_run(run: AgentRun, *, base_dir: Path | str | None = None) -> Path:
    """Persist an AgentRun to disk.

    Returns the run directory path.
    """
    runs_dir = Path(base_dir) if base_dir is not None else _default_runs_dir()
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
        "verification": asdict(run.verification) if run.verification else None,
        "contract_evaluation": (
            run.contract_evaluation.model_dump(mode="json")
            if run.contract_evaluation is not None
            else None
        ),
        "context_stats": run.context_stats or None,
    }
    _atomic_write_text(run_dir / "run.json", json.dumps(run_meta, indent=2) + "\n")
    if run.verification and run.verification.patch:
        _atomic_write_bytes(
            run_dir / "change.patch",
            run.verification.patch.encode(errors="surrogateescape"),
        )

    # ── messages.jsonl ────────────────────────────────────────────
    message_lines = [json.dumps(serialize_message(msg)) + "\n" for msg in run.messages]
    _atomic_write_text(run_dir / "messages.jsonl", "".join(message_lines))

    # ── events.jsonl ──────────────────────────────────────────────
    event_lines = [
        json.dumps(serialize_tool_invocation(inv)) + "\n" for inv in run.tool_invocations
    ]
    _atomic_write_text(run_dir / "events.jsonl", "".join(event_lines))

    return run_dir


def load_run(run_id: str, *, base_dir: Path | str | None = None) -> AgentRun:
    """Load an AgentRun from disk.

    Raises FileNotFoundError if the run directory doesn't exist.
    """
    from agent_forge.agent.contracts import ContractEvaluation
    from agent_forge.agent.verification import VerificationResult

    runs_dir = Path(base_dir) if base_dir is not None else _default_runs_dir()
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
            messages.append(deserialize_message(json.loads(line)))

    # ── events.jsonl ──────────────────────────────────────────────
    invocations: list[ToolInvocation] = []
    events_path = run_dir / "events.jsonl"
    if events_path.exists():
        for line in events_path.read_text().strip().splitlines():
            invocations.append(deserialize_tool_invocation(json.loads(line)))

    tokens = meta["total_tokens"]
    verification = meta.get("verification")
    contract_eval = meta.get("contract_evaluation")
    completed_at = datetime.fromisoformat(meta["completed_at"]) if meta["completed_at"] else None

    return AgentRun(
        task=meta["task"],
        repo_path=meta["repo_path"],
        config=AgentConfig(**meta["config"]),
        id=meta["id"],
        state=RunState(meta["state"]),
        messages=messages,
        iterations=meta["iterations"],
        total_tokens=deserialize_token_usage(tokens),
        tool_invocations=invocations,
        created_at=datetime.fromisoformat(meta["created_at"]),
        completed_at=completed_at,
        error=meta.get("error"),
        verification=VerificationResult(**verification) if verification is not None else None,
        contract_evaluation=(
            ContractEvaluation.model_validate(contract_eval) if contract_eval is not None else None
        ),
        context_stats=dict(meta.get("context_stats") or {}),
    )
