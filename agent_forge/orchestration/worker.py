"""Queue worker — processes tasks from the task queue.

Pulls tasks from a :class:`TaskQueue`, runs them through the agent
pipeline (optionally inside isolated Git worktrees), and publishes
lifecycle events to the :class:`EventBus`.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Coroutine
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_forge.agent.models import AgentRun, RunState
from agent_forge.observability import get_logger
from agent_forge.orchestration.events import Event, EventBus, EventType
from agent_forge.orchestration.queue import Task, TaskQueue, TaskStatus
from agent_forge.orchestration.worktree import (
    DEFAULT_BRANCH_PREFIX,
    WorktreeLease,
    WorktreeManager,
)

logger = get_logger("worker")

# Type for the task runner callable
TaskRunner = Callable[..., Coroutine[Any, Any, Any]]


class Worker:
    """Polls a :class:`TaskQueue` and executes tasks.

    Args:
        queue: The task queue to poll.
        event_bus: The event bus for lifecycle events.
        task_runner: An async callable that executes a task.
            Receives the :class:`Task` as its sole argument.
        poll_interval: Seconds between queue polls when idle.
        max_concurrent_tasks: Maximum tasks executed concurrently.
        worktree_isolation: Isolate each task in its own Git worktree.
        worktree_root: Optional root directory for managed worktrees.
        worktree_branch_prefix: Git branch prefix for isolated runs.
        auto_remove_unchanged_worktrees: Remove worktrees when no files changed.
        keep_changed_worktrees: Keep checked-out worktrees after commit.
    """

    def __init__(
        self,
        queue: TaskQueue,
        event_bus: EventBus,
        task_runner: TaskRunner,
        *,
        poll_interval: float = 1.0,
        max_concurrent_tasks: int = 1,
        worktree_isolation: bool = False,
        worktree_root: str | Path | None = None,
        worktree_branch_prefix: str = DEFAULT_BRANCH_PREFIX,
        auto_remove_unchanged_worktrees: bool = True,
        keep_changed_worktrees: bool = False,
    ) -> None:
        if max_concurrent_tasks < 1:
            msg = "max_concurrent_tasks must be >= 1"
            raise ValueError(msg)
        self._queue = queue
        self._event_bus = event_bus
        self._task_runner = task_runner
        self._poll_interval = poll_interval
        self._max_concurrent_tasks = max_concurrent_tasks
        self._worktree_isolation = worktree_isolation
        self._worktree_root = worktree_root
        self._worktree_branch_prefix = worktree_branch_prefix
        self._auto_remove_unchanged_worktrees = auto_remove_unchanged_worktrees
        self._keep_changed_worktrees = keep_changed_worktrees
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._semaphore = asyncio.Semaphore(max_concurrent_tasks)
        self._in_flight: set[asyncio.Task[None]] = set()
        self._worktree_managers: dict[Path, WorktreeManager] = {}

    @property
    def is_running(self) -> bool:
        """Whether the worker loop is currently active."""
        return self._running

    @property
    def worktree_leases(self) -> dict[str, WorktreeLease]:
        """Snapshot of all worktree leases across repositories."""
        combined: dict[str, WorktreeLease] = {}
        for manager in self._worktree_managers.values():
            combined.update(manager.leases)
        return combined

    def get_worktree_manager(self, repo_path: str | Path) -> WorktreeManager | None:
        """Return the active :class:`WorktreeManager` for *repo_path*, if any."""
        resolved = Path(repo_path).resolve()
        manager = self._worktree_managers.get(resolved)
        if manager is not None:
            return manager
        for item in self._worktree_managers.values():
            if item.repo_root == resolved:
                return item
        return None

    def _get_or_create_manager(self, repo_path: str | Path) -> WorktreeManager:
        existing = self.get_worktree_manager(repo_path)
        if existing is not None:
            return existing
        manager = WorktreeManager(
            repo_path,
            worktrees_root=self._worktree_root,
            branch_prefix=self._worktree_branch_prefix,
        )
        self._worktree_managers[manager.repo_root] = manager
        return manager

    async def start(self) -> None:
        """Start the worker loop as a background task."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("worker_started", max_concurrent_tasks=self._max_concurrent_tasks)

    async def stop(self) -> None:
        """Gracefully stop the worker loop and cancel active background tasks."""
        if not self._running:
            return
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._in_flight:
            pending = list(self._in_flight)
            for bg in pending:
                bg.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            self._in_flight.clear()
        logger.info("worker_stopped")

    async def _run_loop(self) -> None:
        """Main polling loop."""
        if self._max_concurrent_tasks == 1:
            while self._running:
                task = await self._queue.dequeue()
                if task is None:
                    await asyncio.sleep(self._poll_interval)
                    continue
                await self._process_task(task)
            return

        while self._running:
            await self._semaphore.acquire()
            if not self._running:
                self._semaphore.release()
                break
            task = await self._queue.dequeue()
            if task is None:
                self._semaphore.release()
                await asyncio.sleep(self._poll_interval)
                continue
            bg = asyncio.create_task(self._run_bounded_task(task))
            self._in_flight.add(bg)
            bg.add_done_callback(self._in_flight.discard)

    async def _run_bounded_task(self, task: Task) -> None:
        try:
            await self._process_task(task)
        finally:
            self._semaphore.release()

    async def _publish_worktree_event(
        self,
        event_type: EventType,
        run_id: str,
        lease: WorktreeLease,
    ) -> None:
        await self._event_bus.publish(
            Event(
                type=event_type,
                run_id=run_id,
                timestamp=datetime.now(UTC),
                data={
                    "branch": lease.branch,
                    "worktree_path": str(lease.worktree_path),
                    "state": lease.state.value,
                    "has_changes": lease.has_changes,
                    "commit_sha": lease.commit_sha,
                },
            )
        )

    async def _execute_isolated_task(self, task: Task) -> Any:
        manager = self._get_or_create_manager(task.repo_path)
        lease_ref: WorktreeLease | None = None
        try:
            async with manager.lease(
                task.id,
                keep_changed_worktree=self._keep_changed_worktrees,
                auto_remove_unchanged=self._auto_remove_unchanged_worktrees,
            ) as lease:
                lease_ref = lease
                await self._publish_worktree_event(EventType.WORKTREE_CREATED, task.id, lease)
                isolated_task = replace(task, repo_path=str(lease.worktree_path))
                result = await self._task_runner(isolated_task)
                if isinstance(result, AgentRun) and result.state != RunState.COMPLETED:
                    await manager.finalize(task.id, abort=True)
                return result
        finally:
            current = asyncio.current_task()
            if lease_ref is not None and not (current is not None and current.cancelling() > 0):
                await self._publish_worktree_event(EventType.WORKTREE_CLEANED, task.id, lease_ref)

    async def _process_task(self, task: Task) -> None:
        """Execute a single task and publish lifecycle events."""
        await self._event_bus.publish(
            Event(
                type=EventType.RUN_STARTED,
                run_id=task.id,
                timestamp=datetime.now(UTC),
                data={"task_description": task.task_description},
            )
        )

        try:
            result = (
                await self._execute_isolated_task(task)
                if self._worktree_isolation
                else await self._task_runner(task)
            )
        except Exception as exc:
            await self._finish_task(task, TaskStatus.FAILED, str(exc))
            logger.exception("task_failed", task_id=task.id, exc_info=exc)
            return

        status = TaskStatus.COMPLETED
        error = None
        if isinstance(result, AgentRun) and result.state != RunState.COMPLETED:
            status = {
                RunState.TIMEOUT: TaskStatus.TIMEOUT,
                RunState.CANCELLED: TaskStatus.CANCELLED,
            }.get(result.state, TaskStatus.FAILED)
            error = result.error or f"Agent run ended with state {result.state.value}"
        await self._finish_task(task, status, error)

    async def _finish_task(self, task: Task, status: TaskStatus, error: str | None) -> None:
        await self._queue.finish(task, status)
        await self._event_bus.publish(
            Event(
                type=EventType.RUN_COMPLETED
                if status == TaskStatus.COMPLETED
                else EventType.RUN_FAILED,
                run_id=task.id,
                timestamp=datetime.now(UTC),
                data={"state": status.value, "error": error} if error else {},
            )
        )
        logger.info("task_finished", task_id=task.id, status=status.value)
