"""Unit tests for the Worker."""

from __future__ import annotations

import asyncio

import pytest

from agent_forge.orchestration.events import Event, EventBus, EventType
from agent_forge.orchestration.queue import InMemoryQueue, Task, TaskStatus
from agent_forge.orchestration.worker import Worker


def _make_task(task_id: str = "t1", desc: str = "test") -> Task:
    return Task(
        id=task_id,
        task_description=desc,
        repo_path="/tmp/repo",
        config=None,
    )


class TestWorker:
    """Tests for Worker lifecycle and task processing."""

    @pytest.mark.asyncio
    async def test_start_stop(self) -> None:
        q = InMemoryQueue()
        bus = EventBus()

        async def runner(task: Task) -> None:
            pass

        worker = Worker(q, bus, runner, poll_interval=0.01)
        assert not worker.is_running

        await worker.start()
        assert worker.is_running

        await worker.stop()
        assert not worker.is_running

    @pytest.mark.asyncio
    async def test_processes_task(self) -> None:
        q = InMemoryQueue()
        bus = EventBus()
        processed: list[str] = []

        async def runner(task: Task) -> None:
            processed.append(task.id)

        worker = Worker(q, bus, runner, poll_interval=0.01)

        await q.enqueue(_make_task("task-1"))
        await worker.start()
        await asyncio.sleep(0.1)
        await worker.stop()

        assert "task-1" in processed

    @pytest.mark.asyncio
    async def test_publishes_run_started_and_completed(self) -> None:
        q = InMemoryQueue()
        bus = EventBus()
        events: list[Event] = []

        async def handler(event: Event) -> None:
            events.append(event)

        async def runner(task: Task) -> None:
            pass

        await bus.subscribe(EventType.RUN_STARTED, handler)
        await bus.subscribe(EventType.RUN_COMPLETED, handler)

        worker = Worker(q, bus, runner, poll_interval=0.01)
        await q.enqueue(_make_task("task-1"))
        await worker.start()
        await asyncio.sleep(0.1)
        await worker.stop()

        event_types = [e.type for e in events]
        assert EventType.RUN_STARTED in event_types
        assert EventType.RUN_COMPLETED in event_types

    @pytest.mark.asyncio
    async def test_publishes_run_failed_on_error(self) -> None:
        q = InMemoryQueue()
        bus = EventBus()
        events: list[Event] = []

        async def handler(event: Event) -> None:
            events.append(event)

        async def failing_runner(task: Task) -> None:
            msg = "deliberately broken"
            raise RuntimeError(msg)

        await bus.subscribe(EventType.RUN_STARTED, handler)
        await bus.subscribe(EventType.RUN_FAILED, handler)

        worker = Worker(q, bus, failing_runner, poll_interval=0.01)
        await q.enqueue(_make_task("task-1"))
        await worker.start()
        await asyncio.sleep(0.1)
        await worker.stop()

        event_types = [e.type for e in events]
        assert EventType.RUN_STARTED in event_types
        assert EventType.RUN_FAILED in event_types

        failed_event = [
            e for e in events if e.type == EventType.RUN_FAILED
        ][0]
        assert "deliberately broken" in failed_event.data["error"]

    @pytest.mark.asyncio
    async def test_task_status_updated_on_success(self) -> None:
        q = InMemoryQueue()
        bus = EventBus()

        async def runner(task: Task) -> None:
            pass

        worker = Worker(q, bus, runner, poll_interval=0.01)
        await q.enqueue(_make_task("task-1"))
        await worker.start()
        await asyncio.sleep(0.1)
        await worker.stop()

        status = await q.get_status("task-1")
        assert status == TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_task_status_updated_on_failure(self) -> None:
        q = InMemoryQueue()
        bus = EventBus()

        async def failing_runner(task: Task) -> None:
            msg = "fail"
            raise RuntimeError(msg)

        worker = Worker(q, bus, failing_runner, poll_interval=0.01)
        await q.enqueue(_make_task("task-1"))
        await worker.start()
        await asyncio.sleep(0.1)
        await worker.stop()

        status = await q.get_status("task-1")
        assert status == TaskStatus.FAILED

    @pytest.mark.asyncio
    async def test_start_idempotent(self) -> None:
        q = InMemoryQueue()
        bus = EventBus()

        async def runner(task: Task) -> None:
            pass

        worker = Worker(q, bus, runner, poll_interval=0.01)
        await worker.start()
        await worker.start()  # should not raise or create a second task
        assert worker.is_running
        await worker.stop()

    @pytest.mark.asyncio
    async def test_stop_idempotent(self) -> None:
        q = InMemoryQueue()
        bus = EventBus()

        async def runner(task: Task) -> None:
            pass

        worker = Worker(q, bus, runner, poll_interval=0.01)
        await worker.stop()  # should not raise when not started
        assert not worker.is_running
