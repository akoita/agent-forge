"""Domain-agnostic Super-Agent Ticket ledger, intake router, admission, and attention budget."""

from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import posixpath
import re
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent_forge.agent.memory import MemoryEntry, RepoMemoryStore
from agent_forge.orchestration.events import Event, EventBus, EventType

_IGNORED_REPO_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
    }
)
_GLOB_WILDCARD_CHARS = frozenset({"*", "?", "["})
_PATH_TOKEN_RE = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.*-]+)+")
_DEP_REF_RE = re.compile(r"(?:after|depends\s+on|blocked\s+by)\s+#?(\d+)", re.IGNORECASE)
_NUMBERED_LINE_RE = re.compile(r"^(?:\d+[.)]\s+|[-*]\s+)(.+)$")
_DESTRUCTIVE_KEYWORDS: tuple[str, ...] = (
    "rm -rf",
    "git push",
    "git reset --hard",
    "force push",
    "drop table",
    "drop database",
    "truncate table",
    "terraform destroy",
    "kubectl delete",
    "deploy to prod",
    "production deploy",
)


class TicketState(StrEnum):
    """Lifecycle state for a bounded Super-Agent ticket."""

    INTAKE = "intake"
    TRIAGED = "triaged"
    ADMITTED = "admitted"
    BLOCKED_ON_HUMAN = "blocked_on_human"
    DISPATCHED = "dispatched"
    VERIFYING = "verifying"
    READY_FOR_REVIEW = "ready_for_review"
    CONFLICT = "conflict"
    INTEGRATED = "integrated"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TicketKind(StrEnum):
    """Generic work category owned by a ticket."""

    RESEARCH = "research"
    IMPLEMENT = "implement"
    TEST = "test"
    REVIEW = "review"
    FIX = "fix"
    REMEDIATE = "remediate"


class RiskLevel(StrEnum):
    """Estimated risk level driving admission and gating."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


_READ_ONLY_KINDS = frozenset({TicketKind.RESEARCH, TicketKind.REVIEW})


VALID_TICKET_TRANSITIONS: dict[TicketState, frozenset[TicketState]] = {
    TicketState.INTAKE: frozenset({TicketState.TRIAGED, TicketState.CANCELLED}),
    TicketState.TRIAGED: frozenset(
        {TicketState.ADMITTED, TicketState.BLOCKED_ON_HUMAN, TicketState.CANCELLED}
    ),
    TicketState.BLOCKED_ON_HUMAN: frozenset({TicketState.ADMITTED, TicketState.CANCELLED}),
    TicketState.ADMITTED: frozenset(
        {TicketState.DISPATCHED, TicketState.BLOCKED_ON_HUMAN, TicketState.CANCELLED}
    ),
    TicketState.DISPATCHED: frozenset(
        {TicketState.VERIFYING, TicketState.FAILED, TicketState.CANCELLED}
    ),
    TicketState.VERIFYING: frozenset(
        {
            TicketState.READY_FOR_REVIEW,
            TicketState.DISPATCHED,
            TicketState.BLOCKED_ON_HUMAN,
            TicketState.FAILED,
            TicketState.CANCELLED,
        }
    ),
    TicketState.READY_FOR_REVIEW: frozenset(
        {
            TicketState.INTEGRATED,
            TicketState.CONFLICT,
            TicketState.BLOCKED_ON_HUMAN,
            TicketState.CANCELLED,
        }
    ),
    TicketState.CONFLICT: frozenset(
        {
            TicketState.INTEGRATED,
            TicketState.BLOCKED_ON_HUMAN,
            TicketState.FAILED,
            TicketState.CANCELLED,
        }
    ),
    TicketState.INTEGRATED: frozenset(),
    TicketState.FAILED: frozenset(),
    TicketState.CANCELLED: frozenset(),
}

_TERMINAL_TICKET_STATES = frozenset(
    {TicketState.INTEGRATED, TicketState.FAILED, TicketState.CANCELLED}
)


class InvalidTicketTransitionError(ValueError):
    """Raised when attempting an illegal ticket state machine transition."""


class TicketBudget(BaseModel):
    """Per-ticket resource bounds enforced across workers."""

    model_config = ConfigDict(extra="forbid")

    max_iterations: int = Field(default=12, ge=1)
    max_tokens: int = Field(default=80_000, ge=1)
    max_cost_usd: float | None = Field(default=None, ge=0.0)
    max_duration_seconds: float | None = Field(default=None, gt=0.0)


class ProjectInput(BaseModel):
    """Normalized incoming project request entering the Super-Agent router."""

    model_config = ConfigDict(extra="forbid")

    input_id: str = Field(default_factory=lambda: f"inp-{uuid.uuid4().hex[:12]}")
    source: Literal["cli", "acp", "studio", "webhook", "api"] = "cli"
    raw_text: str = Field(min_length=1)
    repo_path: str = Field(min_length=1)
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    metadata: dict[str, Any] = Field(default_factory=dict)


class Ticket(BaseModel):
    """Bounded unit of work tracked inside the durable Super-Agent ledger."""

    model_config = ConfigDict(extra="forbid")

    ticket_id: str = Field(default_factory=lambda: f"tkt-{uuid.uuid4().hex[:10]}")
    parent_input_id: str
    title: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    kind: TicketKind = TicketKind.IMPLEMENT
    state: TicketState = TicketState.INTAKE
    depends_on: list[str] = Field(default_factory=list)
    scope_globs: list[str] = Field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.LOW
    requires_external_action: bool = False
    budget: TicketBudget = Field(default_factory=TicketBudget)
    branch: str = ""
    verification_summary: str = ""
    blocked_reason: str = ""
    attempts: int = 0
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    metadata: dict[str, Any] = Field(default_factory=dict)


class TicketDraft(BaseModel):
    """Intermediate planner output before assignment of ledger IDs and overlap edges."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    kind: TicketKind = TicketKind.IMPLEMENT
    depends_on_indices: list[int] = Field(default_factory=list)
    scope_globs: list[str] = Field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.LOW
    requires_external_action: bool = False
    budget: TicketBudget = Field(default_factory=TicketBudget)
    metadata: dict[str, Any] = Field(default_factory=dict)


