"""Durable, typed run-event stream backed by an append-only JSONL journal.

Both the CLI (``agent-forge events``, ``status --follow``) and the hosted
service (``GET /v1/runs/{run_id}/events``) read from :class:`RunEventStream`
so consumers observe one versioned event stream with monotonic sequence
offsets and lossless reconnect/resume.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent_forge.agent import persistence
from agent_forge.orchestration.events import EventType

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from agent_forge.orchestration.events import Event, EventBus

STREAM_SCHEMA_VERSION: Literal["run-event-v1"] = "run-event-v1"
STREAM_FILENAME = "stream.jsonl"
DEFAULT_MAX_FIELD_CHARS = 4096
DEFAULT_MAX_LIST_ITEMS = 100
DEFAULT_PAGE_LIMIT = 100
MAX_PAGE_LIMIT = 1000

TERMINAL_STREAM_EVENTS: frozenset[str] = frozenset(
    {
        EventType.RUN_COMPLETED.value,
        EventType.RUN_FAILED.value,
        EventType.RUN_CANCELLED.value,
        EventType.RUN_PAUSED.value,
        EventType.SUPER_RUN_COMPLETED.value,
    }
)


class StreamEvent(BaseModel):
    """Typed, offset-indexed run event suitable for JSON pagination and SSE."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["run-event-v1"] = STREAM_SCHEMA_VERSION
    seq: int = Field(ge=0)
    type: str
    run_id: str
    timestamp: str
    data: dict[str, Any] = Field(default_factory=dict)
    truncated: bool = False

    def to_sse(self) -> str:
        """Render this event as a single Server-Sent Events frame."""
        return f"id: {self.seq}\nevent: {self.type}\ndata: {self.model_dump_json()}\n\n"


