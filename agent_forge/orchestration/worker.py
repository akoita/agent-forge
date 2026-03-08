"""Queue worker — processes tasks from the task queue.

Pulls tasks from a :class:`TaskQueue`, runs them through the agent
pipeline, and publishes lifecycle events to the :class:`EventBus`.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any

from agent_forge.observability import get_logger
from agent_forge.orchestration.events import Event, EventBus, EventType
from agent_forge.orchestration.queue import TaskQueue, TaskStatus

logger = get_logger("worker")

# Type for the task runner callable
TaskRunner = Callable[..., Coroutine[Any, Any, Any]]


class Worker:
    """Polls a :class:`TaskQueue` and executes tasks.

    The worker runs as an async background loop.  For each task it
    dequeues, it invokes the configured *task_runner* callable and
    publishes lifecycle events (RUN_STARTED, RUN_COMPLETED, RUN_FAILED)
    to the :class:`EventBus`.

    Args:
        queue: The task queue to poll.
        event_bus: The event bus for lifecycle events.
        task_runner: An async callable that executes a task.
            Receives the :class:`Task` as its sole argument.
        poll_interval: Seconds between queue polls when idle.
    """

    def __init__(
        self,
        queue: TaskQueue,
        event_bus: EventBus,
        task_runner: TaskRunner,
        *,
        poll_interval: float = 1.0,
    ) -> None:
        self._queue = queue
        self._event_bus = event_bus
        self._task_runner = task_runner
        self._poll_interval = poll_interval
        self._running = False
        self._task: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        """Whether the worker loop is currently active."""
        return self._running

    async def start(self) -> None:
        """Start the worker loop as a background task."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("worker_started")

    async def stop(self) -> None:
        """Gracefully stop the worker loop.

        Waits for the current task (if any) to finish processing.
        """
        if not self._running:
            return
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("worker_stopped")

    async def _run_loop(self) -> None:
        """Main polling loop."""
        while self._running:
            task = await self._queue.dequeue()
            if task is None:
                await asyncio.sleep(self._poll_interval)
                continue

            await self._process_task(task)

    async def _process_task(self, task: Any) -> None:
        """Execute a single task and publish lifecycle events."""
        # Publish RUN_STARTED
        await self._event_bus.publish(
            Event(
                type=EventType.RUN_STARTED,
                run_id=task.id,
                timestamp=datetime.now(UTC),
                data={"task_description": task.task_description},
            )
        )

        try:
            await self._task_runner(task)
            task.status = TaskStatus.COMPLETED

            await self._event_bus.publish(
                Event(
                    type=EventType.RUN_COMPLETED,
                    run_id=task.id,
                    timestamp=datetime.now(UTC),
                )
            )
            logger.info("task_completed", task_id=task.id)

        except Exception as exc:  # noqa: BLE001
            task.status = TaskStatus.FAILED

            await self._event_bus.publish(
                Event(
                    type=EventType.RUN_FAILED,
                    run_id=task.id,
                    timestamp=datetime.now(UTC),
                    data={"error": str(exc)},
                )
            )
            logger.exception("task_failed", task_id=task.id, exc_info=exc)
