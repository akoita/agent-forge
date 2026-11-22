"""Step-level local checkpoint persistence for resumable coding runs."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from agent_forge.agent.persistence import _default_runs_dir, load_run, save_run
from agent_forge.llm.base import Role

if TYPE_CHECKING:
    from agent_forge.agent.models import AgentRun


@dataclass(frozen=True)
class RunCheckpoint:
    """Durable checkpoint capturing revision, partial patch, and completed tool calls."""

    run_id: str
    base_revision: str
    iteration: int
    verify_command: str | None = None
    partial_patch: str = ""
    completed_tool_call_ids: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_bytes(data)
    os.replace(tmp_path, path)


def _atomic_write_text(path: Path, text: str) -> None:
    _atomic_write_bytes(path, text.encode("utf-8"))


def save_checkpoint(
    run: AgentRun,
    *,
    base_revision: str,
    partial_patch: str = "",
    verify_command: str | None = None,
    base_dir: Path | str | None = None,
) -> Path:
    """Persist *run* alongside an atomic checkpoint manifest and partial patch."""
    run_dir = save_run(run, base_dir=base_dir)
    completed_ids = [
        msg.tool_call_id for msg in run.messages if msg.role == Role.TOOL and msg.tool_call_id
    ]
    checkpoint = RunCheckpoint(
        run_id=run.id,
        base_revision=base_revision,
        iteration=run.iterations,
        verify_command=verify_command,
        partial_patch=partial_patch,
        completed_tool_call_ids=completed_ids,
    )
    meta = asdict(checkpoint)
    meta.pop("partial_patch", None)
    _atomic_write_text(run_dir / "checkpoint.json", json.dumps(meta, indent=2) + "\n")
    _atomic_write_bytes(
        run_dir / "checkpoint.patch",
        partial_patch.encode(errors="surrogateescape"),
    )
    return run_dir


def load_checkpoint(
    run_id: str,
    *,
    base_dir: Path | str | None = None,
) -> tuple[AgentRun, RunCheckpoint]:
    """Load a persisted ``AgentRun`` and its ``RunCheckpoint`` from disk."""
    run = load_run(run_id, base_dir=base_dir)
    runs_dir = Path(base_dir) if base_dir is not None else _default_runs_dir()
    run_dir = runs_dir / run_id
    checkpoint_path = run_dir / "checkpoint.json"
    if not checkpoint_path.exists():
        msg = f"Checkpoint not found for run: {run_id}"
        raise FileNotFoundError(msg)

    meta = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    patch_path = run_dir / "checkpoint.patch"
    partial_patch = (
        patch_path.read_bytes().decode(errors="surrogateescape") if patch_path.exists() else ""
    )
    checkpoint = RunCheckpoint(
        run_id=str(meta["run_id"]),
        base_revision=str(meta["base_revision"]),
        iteration=int(meta["iteration"]),
        verify_command=meta.get("verify_command"),
        partial_patch=partial_patch,
        completed_tool_call_ids=list(meta.get("completed_tool_call_ids", [])),
        created_at=str(meta.get("created_at", "")),
    )
    return run, checkpoint