class InboxItem(BaseModel):
    """Coalesced human-attention item awaiting approval, clarification, or review."""

    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(default_factory=lambda: f"ibx-{uuid.uuid4().hex[:10]}")
    kind: Literal["approval", "clarification", "review_bundle", "conflict", "budget"]
    ticket_ids: list[str] = Field(default_factory=list)
    summary: str = Field(min_length=1)
    detail: str = ""
    options: list[str] = Field(default_factory=lambda: ["approve", "reject"])
    resolved: bool = False
    resolution: str = ""
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    resolved_at: str = ""


@dataclass(frozen=True)
class ProjectContextSnapshot:
    """Repository files and persistent learnings available to the repartition router."""

    repo_root: Path
    files: tuple[str, ...]
    memory_entries: tuple[MemoryEntry, ...]


@dataclass(frozen=True)
class AdmissionDecision:
    """Result of evaluating a single ticket against the admission policy."""

    admitted: bool
    reason: str
    inbox_kind: Literal["approval", "clarification", "budget"] | None = None
    category: str = ""


@dataclass(frozen=True)
class RouteResult:
    """Outcome of normalizing, repartitioning, and admitting one project input."""

    project_input: ProjectInput
    tickets: tuple[Ticket, ...]
    admitted_ids: tuple[str, ...]
    blocked_ids: tuple[str, ...]
    inbox_items: tuple[InboxItem, ...]


TicketPlannerFn = Callable[
    [ProjectInput, ProjectContextSnapshot, int],
    Awaitable[Sequence[TicketDraft]],
]


def _normalize_glob(pattern: str) -> str:
    cleaned = pattern.strip().replace("\\", "/")
    if not cleaned:
        return ""
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return posixpath.normpath(cleaned) if not _has_wildcards(cleaned) else cleaned.rstrip("/")


def _has_wildcards(text: str) -> bool:
    return any(ch in _GLOB_WILDCARD_CHARS for ch in text)


def is_scope_glob_safe(glob_pattern: str) -> bool:
    """Validate that a ticket scope glob stays inside the repository workspace."""
    if "\x00" in glob_pattern or "\\" in glob_pattern:
        return False
    cleaned = glob_pattern.strip()
    if not cleaned or cleaned.startswith("/"):
        return False
    raw_parts = [p for p in cleaned.split("/") if p]
    if any(p == ".." for p in raw_parts):
        return False
    if raw_parts and raw_parts[0] == ".git":
        return False
    normalized = posixpath.normpath(cleaned)
    norm_parts = [p for p in normalized.split("/") if p and p != "."]
    if any(p == ".." for p in norm_parts):
        return False
    return not (norm_parts and norm_parts[0] == ".git")


def _static_prefix_segments(pattern: str) -> tuple[str, ...]:
    parts: list[str] = []
    for segment in pattern.split("/"):
        if not segment or segment == ".":
            continue
        if _has_wildcards(segment):
            break
        parts.append(segment)
    return tuple(parts)


def _is_segment_prefix(shorter: tuple[str, ...], longer: tuple[str, ...]) -> bool:
    return len(shorter) <= len(longer) and longer[: len(shorter)] == shorter


def _pair_overlaps(glob_a: str, glob_b: str) -> bool:
    na = _normalize_glob(glob_a)
    nb = _normalize_glob(glob_b)
    if not na or not nb:
        return False
    if na in ("*", "**") or nb in ("*", "**") or na == nb:
        return True
    if fnmatch.fnmatchcase(na, nb) or fnmatch.fnmatchcase(nb, na):
        return True
    wild_a = _has_wildcards(na)
    wild_b = _has_wildcards(nb)
    pref_a = _static_prefix_segments(na)
    pref_b = _static_prefix_segments(nb)
    if not pref_a or not pref_b:
        return wild_a or wild_b
    if pref_a == pref_b:
        return wild_a or wild_b or na == nb
    if wild_a and _is_segment_prefix(pref_a, pref_b):
        return True
    return wild_b and _is_segment_prefix(pref_b, pref_a)