class StreamEventPage(BaseModel):
    """Paginated slice of durable run events after a sequence offset."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    events: list[StreamEvent]
    next_after_seq: int
    has_more: bool
    terminal: bool


def _truncate_text(text: str, *, max_field_chars: int) -> tuple[str, bool]:
    if len(text) <= max_field_chars:
        return text, False
    return f"{text[:max_field_chars]}... [truncated]", True


def _sanitize_value(
    value: Any,
    *,
    max_field_chars: int,
    max_list_items: int,
) -> tuple[Any, bool]:
    if value is None or isinstance(value, bool | int | float):
        return value, False
    if isinstance(value, str):
        return _truncate_text(value, max_field_chars=max_field_chars)
    if isinstance(value, list | tuple):
        kept = list(value[:max_list_items])
        truncated = len(value) > max_list_items
        sanitized_items: list[Any] = []
        for item in kept:
            clean_item, item_truncated = _sanitize_value(
                item,
                max_field_chars=max_field_chars,
                max_list_items=max_list_items,
            )
            sanitized_items.append(clean_item)
            truncated = truncated or item_truncated
        return sanitized_items, truncated
    if isinstance(value, dict):
        return sanitize_event_data(
            {str(k): v for k, v in value.items()},
            max_field_chars=max_field_chars,
            max_list_items=max_list_items,
        )
    return _truncate_text(str(value), max_field_chars=max_field_chars)


def sanitize_event_data(
    data: dict[str, Any],
    *,
    max_field_chars: int = DEFAULT_MAX_FIELD_CHARS,
    max_list_items: int = DEFAULT_MAX_LIST_ITEMS,
) -> tuple[dict[str, Any], bool]:
    """Bound payload size and ensure JSON-safe event data for streaming."""
    sanitized: dict[str, Any] = {}
    truncated = False
    for key, value in data.items():
        clean_value, value_truncated = _sanitize_value(
            value,
            max_field_chars=max_field_chars,
            max_list_items=max_list_items,
        )
        sanitized[str(key)] = clean_value
        truncated = truncated or value_truncated
    return sanitized, truncated


class EventJournal:
    """Append-only JSONL journal for a single run's typed events."""

    def __init__(
        self,
        run_id: str,
        *,
        base_dir: Path | str | None = None,
        max_field_chars: int = DEFAULT_MAX_FIELD_CHARS,
        max_list_items: int = DEFAULT_MAX_LIST_ITEMS,
    ) -> None:
        self.run_id = run_id
        self._base_dir: Path | None = Path(base_dir) if base_dir is not None else None
        self._max_field_chars = max_field_chars
        self._max_list_items = max_list_items
        self._next_seq: int | None = None

    @property
    def path(self) -> Path:
        """Return the journal file path for this run."""
        root = self._base_dir if self._base_dir is not None else persistence._default_runs_dir()
        return root / self.run_id / STREAM_FILENAME

    def _resolve_next_seq(self) -> int:
        if self._next_seq is not None:
            return self._next_seq
        existing = self._load_events()
        self._next_seq = (existing[-1].seq + 1) if existing else 0
        return self._next_seq

    def _load_events(self) -> list[StreamEvent]:
        if not self.path.exists():
            return []
        events: list[StreamEvent] = []
        for raw_line in self.path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if line:
                events.append(StreamEvent.model_validate_json(line))
        return events

    def append(self, event: Event) -> StreamEvent:
        """Serialize, bound, and append an :class:`Event` to disk."""
        seq = self._resolve_next_seq()
        clean_data, truncated = sanitize_event_data(
            event.data,
            max_field_chars=self._max_field_chars,
            max_list_items=self._max_list_items,
        )
        stream_event = StreamEvent(
            seq=seq,
            type=event.type.value,
            run_id=event.run_id,
            timestamp=event.timestamp.isoformat(),
            data=clean_data,
            truncated=truncated,
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(stream_event.model_dump_json() + "\n")
            handle.flush()
        self._next_seq = seq + 1
        return stream_event

    def read_page(
        self,
        *,
        after_seq: int = -1,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> StreamEventPage:
        """Read a bounded slice of events strictly after *after_seq*."""
        clamped_limit = max(1, min(limit, MAX_PAGE_LIMIT))
        events = self._load_events()
        terminal = bool(events) and events[-1].type in TERMINAL_STREAM_EVENTS
        filtered = [ev for ev in events if ev.seq > after_seq]
        page_events = filtered[:clamped_limit]
        has_more = len(filtered) > clamped_limit
        next_after_seq = page_events[-1].seq if page_events else after_seq
        return StreamEventPage(
            run_id=self.run_id,
            events=page_events,
            next_after_seq=next_after_seq,
            has_more=has_more,
            terminal=terminal,
        )


class JournalEventSink:
    """EventBus subscriber that durably appends emitted events to disk."""

    def __init__(
        self,
        *,
        base_dir: Path | str | None = None,
        run_id: str | None = None,
        max_field_chars: int = DEFAULT_MAX_FIELD_CHARS,
        max_list_items: int = DEFAULT_MAX_LIST_ITEMS,
    ) -> None:
        self._base_dir: Path | str | None = base_dir
        self._run_id = run_id
        self._max_field_chars = max_field_chars
        self._max_list_items = max_list_items
        self._journals: dict[str, EventJournal] = {}
        self._subscription_ids: list[str] = []

    async def attach(self, event_bus: EventBus) -> None:
        """Subscribe to all lifecycle event types on *event_bus*."""
        for event_type in EventType:
            sub_id = await event_bus.subscribe(event_type, self._handle_event)
            self._subscription_ids.append(sub_id)

    async def detach(self, event_bus: EventBus) -> None:
        """Remove all subscriptions registered by :meth:`attach`."""
        while self._subscription_ids:
            sub_id = self._subscription_ids.pop()
            await event_bus.unsubscribe(sub_id)

    async def _handle_event(self, event: Event) -> None:
        if self._run_id is not None and event.run_id != self._run_id:
            return
        journal = self._journals.get(event.run_id)
        if journal is None:
            journal = EventJournal(
                event.run_id,
                base_dir=self._base_dir,
                max_field_chars=self._max_field_chars,
                max_list_items=self._max_list_items,
            )
            self._journals[event.run_id] = journal
        journal.append(event)


class RunEventStream:
    """Shared resumable reader over a run's durable event journal."""

    def __init__(self, run_id: str, *, base_dir: Path | str | None = None) -> None:
        self.run_id = run_id
        self._journal = EventJournal(run_id, base_dir=base_dir)

    @property
    def journal_path(self) -> Path:
        """Return the underlying journal file path."""
        return self._journal.path

    def exists(self) -> bool:
        """Return whether a journal file exists on disk for this run."""
        return self._journal.path.exists()

    def read_page(
        self,
        *,
        after_seq: int = -1,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> StreamEventPage:
        """Return a paginated slice of persisted events."""
        return self._journal.read_page(after_seq=after_seq, limit=limit)

    async def follow(
        self,
        *,
        after_seq: int = -1,
        page_size: int = DEFAULT_PAGE_LIMIT,
        poll_interval: float = 0.05,
        stop_on_terminal: bool = True,
        max_idle_seconds: float | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Yield events starting after *after_seq*, tailing until completion."""
        cursor = after_seq
        idle_since: float | None = None
        while True:
            page = self.read_page(after_seq=cursor, limit=page_size)
            if page.events:
                idle_since = None
                for event in page.events:
                    cursor = event.seq
                    yield event
                if stop_on_terminal and not page.has_more and page.terminal:
                    return
                continue
            if stop_on_terminal and not page.has_more and page.terminal:
                return
            now = time.monotonic()
            if idle_since is None:
                idle_since = now
            elif max_idle_seconds is not None and (now - idle_since) >= max_idle_seconds:
                return
            await asyncio.sleep(poll_interval)
