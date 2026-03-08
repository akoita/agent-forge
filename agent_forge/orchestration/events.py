"""Event bus — in-process pub/sub for run lifecycle events.

Provides an async publish/subscribe mechanism for decoupled
communication between agent components per spec § 4.5.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from agent_forge.observability import get_logger

logger = get_logger("event_bus")

# Type alias for event handler callbacks
EventHandler = Any  # Callable[[Event], Awaitable[None]]


class EventType(Enum):
    """Run lifecycle event types per spec § 4.5."""

    RUN_STARTED = "run.started"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    ITERATION_STARTED = "iteration.started"
    ITERATION_COMPLETED = "iteration.completed"
    TOOL_CALLED = "tool.called"
    TOOL_COMPLETED = "tool.completed"
    TOKEN_USAGE = "token.usage"  # noqa: S105 — not a password


@dataclass
class Event:
    """A single lifecycle event."""

    type: EventType
    run_id: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    data: dict[str, Any] = field(default_factory=dict)


class EventBus:
    """In-process async pub/sub for run lifecycle events.

    Handlers are called sequentially in subscription order when an
    event is published.  Exceptions in handlers are logged but do
    not interrupt other handlers or the publisher.
    """

    def __init__(self) -> None:
        self._handlers: dict[EventType, list[tuple[str, EventHandler]]] = (
            defaultdict(list)
        )

    async def publish(self, event: Event) -> None:
        """Broadcast *event* to all subscribed handlers.

        Handler exceptions are logged and swallowed so they never
        propagate to the caller.
        """
        handlers = self._handlers.get(event.type, [])
        for sub_id, handler in handlers:
            try:
                await handler(event)
            except Exception:
                logger.exception(
                    "event_handler_error",
                    subscription_id=sub_id,
                    event_type=event.type.value,
                )

    async def subscribe(
        self,
        event_type: EventType,
        handler: EventHandler,
    ) -> str:
        """Register *handler* for *event_type*.

        Returns a subscription ID that can be passed to
        :meth:`unsubscribe` to remove the handler.
        """
        sub_id = str(uuid.uuid4())
        self._handlers[event_type].append((sub_id, handler))
        return sub_id

    async def unsubscribe(self, subscription_id: str) -> None:
        """Remove the handler identified by *subscription_id*.

        Silently does nothing if the subscription has already been
        removed or never existed.
        """
        for event_type, handler_list in self._handlers.items():
            self._handlers[event_type] = [
                (sid, h) for sid, h in handler_list if sid != subscription_id
            ]
