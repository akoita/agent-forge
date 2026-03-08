"""Unit tests for the Redis-backed task queue (mocked Redis)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from agent_forge.orchestration.queue import Task, TaskStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(
    *,
    priority: int = 0,
    task_id: str = "test-task-1",
) -> Task:
    return Task(
        id=task_id,
        task_description="Fix the login bug",
        repo_path="/tmp/test-repo",
        config={},
        priority=priority,
        created_at=datetime.now(UTC),
    )


def _mock_redis() -> AsyncMock:
    """Create a mock async Redis client with required methods."""
    r = AsyncMock()
    r.hset = AsyncMock()
    r.hget = AsyncMock()
    r.hgetall = AsyncMock(return_value={})
    r.zadd = AsyncMock()
    r.zpopmin = AsyncMock(return_value=[])
    r.zrem = AsyncMock()
    r.zcard = AsyncMock(return_value=0)
    r.get = AsyncMock(return_value=None)
    r.incr = AsyncMock()
    r.decr = AsyncMock()
    r.aclose = AsyncMock()
    return r


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRedisQueueEnqueue:
    """Enqueue stores task in hash and sorted set."""

    @pytest.mark.asyncio
    async def test_enqueue_stores_task(self) -> None:
        mock_redis = _mock_redis()
        with patch(
            "agent_forge.orchestration.redis_queue.RedisQueue.__init__",
            return_value=None,
        ):
            from agent_forge.orchestration.redis_queue import RedisQueue

            q = RedisQueue.__new__(RedisQueue)
            q._redis = mock_redis
            q._prefix = ""
            q._queue_key = "agent_forge:queue"
            q._concurrent_key = "agent_forge:active"
            q._max_concurrent = 0

        task = _make_task()
        result = await q.enqueue(task)

        assert result == "test-task-1"
        assert task.status == TaskStatus.QUEUED
        mock_redis.hset.assert_called_once()
        mock_redis.zadd.assert_called_once_with(
            "agent_forge:queue", {"test-task-1": 0},
        )

    @pytest.mark.asyncio
    async def test_enqueue_negative_score_for_priority(self) -> None:
        mock_redis = _mock_redis()
        with patch(
            "agent_forge.orchestration.redis_queue.RedisQueue.__init__",
            return_value=None,
        ):
            from agent_forge.orchestration.redis_queue import RedisQueue

            q = RedisQueue.__new__(RedisQueue)
            q._redis = mock_redis
            q._prefix = ""
            q._queue_key = "agent_forge:queue"
            q._concurrent_key = "agent_forge:active"
            q._max_concurrent = 0

        task = _make_task(priority=5)
        await q.enqueue(task)

        mock_redis.zadd.assert_called_once_with(
            "agent_forge:queue", {"test-task-1": -5},
        )


class TestRedisQueueDequeue:
    """Dequeue pops from sorted set and updates status."""

    @pytest.mark.asyncio
    async def test_dequeue_empty_returns_none(self) -> None:
        mock_redis = _mock_redis()
        mock_redis.zpopmin.return_value = []
        with patch(
            "agent_forge.orchestration.redis_queue.RedisQueue.__init__",
            return_value=None,
        ):
            from agent_forge.orchestration.redis_queue import RedisQueue

            q = RedisQueue.__new__(RedisQueue)
            q._redis = mock_redis
            q._prefix = ""
            q._queue_key = "agent_forge:queue"
            q._concurrent_key = "agent_forge:active"
            q._max_concurrent = 0

        result = await q.dequeue()
        assert result is None

    @pytest.mark.asyncio
    async def test_dequeue_returns_task(self) -> None:
        mock_redis = _mock_redis()
        now = datetime.now(UTC)
        mock_redis.zpopmin.return_value = [("task-1", -5.0)]
        mock_redis.hgetall.return_value = {
            "id": "task-1",
            "task_description": "Fix bug",
            "repo_path": "/tmp/repo",
            "config": "{}",
            "priority": "5",
            "created_at": now.isoformat(),
            "status": "processing",
        }
        with patch(
            "agent_forge.orchestration.redis_queue.RedisQueue.__init__",
            return_value=None,
        ):
            from agent_forge.orchestration.redis_queue import RedisQueue

            q = RedisQueue.__new__(RedisQueue)
            q._redis = mock_redis
            q._prefix = ""
            q._queue_key = "agent_forge:queue"
            q._concurrent_key = "agent_forge:active"
            q._max_concurrent = 0

        result = await q.dequeue()

        assert result is not None
        assert result.id == "task-1"
        assert result.status == TaskStatus.PROCESSING
        mock_redis.incr.assert_called_once()

    @pytest.mark.asyncio
    async def test_dequeue_respects_concurrency_limit(self) -> None:
        mock_redis = _mock_redis()
        mock_redis.get.return_value = "3"
        with patch(
            "agent_forge.orchestration.redis_queue.RedisQueue.__init__",
            return_value=None,
        ):
            from agent_forge.orchestration.redis_queue import RedisQueue

            q = RedisQueue.__new__(RedisQueue)
            q._redis = mock_redis
            q._prefix = ""
            q._queue_key = "agent_forge:queue"
            q._concurrent_key = "agent_forge:active"
            q._max_concurrent = 3

        result = await q.dequeue()
        assert result is None
        mock_redis.zpopmin.assert_not_called()


class TestRedisQueueStatus:
    """Status lookup from Redis hash."""

    @pytest.mark.asyncio
    async def test_get_status_returns_status(self) -> None:
        mock_redis = _mock_redis()
        mock_redis.hget.return_value = "queued"
        with patch(
            "agent_forge.orchestration.redis_queue.RedisQueue.__init__",
            return_value=None,
        ):
            from agent_forge.orchestration.redis_queue import RedisQueue

            q = RedisQueue.__new__(RedisQueue)
            q._redis = mock_redis
            q._prefix = ""
            q._queue_key = "agent_forge:queue"
            q._concurrent_key = "agent_forge:active"
            q._max_concurrent = 0

        status = await q.get_status("task-1")
        assert status == TaskStatus.QUEUED

    @pytest.mark.asyncio
    async def test_get_status_unknown_raises(self) -> None:
        mock_redis = _mock_redis()
        mock_redis.hget.return_value = None
        with patch(
            "agent_forge.orchestration.redis_queue.RedisQueue.__init__",
            return_value=None,
        ):
            from agent_forge.orchestration.redis_queue import RedisQueue

            q = RedisQueue.__new__(RedisQueue)
            q._redis = mock_redis
            q._prefix = ""
            q._queue_key = "agent_forge:queue"
            q._concurrent_key = "agent_forge:active"
            q._max_concurrent = 0

        with pytest.raises(KeyError, match="Unknown task ID"):
            await q.get_status("nonexistent")


class TestRedisQueueCancel:
    """Cancel removes from sorted set and marks FAILED."""

    @pytest.mark.asyncio
    async def test_cancel_queued_task(self) -> None:
        mock_redis = _mock_redis()
        mock_redis.hget.return_value = "queued"
        with patch(
            "agent_forge.orchestration.redis_queue.RedisQueue.__init__",
            return_value=None,
        ):
            from agent_forge.orchestration.redis_queue import RedisQueue

            q = RedisQueue.__new__(RedisQueue)
            q._redis = mock_redis
            q._prefix = ""
            q._queue_key = "agent_forge:queue"
            q._concurrent_key = "agent_forge:active"
            q._max_concurrent = 0

        result = await q.cancel("task-1")

        assert result is True
        mock_redis.zrem.assert_called_once()
        mock_redis.hset.assert_called_once()

    @pytest.mark.asyncio
    async def test_cancel_processing_task_returns_false(self) -> None:
        mock_redis = _mock_redis()
        mock_redis.hget.return_value = "processing"
        with patch(
            "agent_forge.orchestration.redis_queue.RedisQueue.__init__",
            return_value=None,
        ):
            from agent_forge.orchestration.redis_queue import RedisQueue

            q = RedisQueue.__new__(RedisQueue)
            q._redis = mock_redis
            q._prefix = ""
            q._queue_key = "agent_forge:queue"
            q._concurrent_key = "agent_forge:active"
            q._max_concurrent = 0

        result = await q.cancel("task-1")
        assert result is False


class TestRedisQueueLifecycle:
    """Complete/fail decrement the active counter."""

    @pytest.mark.asyncio
    async def test_complete_decrements_counter(self) -> None:
        mock_redis = _mock_redis()
        with patch(
            "agent_forge.orchestration.redis_queue.RedisQueue.__init__",
            return_value=None,
        ):
            from agent_forge.orchestration.redis_queue import RedisQueue

            q = RedisQueue.__new__(RedisQueue)
            q._redis = mock_redis
            q._prefix = ""
            q._queue_key = "agent_forge:queue"
            q._concurrent_key = "agent_forge:active"
            q._max_concurrent = 0

        await q.complete("task-1")
        mock_redis.decr.assert_called_once()

    @pytest.mark.asyncio
    async def test_fail_decrements_counter(self) -> None:
        mock_redis = _mock_redis()
        with patch(
            "agent_forge.orchestration.redis_queue.RedisQueue.__init__",
            return_value=None,
        ):
            from agent_forge.orchestration.redis_queue import RedisQueue

            q = RedisQueue.__new__(RedisQueue)
            q._redis = mock_redis
            q._prefix = ""
            q._queue_key = "agent_forge:queue"
            q._concurrent_key = "agent_forge:active"
            q._max_concurrent = 0

        await q.fail("task-1")
        mock_redis.decr.assert_called_once()

    @pytest.mark.asyncio
    async def test_close(self) -> None:
        mock_redis = _mock_redis()
        with patch(
            "agent_forge.orchestration.redis_queue.RedisQueue.__init__",
            return_value=None,
        ):
            from agent_forge.orchestration.redis_queue import RedisQueue

            q = RedisQueue.__new__(RedisQueue)
            q._redis = mock_redis
            q._prefix = ""
            q._queue_key = "agent_forge:queue"
            q._concurrent_key = "agent_forge:active"
            q._max_concurrent = 0

        await q.close()
        mock_redis.aclose.assert_called_once()


class TestRedisQueueImportError:
    """Missing redis dependency gives a helpful error."""

    def test_import_error_message(self) -> None:
        with patch.dict("sys.modules", {"redis": None, "redis.asyncio": None}):
            # Force reimport
            import importlib

            import agent_forge.orchestration.redis_queue as mod

            importlib.reload(mod)
            with pytest.raises(ImportError, match="redis"):
                mod.RedisQueue()
