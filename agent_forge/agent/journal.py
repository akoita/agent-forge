"""Append-only durable step journal, idempotency keys, and deterministic replay."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_forge.agent import persistence
from agent_forge.agent.models import AgentConfig, AgentRun, RunState, ToolInvocation
from agent_forge.agent.state import InvalidStateTransitionError, transition
from agent_forge.llm.base import Message, Role, TokenUsage, ToolCall
from agent_forge.tools.base import ToolResult

JOURNAL_SCHEMA_VERSION: Literal["run-journal-v1"] = "run-journal-v1"
REPLAY_SCHEMA_VERSION: Literal["run-replay-v1"] = "run-replay-v1"
JOURNAL_FILENAME = "journal.jsonl"

READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        "read_file",
        "list_directory",
        "search_codebase",
        "git_diff",
        "shell_poll",
    }
)

JournalEntryKind = Literal[
    "run_started",
    "state_transition",
    "message_recorded",
    "llm_response",
    "action_requested",
    "action_result",
    "checkpoint_saved",
    "context_compacted",
    "verification_recorded",
    "contract_evaluated",
    "run_interrupted",
    "attempt_result",
]


class JournalEntry(BaseModel):
    """Single immutable record in a run's append-only execution journal."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["run-journal-v1"] = JOURNAL_SCHEMA_VERSION
    seq: int = Field(ge=0)
    timestamp: str
    run_id: str
    iteration: int = Field(ge=0)
    kind: JournalEntryKind
    data: dict[str, Any] = Field(default_factory=dict)


class StateTransitionRecord(BaseModel):
    """State transition reconstructed from a journal."""

    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=0)
    from_state: str
    to_state: str
    reason: str = ""
    timestamp: str


class ReplayedAction(BaseModel):
    """Completed tool action reconstructed from a journal."""

    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=0)
    iteration: int = Field(ge=0)
    tool_call_id: str = ""
    idempotency_key: str = ""
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    side_effecting: bool = True
    exit_code: int = 0
    error: str | None = None
    duration_ms: int = 0
    reused: bool = False


