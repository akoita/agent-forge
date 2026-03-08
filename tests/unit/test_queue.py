"""Unit tests for the InMemoryQueue task queue."""

from __future__ import annotations

import pytest

from agent_forge.orchestration.queue import (
    InMemoryQueue,
    Task,
    TaskStatus,
)


def _make_task(
    *,
    priority: int = 0,
    task_id: str = "t1",
    desc: str = "test task",
) -> Task:
    """Create a Task with sensible defaults."""
    return Task(
        id=task_id,
        task_description=desc,
        repo_path="/tmp/repo",
        config=None,
        priority=priority,
    )


class TestInMemoryQueue:
    """Tests for InMemoryQueue operations."""

    @pytest.mark.asyncio
    async def test_enqueue_and_dequeue(self) -> None:
        q = InMemoryQueue()
        task = _make_task()
        task_id = await q.enqueue(task)

        assert task_id == "t1"
        assert q.size == 1

        result = await q.dequeue()

        assert result is not None
        assert result.id == "t1"
        assert result.status == TaskStatus.PROCESSING

    @pytest.mark.asyncio
    async def test_dequeue_empty(self) -> None:
        q = InMemoryQueue()
        result = await q.dequeue()
        assert result is None

    @pytest.mark.asyncio
    async def test_priority_ordering(self) -> None:
        """Higher-priority tasks should be dequeued first."""
        q = InMemoryQueue()
        await q.enqueue(_make_task(task_id="low", priority=1))
        await q.enqueue(_make_task(task_id="high", priority=10))
        await q.enqueue(_make_task(task_id="mid", priority=5))

        first = await q.dequeue()
        second = await q.dequeue()
        third = await q.dequeue()

        assert first is not None and first.id == "high"
        assert second is not None and second.id == "mid"
        assert third is not None and third.id == "low"

    @pytest.mark.asyncio
    async def test_get_status(self) -> None:
        q = InMemoryQueue()
        task = _make_task()
        await q.enqueue(task)

        status = await q.get_status("t1")
        assert status == TaskStatus.QUEUED

        await q.dequeue()
        status = await q.get_status("t1")
        assert status == TaskStatus.PROCESSING

    @pytest.mark.asyncio
    async def test_get_status_unknown(self) -> None:
        q = InMemoryQueue()
        with pytest.raises(KeyError, match="Unknown task ID"):
            await q.get_status("nonexistent")

    @pytest.mark.asyncio
    async def test_cancel_queued_task(self) -> None:
        q = InMemoryQueue()
        task = _make_task()
        await q.enqueue(task)

        cancelled = await q.cancel("t1")
        assert cancelled is True

        status = await q.get_status("t1")
        assert status == TaskStatus.FAILED

    @pytest.mark.asyncio
    async def test_cancel_processing_task(self) -> None:
        """Cannot cancel a task that is already processing."""
        q = InMemoryQueue()
        await q.enqueue(_make_task())
        await q.dequeue()  # moves to PROCESSING

        cancelled = await q.cancel("t1")
        assert cancelled is False

    @pytest.mark.asyncio
    async def test_cancel_unknown_task(self) -> None:
        q = InMemoryQueue()
        with pytest.raises(KeyError, match="Unknown task ID"):
            await q.cancel("nonexistent")

    @pytest.mark.asyncio
    async def test_dequeue_skips_cancelled(self) -> None:
        """Cancelled tasks in the queue should be skipped."""
        q = InMemoryQueue()
        await q.enqueue(_make_task(task_id="cancelled"))
        await q.enqueue(_make_task(task_id="valid"))

        await q.cancel("cancelled")

        result = await q.dequeue()
        assert result is not None
        assert result.id == "valid"

    @pytest.mark.asyncio
    async def test_size_property(self) -> None:
        q = InMemoryQueue()
        assert q.size == 0

        await q.enqueue(_make_task(task_id="a"))
        await q.enqueue(_make_task(task_id="b"))
        assert q.size == 2

    @pytest.mark.asyncio
    async def test_enqueue_assigns_id_if_empty(self) -> None:
        q = InMemoryQueue()
        task = Task(
            id="",
            task_description="auto-id",
            repo_path="/tmp",
            config=None,
        )
        task_id = await q.enqueue(task)

        assert task_id != ""
        assert len(task_id) > 10  # UUID