def scopes_overlap(globs_a: Sequence[str], globs_b: Sequence[str]) -> bool:
    """Return True if two ticket scope declarations can touch overlapping workspace files."""
    if not globs_a or not globs_b:
        return True
    return any(_pair_overlaps(ga, gb) for ga in globs_a for gb in globs_b)


def _reachable(edges: dict[str, set[str]], start: str, target: str) -> bool:
    visited: set[str] = set()
    queue = deque([start])
    while queue:
        node = queue.popleft()
        if node == target:
            return True
        if node in visited:
            continue
        visited.add(node)
        queue.extend(edges.get(node, ()))
    return False


def serialize_overlapping_scopes(tickets: list[Ticket]) -> list[Ticket]:
    """Inject dependency edges when mutating tickets share overlapping ``scope_globs``."""
    deps_map: dict[str, set[str]] = {t.ticket_id: set(t.depends_on) for t in tickets}
    for idx_b in range(len(tickets)):
        later = tickets[idx_b]
        if later.kind in _READ_ONLY_KINDS:
            continue
        for idx_a in range(idx_b):
            earlier = tickets[idx_a]
            if earlier.kind in _READ_ONLY_KINDS:
                continue
            if earlier.ticket_id in deps_map[later.ticket_id]:
                continue
            if not scopes_overlap(earlier.scope_globs, later.scope_globs):
                continue
            if _reachable(deps_map, earlier.ticket_id, later.ticket_id):
                continue
            later.depends_on.append(earlier.ticket_id)
            deps_map[later.ticket_id].add(earlier.ticket_id)
    return tickets


