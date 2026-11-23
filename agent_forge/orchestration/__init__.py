"""Orchestration layer — task queue, event bus, and workers."""

from agent_forge.orchestration.event_stream import (
    TERMINAL_STREAM_EVENTS,
    EventJournal,
    JournalEventSink,
    RunEventStream,
    StreamEvent,
    StreamEventPage,
)
from agent_forge.orchestration.events import Event, EventBus, EventType
from agent_forge.orchestration.queue import (
    InMemoryQueue,
    Task,
    TaskQueue,
    TaskStatus,
)
from agent_forge.orchestration.worker import Worker
from agent_forge.orchestration.worktree import (
    DEFAULT_BRANCH_PREFIX,
    IntegrationResult,
    WorktreeError,
    WorktreeInfo,
    WorktreeLease,
    WorktreeManager,
    WorktreeState,
    is_git_repo,
)

__all__ = [
    "DEFAULT_BRANCH_PREFIX",
    "TERMINAL_STREAM_EVENTS",
    "Event",
    "EventBus",
    "EventJournal",
    "EventType",
    "InMemoryQueue",
    "IntegrationResult",
    "JournalEventSink",
    "RedisQueue",
    "RunEventStream",
    "StreamEvent",
    "StreamEventPage",
    "Task",
    "TaskQueue",
    "TaskStatus",
    "Worker",
    "WorktreeError",
    "WorktreeInfo",
    "WorktreeLease",
    "WorktreeManager",
    "WorktreeState",
    "is_git_repo",
]


def __getattr__(name: str) -> object:
    """Lazy-import RedisQueue to avoid hard dependency on redis."""
    if name == "RedisQueue":
        from agent_forge.orchestration.redis_queue import RedisQueue

        return RedisQueue
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
