"""Unit tests for the EventBus pub/sub system."""

from __future__ import annotations

import pytest

from agent_forge.orchestration.events import Event, EventBus, EventType


class TestEventBus:
    """Tests for EventBus publish/subscribe."""

    @pytest.mark.asyncio
    async def test_subscribe_and_publish(self) -> None:
        bus = EventBus()
        received: list[Event] = []

        async def handler(event: Event) -> None:
            received.append(event)

        await bus.subscribe(EventType.RUN_STARTED, handler)

        event = Event(
            type=EventType.RUN_STARTED,
            run_id="run-1",
        )
        await bus.publish(event)

        assert len(received) == 1
        assert received[0].run_id == "run-1"
        assert received[0].type == EventType.RUN_STARTED

    @pytest.mark.asyncio
    async def test_multiple_handlers(self) -> None:
        bus = EventBus()
        calls: list[str] = []

        async def handler_a(event: Event) -> None:
            calls.append("a")

        async def handler_b(event: Event) -> None:
            calls.append("b")

        await bus.subscribe(EventType.RUN_COMPLETED, handler_a)
        await bus.subscribe(EventType.RUN_COMPLETED, handler_b)

        await bus.publish(
            Event(type=EventType.RUN_COMPLETED, run_id="run-1")
        )

        assert calls == ["a", "b"]

    @pytest.mark.asyncio
    async def test_unsubscribe(self) -> None:
        bus = EventBus()
        received: list[Event] = []

        async def handler(event: Event) -> None:
            received.append(event)

        sub_id = await bus.subscribe(EventType.RUN_FAILED, handler)
        await bus.unsubscribe(sub_id)

        await bus.publish(
            Event(type=EventType.RUN_FAILED, run_id="run-1")
        )

        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_publish_no_handlers(self) -> None:
        """Publishing to an event with no subscribers should not raise."""
        bus = EventBus()
        await bus.publish(
            Event(type=EventType.TOOL_CALLED, run_id="run-1")
        )

    @pytest.mark.asyncio
    async def test_handler_exception_does_not_propagate(self) -> None:
        """A failing handler should not block other handlers."""
        bus = EventBus()
        calls: list[str] = []

        async def bad_handler(event: Event) -> None:
            msg = "boom"
            raise RuntimeError(msg)

        async def good_handler(event: Event) -> None:
            calls.append("ok")

        await bus.subscribe(EventType.TOKEN_USAGE, bad_handler)
        await bus.subscribe(EventType.TOKEN_USAGE, good_handler)

        await bus.publish(
            Event(type=EventType.TOKEN_USAGE, run_id="run-1")
        )

        assert calls == ["ok"]

    @pytest.mark.asyncio
    async def test_event_types_isolated(self) -> None:
        """Handlers only fire for their subscribed event type."""
        bus = EventBus()
        received: list[Event] = []

        async def handler(event: Event) -> None:
            received.append(event)

        await bus.subscribe(EventType.ITERATION_STARTED, handler)

        # Publish a different event type
        await bus.publish(
            Event(type=EventType.ITERATION_COMPLETED, run_id="run-1")
        )

        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_unsubscribe_nonexistent(self) -> None:
        """Unsubscribing a bogus ID should not raise."""
        bus = EventBus()
        await bus.unsubscribe("nonexistent-id")

    @pytest.mark.asyncio
    async def test_event_data(self) -> None:
        bus = EventBus()
        received: list[Event] = []

        async def handler(event: Event) -> None:
            received.append(event)

        await bus.subscribe(EventType.TOOL_COMPLETED, handler)

        await bus.publish(
            Event(
                type=EventType.TOOL_COMPLETED,
                run_id="run-1",
                data={"tool_name": "read_file", "duration_ms": 42},
            )
        )

        assert received[0].data["tool_name"] == "read_file"
        assert received[0].data["duration_ms"] == 42

    @pytest.mark.asyncio
    async def test_subscribe_returns_unique_ids(self) -> None:
        bus = EventBus()

        async def handler(event: Event) -> None:
            pass

        id1 = await bus.subscribe(EventType.RUN_STARTED, handler)
        id2 = await bus.subscribe(EventType.RUN_STARTED, handler)

        assert id1 != id2
