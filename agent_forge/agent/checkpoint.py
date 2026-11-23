"""Step-level local checkpoint persistence for resumable coding runs."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from agent_forge.agent.journal import (
    JOURNAL_FILENAME,
    RunJournal,
    reconcile_run_from_journal,
)
from agent_forge.agent.persistence import (
    _atomic_write_bytes,
    _atomic_write_text,
    _default_runs_dir,
    load_run,
    save_run,
)
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
    completed_idempotency_keys: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


def save_checkpoint(
    run: AgentRun,
    *,
    base_revision: str,
    partial_patch: str = "",
    verify_command: str | None = None,
    completed_idempotency_keys: list[str] | None = None,
    base_dir: Path | str | None = None,
) -> Path:
    """Persist *run* alongside an atomic checkpoint manifest and partial patch."""
    run_dir = save_run(run, base_dir=base_dir)
    completed_ids = [
        msg.tool_call_id for msg in run.messages if msg.role == Role.TOOL and msg.tool_call_id
    ]
    idem_keys = completed_idempotency_keys
    if idem_keys is None:
        journal_path = run_dir / JOURNAL_FILENAME
        if journal_path.exists():
            by_key, _ = RunJournal(run.id, path=journal_path).completed_action_results()
            idem_keys = list(by_key.keys())
        else:
            idem_keys = []

    checkpoint = RunCheckpoint(
        run_id=run.id,
        base_revision=base_revision,
        iteration=run.iterations,
        verify_command=verify_command,
        partial_patch=partial_patch,
        completed_tool_call_ids=completed_ids,
        completed_idempotency_keys=list(dict.fromkeys(idem_keys)),
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
    """Load a persisted ``AgentRun`` and reconcile its checkpoint and step journal."""
    run = load_run(run_id, base_dir=base_dir)
    runs_dir = Path(base_dir) if base_dir is not None else _default_runs_dir()
    run_dir = runs_dir / run_id
    journal_path = run_dir / JOURNAL_FILENAME
    journal_keys: list[str] = []
    journal_call_ids: list[str] = []
    if journal_path.exists():
        _, journal_keys, journal_call_ids = reconcile_run_from_journal(run, journal_path)

    checkpoint_path = run_dir / "checkpoint.json"
    if checkpoint_path.exists():
        meta = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        patch_path = run_dir / "checkpoint.patch"
        partial_patch = (
            patch_path.read_bytes().decode(errors="surrogateescape") if patch_path.exists() else ""
        )
        merged_call_ids = list(
            dict.fromkeys([*meta.get("completed_tool_call_ids", []), *journal_call_ids])
        )
        merged_keys = list(
            dict.fromkeys([*meta.get("completed_idempotency_keys", []), *journal_keys])
        )
        checkpoint = RunCheckpoint(
            run_id=str(meta["run_id"]),
            base_revision=str(meta["base_revision"]),
            iteration=int(meta["iteration"]),
            verify_command=meta.get("verify_command"),
            partial_patch=partial_patch,
            completed_tool_call_ids=merged_call_ids,
            completed_idempotency_keys=merged_keys,
            created_at=str(meta.get("created_at", "")),
        )
        return run, checkpoint

    if journal_path.exists():
        base_revision, verify_command = RunJournal(run.id, path=journal_path).run_metadata()
        checkpoint = RunCheckpoint(
            run_id=run.id,
            base_revision=base_revision,
            iteration=run.iterations,
            verify_command=verify_command,
            partial_patch="",
            completed_tool_call_ids=journal_call_ids,
            completed_idempotency_keys=journal_keys,
        )
        return run, checkpoint

    msg = f"Checkpoint not found for run: {run_id}"
    raise FileNotFoundError(msg)
