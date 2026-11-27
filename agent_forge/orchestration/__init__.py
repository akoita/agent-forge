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
from agent_forge.orchestration.super_agent import (
    VALID_TICKET_TRANSITIONS,
    AdmissionDecision,
    AdmissionPolicy,
    AttentionBudget,
    InboxItem,
    InvalidTicketTransitionError,
    ProjectContextSnapshot,
    ProjectInput,
    RiskLevel,
    RouteResult,
    SuperAgentRouter,
    Ticket,
    TicketBudget,
    TicketDraft,
    TicketKind,
    TicketLedger,
    TicketPlannerFn,
    TicketState,
    capture_project_context,
    heuristic_repartition,
    is_scope_glob_safe,
    scopes_overlap,
    serialize_overlapping_scopes,
)
from agent_forge.orchestration.super_agent_dispatcher import (
    ConflictContext,
    ConflictRemediatorFn,
    DispatchBatchResult,
    SubagentTicketWorker,
    SuperAgentDispatcher,
    TicketDispatchOutcome,
    TicketExecutionContext,
    TicketVerificationResult,
    TicketVerifierFn,
    TicketWorkerFn,
    WorkerExecutionResult,
    verify_patch_independently,
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
    "VALID_TICKET_TRANSITIONS",
    "AdmissionDecision",
    "AdmissionPolicy",
    "AttentionBudget",
    "ConflictContext",
    "ConflictRemediatorFn",
    "DispatchBatchResult",
    "Event",
    "EventBus",
    "EventJournal",
    "EventType",
    "InMemoryQueue",
    "InboxItem",
    "IntegrationResult",
    "InvalidTicketTransitionError",
    "JournalEventSink",
    "ProjectContextSnapshot",
    "ProjectInput",
    "RedisQueue",
    "RiskLevel",
    "RouteResult",
    "RunEventStream",
    "StreamEvent",
    "StreamEventPage",
    "SubagentTicketWorker",
    "SuperAgentDispatcher",
    "SuperAgentRouter",
    "Task",
    "TaskQueue",
    "TaskStatus",
    "Ticket",
    "TicketBudget",
    "TicketDispatchOutcome",
    "TicketDraft",
    "TicketExecutionContext",
    "TicketKind",
    "TicketLedger",
    "TicketPlannerFn",
    "TicketState",
    "TicketVerificationResult",
    "TicketVerifierFn",
    "TicketWorkerFn",
    "Worker",
    "WorkerExecutionResult",
    "WorktreeError",
    "WorktreeInfo",
    "WorktreeLease",
    "WorktreeManager",
    "WorktreeState",
    "capture_project_context",
    "heuristic_repartition",
    "is_git_repo",
    "is_scope_glob_safe",
    "scopes_overlap",
    "serialize_overlapping_scopes",
    "verify_patch_independently",
]


def __getattr__(name: str) -> object:
    """Lazy-import RedisQueue to avoid hard dependency on redis."""
    if name == "RedisQueue":
        from agent_forge.orchestration.redis_queue import RedisQueue

        return RedisQueue
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
