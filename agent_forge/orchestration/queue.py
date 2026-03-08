"""Task queue — abstract base and in-memory implementation.

Provides the task queue abstraction and an in-memory priority-based
implementation per spec § 4.5.
"""

from __future__ import annotations

import asyncio
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from agent_forge.observability import get_logger

logger = get_logger("task_queue")


class TaskStatus(Enum):
    """Lifecycle status of a queued task."""

    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Task:
    """A unit of work to be processed by a worker."""

    id: str
    task_description: str
    repo_path: str
    config: Any  # AgentConfig — use Any to avoid circular imports
    priority: int = 0  # Higher = more urgent
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    status: TaskStatus = TaskStatus.QUEUED

    def __lt__(self, other: Task) -> bool:
        """Support comparison for priority queue (higher priority first)."""
        return self.priority > other.priority


class TaskQueue(ABC):
    """Abstract interface for task queues."""

    @abstractmethod
    async def enqueue(self, task: Task) -> str:
        """Add a task to the queue.  Returns the task ID."""
        ...

    @abstractmethod
    async def dequeue(self) -> Task | None:
        """Get the next highest-priority task, or ``None`` if empty."""
        ...

    @abstractmethod
    async def get_status(self, task_id: str) -> TaskStatus:
        """Look up the current status of a task.

        Raises:
            KeyError: If the task ID is unknown.
        """
        ...

    @abstractmethod
    async def cancel(self, task_id: str) -> bool:
        """Cancel a queued task.

        Returns ``True`` if the task was successfully cancelled,
        ``False`` if it was already processing/completed/failed.
        """
        ...


class InMemoryQueue(TaskQueue):
    """Priority-based in-memory task queue.

    Uses :class:`asyncio.PriorityQueue` under the hood.  Higher
    ``priority`` values are dequeued first.  Suitable for development,
    testing, and single-process deployments.
    """

    def __init__(self) -> None:
        self._queue: asyncio.PriorityQueue[Task] = asyncio.PriorityQueue()
        self._tasks: dict[str, Task] = {}

    @property
    def size(self) -> int:
        """Number of tasks currently in the queue."""
        return self._queue.qsize()

    async def enqueue(self, task: Task) -> str:
        """Add a task to the queue."""
        if not task.id:
            task.id = str(uuid.uuid4())
        task.status = TaskStatus.QUEUED
        self._tasks[task.id] = task
        await self._queue.put(task)
        logger.info(
            "task_enqueued",
            task_id=task.id,
            priority=task.priority,
        )
        return task.id

    async def dequeue(self) -> Task | None:
        """Get the next highest-priority task, or ``None`` if empty."""
        if self._queue.empty():
            return None

        task = self._queue.get_nowait()

        # Skip cancelled tasks that are still in the underlying queue
        while task.status != TaskStatus.QUEUED:
            if self._queue.empty():
                return None
            task = self._queue.get_nowait()

        task.status = TaskStatus.PROCESSING
        logger.info("task_dequeued", task_id=task.id)
        return task

    async def get_status(self, task_id: str) -> TaskStatus:
        """Look up the current status of a task."""
        task = self._tasks.get(task_id)
        if task is None:
            msg = f"Unknown task ID: {task_id}"
            raise KeyError(msg)
        return task.status

    async def cancel(self, task_id: str) -> bool:
        """Cancel a queued task.

        Only tasks with ``QUEUED`` status can be cancelled.
        """
        task = self._tasks.get(task_id)
        if task is None:
            msg = f"Unknown task ID: {task_id}"
            raise KeyError(msg)

        if task.status != TaskStatus.QUEUED:
            return False

        task.status = TaskStatus.FAILED
        logger.info("task_cancelled", task_id=task_id)
        return True
