"""Redis-backed task queue for concurrent agent runs.

Uses ``redis.asyncio`` with sorted sets for priority ordering and
hashes for task metadata storage.  Implements the :class:`TaskQueue`
ABC from spec § 4.5.

Requires the ``redis`` optional dependency group::

    pip install agent-forge[redis]
"""

from __future__ import annotations

import json
from typing import Any

from agent_forge.observability import get_logger
from agent_forge.orchestration.queue import Task, TaskQueue, TaskStatus

logger = get_logger("redis_queue")

# Redis key prefixes
_QUEUE_KEY = "agent_forge:queue"  # Sorted set: score = -priority
_TASK_PREFIX = "agent_forge:task:"  # Hash per task
_CONCURRENT_KEY = "agent_forge:active"  # Counter of active runs


class RedisQueue(TaskQueue):
    """Redis-backed priority task queue.

    Tasks are stored in a sorted set keyed by *negative* priority
    (``ZPOPMIN`` pops the lowest score, i.e. the highest priority).
    Task metadata lives in individual hashes so status lookups are O(1).

    Parameters
    ----------
    redis_url:
        Redis connection URL (default ``redis://localhost:6379/0``).
    max_concurrent_runs:
        Maximum number of tasks that can be processed at once.
        ``0`` means unlimited.
    key_prefix:
        Optional prefix for all Redis keys (useful for test isolation).
    """

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379/0",
        *,
        max_concurrent_runs: int = 0,
        key_prefix: str = "",
    ) -> None:
        try:
            import redis.asyncio as aioredis
        except ImportError as exc:
            msg = (
                "Redis support requires the 'redis' optional dependency. "
                "Install with: pip install agent-forge[redis]"
            )
            raise ImportError(msg) from exc

        self._redis: Any = aioredis.from_url(
            redis_url,
            decode_responses=True,
        )
        self._max_concurrent = max_concurrent_runs
        self._prefix = key_prefix
        self._queue_key = f"{key_prefix}{_QUEUE_KEY}"
        self._concurrent_key = f"{key_prefix}{_CONCURRENT_KEY}"

    # ------------------------------------------------------------------
    # TaskQueue ABC
    # ------------------------------------------------------------------

    async def enqueue(self, task: Task) -> str:
        """Add a task to the queue."""
        task.status = TaskStatus.QUEUED
        task_key = self._task_key(task.id)

        # Store task metadata as a hash
        await self._redis.hset(task_key, mapping=self._task_to_dict(task))

        # Add to sorted set: score = -priority (ZPOPMIN gets lowest score)
        await self._redis.zadd(self._queue_key, {task.id: -task.priority})

        logger.info(
            "task_enqueued",
            task_id=task.id,
            priority=task.priority,
        )
        return task.id

    async def dequeue(self) -> Task | None:
        """Get the next highest-priority task, or ``None`` if empty.

        Respects ``max_concurrent_runs`` — returns ``None`` if the
        concurrency limit is reached even when tasks are queued.
        """
        # Concurrency check
        if self._max_concurrent > 0:
            active = await self._redis.get(self._concurrent_key)
            if active is not None and int(active) >= self._max_concurrent:
                return None

        # Pop lowest-score (= highest-priority) task
        result = await self._redis.zpopmin(self._queue_key, count=1)
        if not result:
            return None

        task_id: str = result[0][0]
        task_key = self._task_key(task_id)

        # Update status to PROCESSING
        await self._redis.hset(task_key, "status", TaskStatus.PROCESSING.value)

        # Increment active counter
        await self._redis.incr(self._concurrent_key)

        task = await self._load_task(task_id)
        if task is not None:
            logger.info("task_dequeued", task_id=task.id)
        return task

    async def get_status(self, task_id: str) -> TaskStatus:
        """Look up the current status of a task."""
        task_key = self._task_key(task_id)
        status_val = await self._redis.hget(task_key, "status")
        if status_val is None:
            msg = f"Unknown task ID: {task_id}"
            raise KeyError(msg)
        return TaskStatus(status_val)

    async def cancel(self, task_id: str) -> bool:
        """Cancel a queued task.

        Only tasks with ``QUEUED`` status can be cancelled.
        """
        task_key = self._task_key(task_id)
        status_val = await self._redis.hget(task_key, "status")
        if status_val is None:
            msg = f"Unknown task ID: {task_id}"
            raise KeyError(msg)

        if status_val != TaskStatus.QUEUED.value:
            return False

        # Remove from sorted set and mark as FAILED
        await self._redis.zrem(self._queue_key, task_id)
        await self._redis.hset(task_key, "status", TaskStatus.FAILED.value)
        logger.info("task_cancelled", task_id=task_id)
        return True

    # ------------------------------------------------------------------
    # Extended API
    # ------------------------------------------------------------------

    async def complete(self, task_id: str) -> None:
        """Mark a task as completed and decrement the active counter."""
        task_key = self._task_key(task_id)
        await self._redis.hset(task_key, "status", TaskStatus.COMPLETED.value)
        await self._redis.decr(self._concurrent_key)
        logger.info("task_completed", task_id=task_id)

    async def fail(self, task_id: str) -> None:
        """Mark a task as failed and decrement the active counter."""
        task_key = self._task_key(task_id)
        await self._redis.hset(task_key, "status", TaskStatus.FAILED.value)
        await self._redis.decr(self._concurrent_key)
        logger.info("task_failed", task_id=task_id)

    async def size(self) -> int:
        """Number of tasks currently in the queue."""
        result: int = await self._redis.zcard(self._queue_key)
        return result

    async def close(self) -> None:
        """Close the Redis connection."""
        await self._redis.aclose()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _task_key(self, task_id: str) -> str:
        return f"{self._prefix}{_TASK_PREFIX}{task_id}"

    @staticmethod
    def _task_to_dict(task: Task) -> dict[str, str]:
        """Serialize a Task to a flat dict for Redis HSET."""
        return {
            "id": task.id,
            "task_description": task.task_description,
            "repo_path": task.repo_path,
            "config": json.dumps(
                task.config if isinstance(task.config, dict) else {},
            ),
            "priority": str(task.priority),
            "created_at": task.created_at.isoformat(),
            "status": task.status.value,
        }

    async def _load_task(self, task_id: str) -> Task | None:
        """Reconstruct a Task from its Redis hash."""
        from datetime import datetime

        task_key = self._task_key(task_id)
        data: dict[str, Any] = await self._redis.hgetall(task_key)
        if not data:
            return None

        return Task(
            id=data["id"],
            task_description=data["task_description"],
            repo_path=data["repo_path"],
            config=json.loads(data.get("config", "{}")),
            priority=int(data.get("priority", "0")),
            created_at=datetime.fromisoformat(data["created_at"]),
            status=TaskStatus(data["status"]),
        )