def _walk_repo_files(root: Path, *, max_files: int) -> list[str]:
    collected: list[str] = []
    queue: deque[Path] = deque([root])
    while queue and len(collected) < max_files:
        current = queue.popleft()
        try:
            children = sorted(current.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError:
            continue
        for child in children:
            if len(collected) >= max_files:
                break
            if child.name in _IGNORED_REPO_DIRS:
                continue
            resolved = child.resolve()
            if not resolved.is_relative_to(root):
                continue
            if resolved.is_dir():
                queue.append(resolved)
            elif resolved.is_file():
                collected.append(resolved.relative_to(root).as_posix())
    return collected


def capture_project_context(
    repo_path: str | Path,
    *,
    max_files: int = 300,
) -> ProjectContextSnapshot:
    """Snapshot workspace file paths and ``.agent-forge/memory.md`` learnings."""
    root = Path(repo_path).resolve()
    files = tuple(_walk_repo_files(root, max_files=max_files)) if root.is_dir() else ()
    memories = tuple(RepoMemoryStore(root).load_entries()) if root.exists() else ()
    return ProjectContextSnapshot(repo_root=root, files=files, memory_entries=memories)


def _split_input_segments(raw_text: str) -> list[str]:
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    structured: list[str] = []
    for line in lines:
        match = _NUMBERED_LINE_RE.match(line)
        if match:
            structured.append(match.group(1).strip())
    if structured:
        return structured
    paragraphs = [block.strip() for block in re.split(r"\n\s*\n", raw_text) if block.strip()]
    if len(paragraphs) > 1:
        return [" ".join(block.split()) for block in paragraphs]
    cleaned = " ".join(raw_text.split())
    return [cleaned] if cleaned else []


def _infer_kind_and_risk(text: str) -> tuple[TicketKind, RiskLevel, bool]:
    lower = text.lower()
    external = any(marker in lower for marker in _DESTRUCTIVE_KEYWORDS)
    if external:
        return TicketKind.IMPLEMENT, RiskLevel.HIGH, True
    if any(tok in lower for tok in ("research", "investigate", "inspect", "explore")):
        return TicketKind.RESEARCH, RiskLevel.LOW, False
    if any(tok in lower for tok in ("review", "verify diff", "check behavior")):
        return TicketKind.REVIEW, RiskLevel.LOW, False
    if any(tok in lower for tok in ("test", "pytest", "unit test", "coverage")):
        return TicketKind.TEST, RiskLevel.LOW, False
    if any(tok in lower for tok in ("fix", "bug", "regression", "broken")):
        return TicketKind.FIX, RiskLevel.MEDIUM, False
    return TicketKind.IMPLEMENT, RiskLevel.MEDIUM, False


def _extract_scope_globs(segment: str, context: ProjectContextSnapshot) -> list[str]:
    globs: list[str] = []
    for token in _PATH_TOKEN_RE.findall(segment):
        cleaned = token.strip(".,:;`\"'")
        if cleaned and cleaned not in globs:
            globs.append(cleaned)
    if globs:
        return globs
    lower_seg = segment.lower()
    matched = [path for path in context.files if Path(path).name.lower() in lower_seg]
    return matched[:4] if matched else ["**"]


def _extract_dependency_indices(segment: str, current_idx: int) -> list[int]:
    deps: list[int] = []
    for raw_num in _DEP_REF_RE.findall(segment):
        ref_idx = int(raw_num) - 1
        if 0 <= ref_idx < current_idx and ref_idx not in deps:
            deps.append(ref_idx)
    return deps


async def heuristic_repartition(
    project_input: ProjectInput,
    context: ProjectContextSnapshot,
    max_tickets: int = 12,
    *,
    default_max_iterations: int = 12,
    default_max_tokens: int = 80_000,
) -> list[TicketDraft]:
    """Deterministically repartition multi-part text into bounded ``TicketDraft`` items."""
    segments = _split_input_segments(project_input.raw_text)[: max(1, max_tickets)]
    drafts: list[TicketDraft] = []
    memory_note = context.memory_entries[-1].text if context.memory_entries else ""
    for idx, seg in enumerate(segments):
        kind, risk, external = _infer_kind_and_risk(seg)
        globs = _extract_scope_globs(seg, context)
        dep_indices = _extract_dependency_indices(seg, idx)
        title = seg[:72].rstrip() + ("..." if len(seg) > 72 else "")
        goal = f"{seg}\n\n[Memory context] {memory_note}" if memory_note else seg
        drafts.append(
            TicketDraft(
                title=title,
                goal=goal,
                kind=kind,
                depends_on_indices=dep_indices,
                scope_globs=globs,
                risk_level=risk,
                requires_external_action=external,
                budget=TicketBudget(
                    max_iterations=default_max_iterations,
                    max_tokens=default_max_tokens,
                ),
            )
        )
    return drafts


class TicketLedger:
    """Durable append-only JSONL ledger with atomic snapshots and DAG queries."""

    def __init__(self, ledger_dir: str | Path | None = None) -> None:
        self._dir = Path(ledger_dir).resolve() if ledger_dir is not None else None
        self._journal_path = self._dir / "tickets.jsonl" if self._dir else None
        self._snapshot_path = self._dir / "state.json" if self._dir else None
        self._seq = 0
        self._seen_keys: set[str] = set()
        self._inputs: dict[str, ProjectInput] = {}
        self._tickets: dict[str, Ticket] = {}
        self._ticket_order: list[str] = []
        self._inbox: dict[str, InboxItem] = {}
        if self._dir is not None:
            self._dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def recover(cls, ledger_dir: str | Path) -> TicketLedger:
        """Recover ledger state from ``tickets.jsonl``, ignoring truncated tail writes."""
        ledger = cls(ledger_dir)
        assert ledger._journal_path is not None  # noqa: S101
        if not ledger._journal_path.is_file():
            return ledger
        raw_lines = ledger._journal_path.read_text(encoding="utf-8", errors="replace").splitlines()
        for raw_line in raw_lines:
            cleaned = raw_line.strip()
            if not cleaned:
                continue
            try:
                record = json.loads(cleaned)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                ledger._apply_journal_record(record)
        ledger._write_snapshot()
        return ledger

    def _apply_journal_record(self, record: dict[str, Any]) -> None:
        seq = record.get("seq")
        if isinstance(seq, int):
            self._seq = max(self._seq, seq)
        idem = record.get("idempotency_key")
        if isinstance(idem, str) and idem:
            self._seen_keys.add(idem)
        op = record.get("op")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return
        if op == "input":
            item = ProjectInput.model_validate(payload)
            self._inputs[item.input_id] = item
        elif op == "ticket":
            ticket = Ticket.model_validate(payload)
            if ticket.ticket_id not in self._tickets:
                self._ticket_order.append(ticket.ticket_id)
            self._tickets[ticket.ticket_id] = ticket
        elif op == "inbox":
            inbox = InboxItem.model_validate(payload)
            self._inbox[inbox.item_id] = inbox

    def _append_record(self, op: str, payload: dict[str, Any], idempotency_key: str | None) -> None:
        self._seq += 1
        key = idempotency_key or ""
        if key:
            self._seen_keys.add(key)
        if self._journal_path is not None:
            record = {
                "seq": self._seq,
                "op": op,
                "idempotency_key": key,
                "timestamp": datetime.now(UTC).isoformat(),
                "payload": payload,
            }
            line = json.dumps(record, ensure_ascii=False) + "\n"
            with self._journal_path.open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
        self._write_snapshot()

    def _write_snapshot(self) -> None:
        if self._snapshot_path is None:
            return
        snapshot = {
            "seq": self._seq,
            "inputs": [item.model_dump(mode="json") for item in self._inputs.values()],
            "tickets": [self._tickets[tid].model_dump(mode="json") for tid in self._ticket_order],
            "inbox": [item.model_dump(mode="json") for item in self._inbox.values()],
        }
        tmp = self._snapshot_path.with_name(f"{self._snapshot_path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._snapshot_path)

    def has_idempotency_key(self, key: str | None) -> bool:
        """Return True when *key* was already committed to this ledger."""
        return bool(key and key in self._seen_keys)

    def record_input(
        self,
        project_input: ProjectInput,
        *,
        idempotency_key: str | None = None,
    ) -> ProjectInput:
        """Persist a normalized project input idempotently."""
        if idempotency_key and idempotency_key in self._seen_keys:
            for existing in self._inputs.values():
                if existing.metadata.get("idempotency_key") == idempotency_key:
                    return existing
        if idempotency_key:
            project_input = project_input.model_copy(
                update={"metadata": {**project_input.metadata, "idempotency_key": idempotency_key}}
            )
        self._inputs[project_input.input_id] = project_input
        self._append_record("input", project_input.model_dump(mode="json"), idempotency_key)
        return project_input

    def add_ticket(self, ticket: Ticket, *, idempotency_key: str | None = None) -> Ticket:
        """Insert a new ticket cleanly into the ledger."""
        if idempotency_key and idempotency_key in self._seen_keys:
            return self._tickets[ticket.ticket_id]
        if ticket.ticket_id not in self._tickets:
            self._ticket_order.append(ticket.ticket_id)
        self._tickets[ticket.ticket_id] = ticket
        self._append_record("ticket", ticket.model_dump(mode="json"), idempotency_key)
        return ticket

    def transition_ticket(
        self,
        ticket_id: str,
        next_state: TicketState,
        *,
        idempotency_key: str | None = None,
        blocked_reason: str | None = None,
        branch: str | None = None,
        verification_summary: str | None = None,
        attempts: int | None = None,
        metadata_update: dict[str, Any] | None = None,
    ) -> Ticket:
        """Validate and persist a legal ticket state transition."""
        current = self.get_ticket(ticket_id)
        if idempotency_key and idempotency_key in self._seen_keys:
            return current
        allowed = VALID_TICKET_TRANSITIONS[current.state]
        if next_state not in allowed:
            msg = f"Illegal ticket transition for {ticket_id}: {current.state} -> {next_state}"
            raise InvalidTicketTransitionError(msg)
        updates: dict[str, Any] = {
            "state": next_state,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        if blocked_reason is not None:
            updates["blocked_reason"] = blocked_reason
        if branch is not None:
            updates["branch"] = branch
        if verification_summary is not None:
            updates["verification_summary"] = verification_summary
        if attempts is not None:
            updates["attempts"] = attempts
        if metadata_update is not None:
            updates["metadata"] = {**current.metadata, **metadata_update}
        updated = current.model_copy(update=updates)
        self._tickets[ticket_id] = updated
        self._append_record("ticket", updated.model_dump(mode="json"), idempotency_key)
        return updated

    def add_or_update_inbox_item(
        self,
        item: InboxItem,
        *,
        idempotency_key: str | None = None,
    ) -> InboxItem:
        """Persist a new or coalesced human Inbox escalation item."""
        if idempotency_key and idempotency_key in self._seen_keys and item.item_id in self._inbox:
            return self._inbox[item.item_id]
        self._inbox[item.item_id] = item
        self._append_record("inbox", item.model_dump(mode="json"), idempotency_key)
        return item

    def get_ticket(self, ticket_id: str) -> Ticket:
        """Return ticket by ID or raise KeyError."""
        return self._tickets[ticket_id]

    def get_input(self, input_id: str) -> ProjectInput:
        """Return project input by ID or raise KeyError."""
        return self._inputs[input_id]

    def list_tickets(
        self,
        *,
        state: TicketState | None = None,
        parent_input_id: str | None = None,
    ) -> list[Ticket]:
        """Return ordered tickets filtered by optional state or parent input."""
        items = [self._tickets[tid] for tid in self._ticket_order if tid in self._tickets]
        if state is not None:
            items = [t for t in items if t.state == state]
        if parent_input_id is not None:
            items = [t for t in items if t.parent_input_id == parent_input_id]
        return items

    def list_inbox(self, *, unresolved_only: bool = False) -> list[InboxItem]:
        """Return Inbox items sorted in insertion order."""
        items = list(self._inbox.values())
        return [item for item in items if not item.resolved] if unresolved_only else items

    def allocated_active_tokens(self) -> int:
        """Sum token budgets across active non-terminal tickets."""
        return sum(
            t.budget.max_tokens
            for t in self._tickets.values()
            if t.state not in _TERMINAL_TICKET_STATES
        )

    def topological_order(self) -> list[Ticket]:
        """Return tickets ordered so every dependency precedes its dependents."""
        all_tickets = self.list_tickets()
        known = {t.ticket_id for t in all_tickets}
        in_degree: dict[str, int] = {t.ticket_id: 0 for t in all_tickets}
        dependents: dict[str, list[str]] = {t.ticket_id: [] for t in all_tickets}
        for ticket in all_tickets:
            for dep in ticket.depends_on:
                if dep in known:
                    in_degree[ticket.ticket_id] += 1
                    dependents[dep].append(ticket.ticket_id)
        ready = deque(t.ticket_id for t in all_tickets if in_degree[t.ticket_id] == 0)
        ordered: list[Ticket] = []
        while ready:
            tid = ready.popleft()
            ordered.append(self._tickets[tid])
            for nxt in dependents[tid]:
                in_degree[nxt] -= 1
                if in_degree[nxt] == 0:
                    ready.append(nxt)
        if len(ordered) != len(all_tickets):
            raise ValueError("Cycle detected in Super-Agent ticket dependency graph")
        return ordered

    def has_cycle(self) -> bool:
        """Return True if ticket dependencies form a cycle."""
        try:
            self.topological_order()
        except ValueError:
            return True
        return False

    def _dependency_satisfied(self, dep_id: str) -> bool:
        dep = self._tickets.get(dep_id)
        if dep is None:
            return False
        if dep.state == TicketState.INTEGRATED:
            return True
        return dep.kind in _READ_ONLY_KINDS and dep.state == TicketState.READY_FOR_REVIEW

    def ready_to_dispatch(self) -> list[Ticket]:
        """Return ``ADMITTED`` tickets whose dependencies are satisfied."""
        return [
            t
            for t in self.list_tickets(state=TicketState.ADMITTED)
            if all(self._dependency_satisfied(dep) for dep in t.depends_on)
        ]


class AdmissionPolicy:
    """Explicit authorization matrix determining auto-admission vs human Inbox gating."""

    def __init__(
        self,
        *,
        mode: Literal["guarded", "strict", "auto"] = "guarded",
        max_total_tokens: int = 400_000,
        auto_approve_categories: Sequence[str] = (),
    ) -> None:
        self.mode = mode
        self.max_total_tokens = max_total_tokens
        self.auto_approved = frozenset(c.strip().lower() for c in auto_approve_categories if c)

    def _check_unsafe_scopes(self, ticket: Ticket) -> AdmissionDecision | None:
        for glob_pattern in ticket.scope_globs:
            if not is_scope_glob_safe(glob_pattern):
                return AdmissionDecision(
                    admitted=False,
                    reason=f"Unsafe scope glob outside repository boundary: {glob_pattern!r}",
                    inbox_kind="approval",
                    category="boundary_escape",
                )
        return None

    def _check_external_or_destructive(self, ticket: Ticket) -> AdmissionDecision | None:
        text = f"{ticket.title}\n{ticket.goal}".lower()
        has_keyword = any(marker in text for marker in _DESTRUCTIVE_KEYWORDS)
        if not ticket.requires_external_action and not has_keyword:
            return None
        if "external_or_destructive" in self.auto_approved:
            return None
        return AdmissionDecision(
            admitted=False,
            reason="External or destructive operation requires explicit human approval.",
            inbox_kind="approval",
            category="external_or_destructive",
        )

    def evaluate(self, ticket: Ticket, *, allocated_tokens: int = 0) -> AdmissionDecision:
        """Evaluate whether *ticket* can execute autonomously or must pause on the Inbox."""
        unsafe = self._check_unsafe_scopes(ticket)
        if unsafe is not None:
            return unsafe

        if allocated_tokens + ticket.budget.max_tokens > self.max_total_tokens:
            return AdmissionDecision(
                admitted=False,
                reason=(
                    f"Ticket token budget ({allocated_tokens + ticket.budget.max_tokens}) "
                    f"exceeds max_total_tokens ({self.max_total_tokens})."
                ),
                inbox_kind="budget",
                category="budget_limit",
            )

        ext = self._check_external_or_destructive(ticket)
        if ext is not None:
            return ext

        if (
            self.mode == "strict"
            and ticket.kind not in _READ_ONLY_KINDS
            and "strict_mutation" not in self.auto_approved
        ):
            return AdmissionDecision(
                admitted=False,
                reason="Strict admission mode requires approval for mutating tickets.",
                inbox_kind="approval",
                category="strict_mutation",
            )

        if (
            self.mode == "guarded"
            and ticket.risk_level == RiskLevel.HIGH
            and "high_risk" not in self.auto_approved
        ):
            return AdmissionDecision(
                admitted=False,
                reason="High-risk ticket paused for human approval under guarded mode.",
                inbox_kind="approval",
                category="high_risk",
            )

        return AdmissionDecision(admitted=True, reason="Admitted by policy", category="auto")


class AttentionBudget:
    """Caps concurrent human interrupts and coalesces overflow escalations."""

    def __init__(
        self,
        *,
        max_active_interrupts: int = 3,
        window_seconds: float = 300.0,
        max_interrupts_per_window: int = 5,
    ) -> None:
        self.max_active_interrupts = max(1, max_active_interrupts)
        self.window_seconds = max(1.0, window_seconds)
        self.max_interrupts_per_window = max(1, max_interrupts_per_window)
        self._recent_timestamps: deque[float] = deque()

    def _prune_window(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._recent_timestamps and self._recent_timestamps[0] <= cutoff:
            self._recent_timestamps.popleft()

    def enqueue_escalation(
        self,
        ledger: TicketLedger,
        *,
        kind: Literal["approval", "clarification", "review_bundle", "conflict", "budget"],
        ticket_ids: Sequence[str],
        summary: str,
        detail: str = "",
        options: Sequence[str] | None = None,
        idempotency_key: str | None = None,
    ) -> InboxItem:
        """Create a new Inbox item or coalesce into an active item if budget is reached."""
        now = time.monotonic()
        self._prune_window(now)
        unresolved = ledger.list_inbox(unresolved_only=True)
        within_active = len(unresolved) < self.max_active_interrupts
        within_rate = len(self._recent_timestamps) < self.max_interrupts_per_window
        if within_active and within_rate:
            self._recent_timestamps.append(now)
            item = InboxItem(
                kind=kind,
                ticket_ids=list(ticket_ids),
                summary=summary,
                detail=detail,
                options=list(options) if options is not None else ["approve", "reject"],
            )
            return ledger.add_or_update_inbox_item(item, idempotency_key=idempotency_key)

        target = next((it for it in unresolved if it.kind == kind), unresolved[0])
        merged_ids = list(target.ticket_ids)
        for tid in ticket_ids:
            if tid not in merged_ids:
                merged_ids.append(tid)
        extra_line = f"- {summary}" + (f" ({detail})" if detail else "")
        merged_detail = f"{target.detail}\n{extra_line}".strip()
        updated = target.model_copy(
            update={
                "ticket_ids": merged_ids,
                "summary": f"{target.summary} (+{len(merged_ids) - 1} batched)",
                "detail": merged_detail,
            }
        )
        return ledger.add_or_update_inbox_item(updated, idempotency_key=idempotency_key)

    def resolve_item(
        self,
        ledger: TicketLedger,
        item_id: str,
        decision: Literal["approve", "reject"],
        *,
        resolution_note: str = "",
    ) -> list[Ticket]:
        """Resolve an Inbox item and advance linked ``BLOCKED_ON_HUMAN`` tickets."""
        items = {it.item_id: it for it in ledger.list_inbox(unresolved_only=False)}
        item = items[item_id]
        resolved_item = item.model_copy(
            update={
                "resolved": True,
                "resolution": f"{decision}: {resolution_note}".strip(": "),
                "resolved_at": datetime.now(UTC).isoformat(),
            }
        )
        ledger.add_or_update_inbox_item(resolved_item)
        target_state = TicketState.ADMITTED if decision == "approve" else TicketState.CANCELLED
        transitioned: list[Ticket] = []
        for tid in item.ticket_ids:
            ticket = ledger.get_ticket(tid)
            if ticket.state == TicketState.BLOCKED_ON_HUMAN:
                transitioned.append(
                    ledger.transition_ticket(
                        tid,
                        target_state,
                        blocked_reason="" if decision == "approve" else resolution_note,
                    )
                )
        return transitioned


class SuperAgentRouter:
    """Project-level intake normalizer, DAG repartition planner, and admission router."""

    def __init__(
        self,
        *,
        repo_path: str | Path,
        ledger: TicketLedger | None = None,
        admission: AdmissionPolicy | None = None,
        attention_budget: AttentionBudget | None = None,
        planner_fn: TicketPlannerFn | None = None,
        event_bus: EventBus | None = None,
        max_tickets_per_input: int = 12,
        default_ticket_max_iterations: int = 12,
        default_ticket_max_tokens: int = 80_000,
    ) -> None:
        self.repo_path = Path(repo_path).resolve()
        self.ledger = ledger or TicketLedger()
        self.admission = admission or AdmissionPolicy()
        self.attention_budget = attention_budget or AttentionBudget()
        self._planner_fn = planner_fn
        self._event_bus = event_bus
        self._max_tickets_per_input = max_tickets_per_input
        self._default_ticket_max_iterations = default_ticket_max_iterations
        self._default_ticket_max_tokens = default_ticket_max_tokens

    async def _publish(self, event_type: EventType, run_id: str, data: dict[str, Any]) -> None:
        if self._event_bus is not None:
            await self._event_bus.publish(Event(type=event_type, run_id=run_id, data=data))

    async def _plan_drafts(
        self,
        project_input: ProjectInput,
        context: ProjectContextSnapshot,
    ) -> list[TicketDraft]:
        if self._planner_fn is not None:
            drafts = list(
                await self._planner_fn(project_input, context, self._max_tickets_per_input)
            )
            return drafts[: self._max_tickets_per_input]
        return await heuristic_repartition(
            project_input,
            context,
            self._max_tickets_per_input,
            default_max_iterations=self._default_ticket_max_iterations,
            default_max_tokens=self._default_ticket_max_tokens,
        )

    def _build_tickets_from_drafts(
        self,
        project_input: ProjectInput,
        drafts: Sequence[TicketDraft],
    ) -> list[Ticket]:
        raw_tickets = [
            Ticket(
                parent_input_id=project_input.input_id,
                title=draft.title,
                goal=draft.goal,
                kind=draft.kind,
                scope_globs=list(draft.scope_globs),
                risk_level=draft.risk_level,
                requires_external_action=draft.requires_external_action,
                budget=draft.budget,
                metadata=dict(draft.metadata),
            )
            for draft in drafts
        ]
        for idx, draft in enumerate(drafts):
            for dep_idx in draft.depends_on_indices:
                if dep_idx == idx or dep_idx < 0 or dep_idx >= len(raw_tickets):
                    raise ValueError(f"Invalid dependency index {dep_idx} on draft {idx}")
                dep_id = raw_tickets[dep_idx].ticket_id
                if dep_id not in raw_tickets[idx].depends_on:
                    raw_tickets[idx].depends_on.append(dep_id)
        return serialize_overlapping_scopes(raw_tickets)

    async def route_input(
        self,
        raw_text: str,
        *,
        source: Literal["cli", "acp", "studio", "webhook", "api"] = "cli",
        idempotency_key: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RouteResult:
        """Normalize incoming input, repartition into DAG tickets, and apply admission policy."""
        cleaned = raw_text.strip()
        if not cleaned:
            raise ValueError("Project input raw_text must not be empty")

        if idempotency_key and self.ledger.has_idempotency_key(idempotency_key):
            existing_input = self.ledger.record_input(
                ProjectInput(source=source, raw_text=cleaned, repo_path=str(self.repo_path)),
                idempotency_key=idempotency_key,
            )
            existing_tickets = tuple(
                self.ledger.list_tickets(parent_input_id=existing_input.input_id)
            )
            admitted = tuple(
                t.ticket_id for t in existing_tickets if t.state == TicketState.ADMITTED
            )
            blocked = tuple(
                t.ticket_id for t in existing_tickets if t.state == TicketState.BLOCKED_ON_HUMAN
            )
            return RouteResult(
                project_input=existing_input,
                tickets=existing_tickets,
                admitted_ids=admitted,
                blocked_ids=blocked,
                inbox_items=tuple(self.ledger.list_inbox(unresolved_only=True)),
            )

        project_input = self.ledger.record_input(
            ProjectInput(
                source=source,
                raw_text=cleaned,
                repo_path=str(self.repo_path),
                metadata=dict(metadata or {}),
            ),
            idempotency_key=idempotency_key,
        )
        await self._publish(
            EventType.SUPER_INPUT_RECEIVED,
            project_input.input_id,
            {"source": source, "input_id": project_input.input_id},
        )

        context = await asyncio.to_thread(capture_project_context, self.repo_path)
        drafts = await self._plan_drafts(project_input, context)
        built_tickets = self._build_tickets_from_drafts(project_input, drafts)

        for ticket in built_tickets:
            self.ledger.add_ticket(ticket)
        if self.ledger.has_cycle():
            raise ValueError("Repartitioned ticket graph contains a dependency cycle")

        admitted_ids: list[str] = []
        blocked_ids: list[str] = []
        created_inbox: list[InboxItem] = []
        final_tickets: list[Ticket] = []

        for ticket in built_tickets:
            triaged = self.ledger.transition_ticket(ticket.ticket_id, TicketState.TRIAGED)
            decision = self.admission.evaluate(
                triaged,
                allocated_tokens=self.ledger.allocated_active_tokens() - triaged.budget.max_tokens,
            )
            if decision.admitted:
                admitted_ticket = self.ledger.transition_ticket(
                    triaged.ticket_id, TicketState.ADMITTED
                )
                admitted_ids.append(admitted_ticket.ticket_id)
                final_tickets.append(admitted_ticket)
                await self._publish(
                    EventType.SUPER_TICKET_UPDATED,
                    project_input.input_id,
                    {"ticket_id": admitted_ticket.ticket_id, "state": admitted_ticket.state.value},
                )
            else:
                blocked_ticket = self.ledger.transition_ticket(
                    triaged.ticket_id,
                    TicketState.BLOCKED_ON_HUMAN,
                    blocked_reason=decision.reason,
                )
                blocked_ids.append(blocked_ticket.ticket_id)
                final_tickets.append(blocked_ticket)
                inbox_item = self.attention_budget.enqueue_escalation(
                    self.ledger,
                    kind=decision.inbox_kind or "approval",
                    ticket_ids=[blocked_ticket.ticket_id],
                    summary=f"{blocked_ticket.title}: {decision.reason}",
                    detail=blocked_ticket.goal,
                )
                if all(it.item_id != inbox_item.item_id for it in created_inbox):
                    created_inbox.append(inbox_item)
                else:
                    created_inbox = [
                        inbox_item if it.item_id == inbox_item.item_id else it
                        for it in created_inbox
                    ]
                await self._publish(
                    EventType.SUPER_INBOX_UPDATED,
                    project_input.input_id,
                    {"item_id": inbox_item.item_id, "ticket_ids": inbox_item.ticket_ids},
                )

        return RouteResult(
            project_input=project_input,
            tickets=tuple(final_tickets),
            admitted_ids=tuple(admitted_ids),
            blocked_ids=tuple(blocked_ids),
            inbox_items=tuple(created_inbox),
        )


__all__ = [
    "VALID_TICKET_TRANSITIONS",
    "AdmissionDecision",
    "AdmissionPolicy",
    "AttentionBudget",
    "InboxItem",
    "InvalidTicketTransitionError",
    "ProjectContextSnapshot",
    "ProjectInput",
    "RiskLevel",
    "RouteResult",
    "SuperAgentRouter",
    "Ticket",
    "TicketBudget",
    "TicketDraft",
    "TicketKind",
    "TicketLedger",
    "TicketPlannerFn",
    "TicketState",
    "capture_project_context",
    "heuristic_repartition",
    "is_scope_glob_safe",
    "scopes_overlap",
    "serialize_overlapping_scopes",
]
