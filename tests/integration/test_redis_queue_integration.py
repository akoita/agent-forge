"""Integration tests for RedisQueue against a real Redis instance.

These tests require a running Redis server on localhost:6379.
They are automatically skipped if Redis is not available.

Run with: docker compose up redis -d && pytest tests/integration/ -v
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agent_forge.orchestration.queue import Task, TaskStatus

# Skip entire module if redis is not installed or server is unreachable
try:
    import redis.asyncio as aioredis

    _r = aioredis.from_url("redis://localhost:6379/0")
except ImportError:
    pytest.skip("redis package not installed", allow_module_level=True)


async def _redis_available() -> bool:
    """Check if a Redis server is reachable."""
    try:
        r = aioredis.from_url("redis://localhost:6379/0")
        await r.ping()
        await r.aclose()
    except Exception:  # noqa: BLE001
        return False
    else:
        return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PREFIX = "test_integration:"


def _make_task(
    *,
    priority: int = 0,
    task_id: str | None = None,
) -> Task:
    import uuid

    return Task(
        id=task_id or str(uuid.uuid4()),
        task_description="Integration test task",
        repo_path="/tmp/test-repo",
        config={},
        priority=priority,
        created_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redis_available() -> None:
    """Prerequisite — skip remaining tests if Redis is down."""
    if not await _redis_available():
        pytest.skip("Redis server not available at localhost:6379")


@pytest.mark.asyncio
async def test_full_lifecycle() -> None:
    """Enqueue → dequeue → complete round-trip."""
    if not await _redis_available():
        pytest.skip("Redis server not available")

    from agent_forge.orchestration.redis_queue import RedisQueue

    q = RedisQueue(key_prefix=_PREFIX)
    try:
        task = _make_task()
        task_id = await q.enqueue(task)
        assert await q.get_status(task_id) == TaskStatus.QUEUED

        dequeued = await q.dequeue()
        assert dequeued is not None
        assert dequeued.id == task_id
        assert await q.get_status(task_id) == TaskStatus.PROCESSING

        await q.complete(task_id)
        assert await q.get_status(task_id) == TaskStatus.COMPLETED
    finally:
        # Cleanup
        await q._redis.flushdb()
        await q.close()


@pytest.mark.asyncio
async def test_priority_ordering() -> None:
    """Higher priority tasks dequeue first."""
    if not await _redis_available():
        pytest.skip("Redis server not available")

    from agent_forge.orchestration.redis_queue import RedisQueue

    q = RedisQueue(key_prefix=_PREFIX)
    try:
        low = _make_task(priority=1, task_id="low")
        high = _make_task(priority=10, task_id="high")
        mid = _make_task(priority=5, task_id="mid")

        await q.enqueue(low)
        await q.enqueue(high)
        await q.enqueue(mid)

        first = await q.dequeue()
        second = await q.dequeue()
        third = await q.dequeue()

        assert first is not None and first.id == "high"
        assert second is not None and second.id == "mid"
        assert third is not None and third.id == "low"
    finally:
        await q._redis.flushdb()
        await q.close()


@pytest.mark.asyncio
async def test_cancel() -> None:
    """Cancelled tasks are removed from the queue."""
    if not await _redis_available():
        pytest.skip("Redis server not available")

    from agent_forge.orchestration.redis_queue import RedisQueue

    q = RedisQueue(key_prefix=_PREFIX)
    try:
        task = _make_task()
        task_id = await q.enqueue(task)

        assert await q.cancel(task_id) is True
        assert await q.get_status(task_id) == TaskStatus.FAILED

        # Queue should now be empty
        dequeued = await q.dequeue()
        assert dequeued is None
    finally:
        await q._redis.flushdb()
        await q.close()


@pytest.mark.asyncio
async def test_max_concurrent_runs() -> None:
    """Dequeue respects max_concurrent_runs limit."""
    if not await _redis_available():
        pytest.skip("Redis server not available")

    from agent_forge.orchestration.redis_queue import RedisQueue

    q = RedisQueue(key_prefix=_PREFIX, max_concurrent_runs=1)
    try:
        t1 = _make_task(task_id="t1")
        t2 = _make_task(task_id="t2")

        await q.enqueue(t1)
        await q.enqueue(t2)

        # First dequeue should work
        first = await q.dequeue()
        assert first is not None

        # Second should be blocked by concurrency limit
        second = await q.dequeue()
        assert second is None

        # After completing first, second should work
        await q.complete(first.id)
        second = await q.dequeue()
        assert second is not None
    finally:
        await q._redis.flushdb()
        await q.close()