class ReplayResult(BaseModel):
    """Deterministic reconstruction and validation summary of a run journal."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["run-replay-v1"] = REPLAY_SCHEMA_VERSION
    run_id: str
    journal_path: str
    entry_count: int = Field(ge=0)
    valid: bool
    anomalies: list[str] = Field(default_factory=list)
    had_truncated_tail: bool = False
    initial_state: str = RunState.PENDING.value
    final_state: str = RunState.PENDING.value
    state_sequence: list[str] = Field(default_factory=list)
    transitions: list[StateTransitionRecord] = Field(default_factory=list)
    iterations: int = Field(ge=0)
    llm_turns: int = Field(ge=0)
    actions: list[ReplayedAction] = Field(default_factory=list)
    incomplete_actions: list[str] = Field(default_factory=list)
    duplicate_side_effects: list[str] = Field(default_factory=list)
    reused_actions: int = Field(ge=0)
    checkpoints: int = Field(ge=0)
    compactions: int = Field(default=0, ge=0)
    context_tokens_saved: int = Field(default=0, ge=0)
    interruptions: list[dict[str, Any]] = Field(default_factory=list)
    total_tokens: dict[str, int] = Field(default_factory=dict)
    verification: dict[str, Any] | None = None
    contract_evaluation: dict[str, Any] | None = None
    attempt_result: dict[str, Any] | None = None


def is_side_effecting_tool(tool_name: str, tool: Any = None) -> bool:
    """Return whether executing *tool_name* may produce side effects."""
    explicit = getattr(tool, "side_effecting", None)
    if isinstance(explicit, bool):
        return explicit
    return tool_name not in READ_ONLY_TOOLS


def compute_tool_idempotency_key(
    run_id: str,
    iteration: int,
    tool_call_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    call_index: int = 0,
) -> str:
    """Compute a deterministic SHA-256 idempotency key for one action step."""
    canonical = json.dumps(
        [run_id, iteration, tool_call_id, tool_name, call_index, arguments],
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"idem_{digest[:24]}"


def format_tool_output(result: ToolResult) -> str:
    """Format a :class:`ToolResult` consistently for a ``Role.TOOL`` message."""
    content = result.output
    if result.error:
        content += f"\n[ERROR] {result.error}" if content else f"[ERROR] {result.error}"
    return content


def resolve_journal_path(
    target: Path | str,
    *,
    base_dir: Path | str | None = None,
) -> Path:
    """Resolve either an explicit journal file path or a persisted ``run_id``."""
    candidate = Path(target)
    if candidate.is_file():
        return candidate
    runs_dir = Path(base_dir) if base_dir is not None else persistence._default_runs_dir()
    run_journal = runs_dir / str(target) / JOURNAL_FILENAME
    if run_journal.is_file():
        return run_journal
    msg = f"Journal not found for '{target}' (checked {candidate} and {run_journal})"
    raise FileNotFoundError(msg)


def load_journal_entries(
    path: Path,
    *,
    allow_truncated_tail: bool = True,
) -> tuple[list[JournalEntry], bool]:
    """Read journal entries from *path*, optionally tolerating a crash-torn final line."""
    if not path.exists():
        return [], False

    non_empty = [raw for raw in path.read_bytes().splitlines() if raw.strip()]
    entries: list[JournalEntry] = []
    had_truncated_tail = False
    last_index = len(non_empty) - 1

    for idx, raw_line in enumerate(non_empty):
        try:
            text = raw_line.decode("utf-8", errors="strict")
            entries.append(JournalEntry.model_validate_json(text))
        except (UnicodeDecodeError, ValueError, ValidationError) as exc:
            if idx == last_index and allow_truncated_tail:
                had_truncated_tail = True
                break
            msg = f"Corrupt journal entry at line {idx + 1} in {path}: {exc}"
            raise ValueError(msg) from exc

    return entries, had_truncated_tail


def _tool_result_from_payload(payload: dict[str, Any]) -> ToolResult:
    raw = payload.get("result")
    source = raw if isinstance(raw, dict) else payload
    raw_meta = source.get("metadata", {})
    return ToolResult(
        output=str(source.get("output", "")),
        error=str(source["error"]) if source.get("error") is not None else None,
        exit_code=int(source.get("exit_code", 0)),
        execution_time_ms=int(source.get("execution_time_ms", payload.get("duration_ms", 0))),
        metadata=dict(raw_meta) if isinstance(raw_meta, dict) else {},
    )


def _message_from_payload(data: dict[str, Any]) -> Message:
    raw_calls = data.get("tool_calls")
    tool_calls = (
        [
            ToolCall(
                id=str(tc.get("id", "")),
                name=str(tc.get("name", "")),
                arguments=dict(tc.get("arguments", {})),
            )
            for tc in raw_calls
            if isinstance(tc, dict)
        ]
        if isinstance(raw_calls, list)
        else None
    )
    return Message(
        role=Role(str(data["role"])),
        content=str(data.get("content", "")),
        tool_call_id=(str(data["tool_call_id"]) if data.get("tool_call_id") is not None else None),
        tool_calls=tool_calls,
    )


class RunJournal:
    """Append-only fsync-backed JSONL step journal for one agent run or trajectory."""

    def __init__(
        self,
        run_id: str,
        *,
        base_dir: Path | str | None = None,
        path: Path | None = None,
        fsync: bool = True,
    ) -> None:
        self.run_id = run_id
        self._base_dir: Path | None = Path(base_dir) if base_dir is not None else None
        self._explicit_path = path
        self._fsync = fsync
        self._next_seq: int | None = None

    @property
    def path(self) -> Path:
        """Return the on-disk path to ``journal.jsonl``."""
        if self._explicit_path is not None:
            return self._explicit_path
        root = self._base_dir if self._base_dir is not None else persistence._default_runs_dir()
        return root / self.run_id / JOURNAL_FILENAME

    @property
    def entry_count(self) -> int:
        """Return the number of valid journal entries currently on disk."""
        if self._next_seq is not None:
            return self._next_seq
        if not self.path.exists():
            return 0
        entries, _ = load_journal_entries(self.path, allow_truncated_tail=True)
        return len(entries)

    def load(self, *, allow_truncated_tail: bool = True) -> list[JournalEntry]:
        """Load valid entries from this journal."""
        entries, _ = load_journal_entries(self.path, allow_truncated_tail=allow_truncated_tail)
        return entries

    def reset(self) -> None:
        """Clear any existing on-disk journal file before starting a brand-new run."""
        if self.path.exists():
            self.path.unlink()
        self._next_seq = 0

    def _needs_repair(self) -> bool:
        size = self.path.stat().st_size
        if size == 0:
            return False
        with self.path.open("rb") as handle:
            handle.seek(-1, os.SEEK_END)
            return handle.read(1) != b"\n"

    def _prepare_for_append(self) -> int:
        if not self.path.exists():
            self._next_seq = 0
            return 0
        if self._next_seq is not None and not self._needs_repair():
            return self._next_seq
        entries, had_truncated = load_journal_entries(self.path, allow_truncated_tail=True)
        if had_truncated:
            repaired = "\n".join(item.model_dump_json() for item in entries)
            persistence._atomic_write_text(self.path, f"{repaired}\n" if repaired else "")
        self._next_seq = (entries[-1].seq + 1) if entries else 0
        return self._next_seq

    def append(
        self,
        kind: JournalEntryKind,
        data: dict[str, Any] | None = None,
        *,
        iteration: int = 0,
    ) -> JournalEntry:
        """Append one durable record, flushing and fsyncing before returning."""
        seq = self._prepare_for_append()
        entry = JournalEntry(
            seq=seq,
            timestamp=datetime.now(UTC).isoformat(),
            run_id=self.run_id,
            iteration=iteration,
            kind=kind,
            data=dict(data) if data is not None else {},
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(entry.model_dump_json() + "\n")
            handle.flush()
            if self._fsync:
                os.fsync(handle.fileno())
        self._next_seq = seq + 1
        return entry

    def completed_action_results(self) -> tuple[dict[str, ToolResult], dict[str, ToolResult]]:
        """Return completed action results keyed by idempotency key and tool_call_id."""
        by_key: dict[str, ToolResult] = {}
        by_call_id: dict[str, ToolResult] = {}
        for entry in self.load(allow_truncated_tail=True):
            if entry.kind != "action_result":
                continue
            tool_result = _tool_result_from_payload(entry.data)
            idem_key = str(entry.data.get("idempotency_key", ""))
            call_id = str(entry.data.get("tool_call_id", ""))
            if idem_key:
                by_key[idem_key] = tool_result
            if call_id:
                by_call_id[call_id] = tool_result
        return by_key, by_call_id

    def last_recorded_state(self) -> str | None:
        """Return the latest state recorded in the journal, if any."""
        state: str | None = None
        for entry in self.load(allow_truncated_tail=True):
            if entry.kind == "run_started" and state is None:
                state = str(entry.data.get("initial_state", RunState.PENDING.value))
            elif entry.kind == "state_transition":
                state = str(entry.data.get("to_state", state))
        return state

    def run_metadata(self) -> tuple[str, str | None]:
        """Return ``(base_revision, verify_command)`` recorded in the journal."""
        base_revision = ""
        verify_command: str | None = None
        for entry in self.load(allow_truncated_tail=True):
            if entry.kind == "run_started":
                base_revision = str(entry.data.get("base_revision", base_revision))
                if entry.data.get("verify_command") is not None:
                    verify_command = str(entry.data["verify_command"])
            elif entry.kind == "checkpoint_saved":
                if entry.data.get("base_revision"):
                    base_revision = str(entry.data["base_revision"])
                if entry.data.get("verify_command") is not None:
                    verify_command = str(entry.data["verify_command"])
        return base_revision, verify_command


def _reconcile_messages(run: AgentRun, entries: list[JournalEntry]) -> None:
    recorded = [e for e in entries if e.kind == "message_recorded"]
    recorded.sort(key=lambda e: (int(e.data.get("index", 10**9)), e.seq))
    for entry in recorded:
        idx = entry.data.get("index")
        if isinstance(idx, int) and idx == len(run.messages):
            run.messages.append(_message_from_payload(entry.data))


def _reconcile_actions(
    run: AgentRun, entries: list[JournalEntry]
) -> tuple[dict[str, ToolResult], list[str], list[str]]:
    by_key: dict[str, ToolResult] = {}
    ordered_keys: list[str] = []
    ordered_call_ids: list[str] = [
        msg.tool_call_id for msg in run.messages if msg.role == Role.TOOL and msg.tool_call_id
    ]
    seen_call_ids = set(ordered_call_ids)

    for entry in entries:
        if entry.kind != "action_result":
            continue
        result = _tool_result_from_payload(entry.data)
        idem_key = str(entry.data.get("idempotency_key", ""))
        call_id = str(entry.data.get("tool_call_id", ""))
        if idem_key:
            if idem_key not in by_key:
                ordered_keys.append(idem_key)
            by_key[idem_key] = result
        if call_id and call_id not in seen_call_ids:
            seen_call_ids.add(call_id)
            ordered_call_ids.append(call_id)
            run.messages.append(
                Message(
                    role=Role.TOOL,
                    content=format_tool_output(result),
                    tool_call_id=call_id,
                )
            )
            run.tool_invocations.append(
                ToolInvocation(
                    tool_name=str(entry.data.get("tool_name", "")),
                    arguments=dict(entry.data.get("arguments", {})),
                    result=result,
                    iteration=entry.iteration,
                    timestamp=datetime.fromisoformat(entry.timestamp),
                    duration_ms=int(entry.data.get("duration_ms", result.execution_time_ms)),
                )
            )
    return by_key, ordered_keys, ordered_call_ids


def _extract_usage_counts(entry: JournalEntry) -> tuple[int, int, int]:
    usage = entry.data.get("usage", {})
    if not isinstance(usage, dict):
        return 0, 0, 0
    return (
        int(usage.get("prompt_tokens", 0)),
        int(usage.get("completion_tokens", 0)),
        int(usage.get("total_tokens", 0)),
    )


def _apply_recovered_state(
    run: AgentRun,
    latest_state: str | None,
    latest_error: str | None,
) -> None:
    if latest_state:
        try:
            target_state = RunState(latest_state)
        except ValueError:
            target_state = run.state
        if run.state in (RunState.PENDING, RunState.RUNNING):
            run.state = target_state
    if latest_error and run.state in (RunState.PAUSED, RunState.CANCELLED):
        run.error = latest_error


def _apply_recovered_artifacts(
    run: AgentRun,
    latest_verification: dict[str, Any] | None,
    latest_contract: dict[str, Any] | None,
) -> None:
    from agent_forge.agent.contracts import ContractEvaluation
    from agent_forge.agent.verification import VerificationResult

    if run.verification is None and latest_verification is not None:
        run.verification = VerificationResult(
            base_revision=str(latest_verification.get("base_revision", "")),
            patch=str(latest_verification.get("patch", "")),
            reason=str(latest_verification.get("reason", "")),
            command=str(latest_verification.get("command", "")),
            exit_code=(
                int(latest_verification["exit_code"])
                if latest_verification.get("exit_code") is not None
                else None
            ),
            stdout=str(latest_verification.get("stdout", "")),
            stderr=str(latest_verification.get("stderr", "")),
        )
    if run.contract_evaluation is None and latest_contract is not None:
        run.contract_evaluation = ContractEvaluation.model_validate(latest_contract)


def _reconcile_tokens_and_state(run: AgentRun, entries: list[JournalEntry]) -> None:
    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0
    latest_state: str | None = None
    latest_error: str | None = None
    latest_verification: dict[str, Any] | None = None
    latest_contract: dict[str, Any] | None = None

    for entry in entries:
        run.iterations = max(run.iterations, entry.iteration)
        if entry.kind == "llm_response":
            p, c, t = _extract_usage_counts(entry)
            prompt_tokens += p
            completion_tokens += c
            total_tokens += t
        elif entry.kind == "state_transition":
            latest_state = str(entry.data.get("to_state", ""))
        elif entry.kind == "run_interrupted":
            latest_error = str(entry.data.get("reason", "")) or latest_error
        elif entry.kind == "verification_recorded":
            latest_verification = dict(entry.data)
        elif entry.kind == "contract_evaluated":
            latest_contract = dict(entry.data)

    if total_tokens > run.total_tokens.total_tokens:
        run.total_tokens = TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )
    _apply_recovered_state(run, latest_state, latest_error)
    _apply_recovered_artifacts(run, latest_verification, latest_contract)


def reconcile_run_from_journal(
    run: AgentRun,
    journal_path: Path,
) -> tuple[dict[str, ToolResult], list[str], list[str]]:
    """Recover missing messages, action results, tokens, and state from *journal_path*."""
    if not journal_path.exists():
        call_ids = [
            msg.tool_call_id for msg in run.messages if msg.role == Role.TOOL and msg.tool_call_id
        ]
        return {}, [], call_ids

    entries, _ = load_journal_entries(journal_path, allow_truncated_tail=True)
    _reconcile_messages(run, entries)
    by_key, ordered_keys, ordered_call_ids = _reconcile_actions(run, entries)
    _reconcile_tokens_and_state(run, entries)
    return by_key, ordered_keys, ordered_call_ids


@dataclass
class _ReplayAccumulator:
    shadow: AgentRun
    initial_state: str
    state_sequence: list[str]
    transitions: list[StateTransitionRecord] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)
    actions: list[ReplayedAction] = field(default_factory=list)
    pending_actions: dict[str, str] = field(default_factory=dict)
    executed_side_effects: set[str] = field(default_factory=set)
    duplicate_side_effects: list[str] = field(default_factory=list)
    reused_actions: int = 0
    checkpoints: int = 0
    compactions: int = 0
    context_tokens_saved: int = 0
    llm_turns: int = 0
    iterations: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    interruptions: list[dict[str, Any]] = field(default_factory=list)
    verification: dict[str, Any] | None = None
    contract_evaluation: dict[str, Any] | None = None
    attempt_result: dict[str, Any] | None = None
    expected_seq: int = 0


def _apply_transition_entry(acc: _ReplayAccumulator, entry: JournalEntry) -> None:
    from_state = str(entry.data.get("from_state", ""))
    to_state = str(entry.data.get("to_state", ""))
    reason = str(entry.data.get("reason", ""))
    if from_state != acc.shadow.state.value:
        acc.anomalies.append(
            f"State mismatch at seq {entry.seq}: expected '{acc.shadow.state.value}', "
            f"journal recorded '{from_state}'"
        )
    try:
        target = RunState(to_state)
    except ValueError:
        acc.anomalies.append(f"Unknown state '{to_state}' at seq {entry.seq}")
        return
    try:
        transition(acc.shadow, target)
    except InvalidStateTransitionError as exc:
        acc.anomalies.append(f"Invalid transition at seq {entry.seq}: {exc}")
        acc.shadow.state = target
    acc.state_sequence.append(target.value)
    acc.transitions.append(
        StateTransitionRecord(
            seq=entry.seq,
            from_state=from_state,
            to_state=to_state,
            reason=reason,
            timestamp=entry.timestamp,
        )
    )


def _apply_action_result_entry(acc: _ReplayAccumulator, entry: JournalEntry) -> None:
    idem_key = str(entry.data.get("idempotency_key", ""))
    if idem_key:
        acc.pending_actions.pop(idem_key, None)
    side_effecting = bool(entry.data.get("side_effecting", True))
    reused = bool(entry.data.get("reused", False))
    result = _tool_result_from_payload(entry.data)

    if reused:
        acc.reused_actions += 1
    elif side_effecting and idem_key:
        if idem_key in acc.executed_side_effects:
            acc.duplicate_side_effects.append(idem_key)
            acc.anomalies.append(
                f"Duplicate side-effecting execution at seq {entry.seq}: {idem_key}"
            )
        else:
            acc.executed_side_effects.add(idem_key)

    acc.actions.append(
        ReplayedAction(
            seq=entry.seq,
            iteration=entry.iteration,
            tool_call_id=str(entry.data.get("tool_call_id", "")),
            idempotency_key=idem_key,
            tool_name=str(entry.data.get("tool_name", "")),
            arguments=dict(entry.data.get("arguments", {})),
            side_effecting=side_effecting,
            exit_code=result.exit_code,
            error=result.error,
            duration_ms=int(entry.data.get("duration_ms", result.execution_time_ms)),
            reused=reused,
        )
    )


def _apply_llm_response_entry(acc: _ReplayAccumulator, entry: JournalEntry) -> None:
    acc.llm_turns += 1
    p, c, t = _extract_usage_counts(entry)
    acc.prompt_tokens += p
    acc.completion_tokens += c
    acc.total_tokens += t


def _apply_action_requested_entry(acc: _ReplayAccumulator, entry: JournalEntry) -> None:
    idem_key = str(entry.data.get("idempotency_key", ""))
    tool_name = str(entry.data.get("tool_name", ""))
    if idem_key:
        acc.pending_actions[idem_key] = tool_name


def _apply_checkpoint_entry(acc: _ReplayAccumulator, entry: JournalEntry) -> None:
    del entry
    acc.checkpoints += 1


def _apply_compaction_entry(acc: _ReplayAccumulator, entry: JournalEntry) -> None:
    acc.compactions += 1
    acc.context_tokens_saved += int(entry.data.get("tokens_saved", 0))


def _apply_interrupted_entry(acc: _ReplayAccumulator, entry: JournalEntry) -> None:
    acc.interruptions.append(dict(entry.data))


def _apply_verification_entry(acc: _ReplayAccumulator, entry: JournalEntry) -> None:
    acc.verification = dict(entry.data)


def _apply_contract_entry(acc: _ReplayAccumulator, entry: JournalEntry) -> None:
    acc.contract_evaluation = dict(entry.data)


def _apply_attempt_entry(acc: _ReplayAccumulator, entry: JournalEntry) -> None:
    acc.attempt_result = dict(entry.data)


_REPLAY_HANDLERS: dict[str, Any] = {
    "state_transition": _apply_transition_entry,
    "llm_response": _apply_llm_response_entry,
    "action_requested": _apply_action_requested_entry,
    "action_result": _apply_action_result_entry,
    "checkpoint_saved": _apply_checkpoint_entry,
    "context_compacted": _apply_compaction_entry,
    "run_interrupted": _apply_interrupted_entry,
    "verification_recorded": _apply_verification_entry,
    "contract_evaluated": _apply_contract_entry,
    "attempt_result": _apply_attempt_entry,
}


def _apply_replay_entry(acc: _ReplayAccumulator, entry: JournalEntry) -> None:
    if entry.seq != acc.expected_seq:
        acc.anomalies.append(
            f"Sequence gap or disorder: expected seq {acc.expected_seq}, got {entry.seq}"
        )
    acc.expected_seq = entry.seq + 1
    acc.iterations = max(acc.iterations, entry.iteration)

    handler = _REPLAY_HANDLERS.get(entry.kind)
    if handler is not None:
        handler(acc, entry)


def replay_journal(
    target: Path | str,
    *,
    base_dir: Path | str | None = None,
) -> ReplayResult:
    """Deterministically reconstruct and validate run execution from its journal."""
    journal_path = resolve_journal_path(target, base_dir=base_dir)
    entries, had_truncated_tail = load_journal_entries(journal_path, allow_truncated_tail=True)
    if not entries:
        return ReplayResult(
            run_id=journal_path.parent.name,
            journal_path=str(journal_path),
            entry_count=0,
            valid=False,
            anomalies=[f"Journal is empty: {journal_path}"],
            had_truncated_tail=had_truncated_tail,
            iterations=0,
            llm_turns=0,
            reused_actions=0,
            checkpoints=0,
            total_tokens={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        )

    first = entries[0]
    initial_state = (
        str(first.data.get("initial_state", RunState.PENDING.value))
        if first.kind == "run_started"
        else RunState.PENDING.value
    )
    try:
        start_state = RunState(initial_state)
    except ValueError:
        start_state = RunState.PENDING

    shadow = AgentRun(
        id=first.run_id,
        task="",
        repo_path="",
        config=AgentConfig(),
        state=start_state,
    )
    acc = _ReplayAccumulator(
        shadow=shadow,
        initial_state=start_state.value,
        state_sequence=[start_state.value],
    )
    for entry in entries:
        _apply_replay_entry(acc, entry)

    valid = len(acc.anomalies) == 0 and len(acc.duplicate_side_effects) == 0
    return ReplayResult(
        run_id=first.run_id,
        journal_path=str(journal_path),
        entry_count=len(entries),
        valid=valid,
        anomalies=acc.anomalies,
        had_truncated_tail=had_truncated_tail,
        initial_state=acc.initial_state,
        final_state=acc.shadow.state.value,
        state_sequence=acc.state_sequence,
        transitions=acc.transitions,
        iterations=acc.iterations,
        llm_turns=acc.llm_turns,
        actions=acc.actions,
        incomplete_actions=list(acc.pending_actions.keys()),
        duplicate_side_effects=acc.duplicate_side_effects,
        reused_actions=acc.reused_actions,
        checkpoints=acc.checkpoints,
        compactions=acc.compactions,
        context_tokens_saved=acc.context_tokens_saved,
        interruptions=acc.interruptions,
        total_tokens={
            "prompt_tokens": acc.prompt_tokens,
            "completion_tokens": acc.completion_tokens,
            "total_tokens": acc.total_tokens,
        },
        verification=acc.verification,
        contract_evaluation=acc.contract_evaluation,
        attempt_result=acc.attempt_result,
    )
