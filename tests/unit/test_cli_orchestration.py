"""Tests for CLI orchestration wiring (issue #80).

Verifies that:
- Direct mode creates EventBus and passes it to react_loop
- Queue mode creates queue + worker, enqueues task, waits for completion
- --queue=redis creates RedisQueue
- _create_llm and _make_task_runner work correctly
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_forge.orchestration.events import EventBus
from agent_forge.orchestration.queue import Task, TaskStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_config() -> MagicMock:
    """Return a minimal config mock."""
    cfg = MagicMock()
    cfg.agent.default_model = "gemini-2.0-flash"
    cfg.agent.max_iterations = 10
    cfg.agent.max_tokens_per_run = 100_000
    cfg.agent.temperature = 0.0
    return cfg


# ---------------------------------------------------------------------------
# Direct mode
# ---------------------------------------------------------------------------

class TestDirectModeEventBus:
    """_run_agent should create EventBus and pass it to react_loop."""

    @pytest.mark.asyncio
    async def test_event_bus_passed_to_react_loop(self) -> None:
        """EventBus is created and passed as kwarg to react_loop."""
        from agent_forge.cli import _run_agent

        with (
            patch("agent_forge.cli._create_llm") as mock_llm_factory,
            patch("agent_forge.tools.create_default_registry") as mock_tools,
            patch("agent_forge.agent.core.react_loop", new_callable=AsyncMock) as mock_react,
            patch("agent_forge.sandbox.docker.DockerSandbox") as mock_sandbox_cls,
        ):
            mock_sandbox = AsyncMock()
            mock_sandbox_cls.return_value = mock_sandbox
            mock_llm = AsyncMock()
            mock_llm_factory.return_value = mock_llm
            mock_tools.return_value = MagicMock()

            # react_loop returns the run — Rich needs real strings
            mock_result = MagicMock()
            mock_result.state.value = "completed"
            mock_result.id = "run-123"
            mock_result.task = "test"
            mock_result.iterations = 1
            mock_result.total_tokens.total_tokens = 100
            mock_result.completed_at = None
            mock_result.error = None
            mock_react.return_value = mock_result

            await _run_agent("test task", "/tmp/repo", _fake_config(), "gemini", "key")

            # Verify react_loop was called with event_bus kwarg
            mock_react.assert_called_once()
            call_kwargs = mock_react.call_args
            assert "event_bus" in call_kwargs.kwargs
            assert isinstance(call_kwargs.kwargs["event_bus"], EventBus)


# ---------------------------------------------------------------------------
# Queue mode
# ---------------------------------------------------------------------------

class TestQueueModeWiring:
    """_run_agent_queued wires queue → worker → task_runner pipeline."""

    @pytest.mark.asyncio
    async def test_memory_queue_enqueue_and_complete(self) -> None:
        """Queue mode with memory backend enqueues task and waits."""
        from agent_forge.cli import _run_agent_queued

        with (
            patch("agent_forge.cli._make_task_runner") as mock_make_runner,
            patch("agent_forge.orchestration.worker.Worker.start", new_callable=AsyncMock),
            patch("agent_forge.orchestration.worker.Worker.stop", new_callable=AsyncMock),
            patch(
                "agent_forge.orchestration.queue.InMemoryQueue.get_status",
                new_callable=AsyncMock,
                return_value=TaskStatus.COMPLETED,
            ),
            patch(
                "agent_forge.orchestration.queue.InMemoryQueue.enqueue",
                new_callable=AsyncMock,
                return_value="task-abc",
            ),
        ):
            async def fake_runner(task: Task) -> None:
                pass

            mock_make_runner.return_value = fake_runner

            await _run_agent_queued(
                "fix bug", "/tmp/repo", _fake_config(), "gemini", "key",
                queue_backend="memory",
                redis_url="redis://localhost",
                max_concurrent_runs=0,
            )

            mock_make_runner.assert_called_once()

    @pytest.mark.asyncio
    async def test_redis_queue_selected(self) -> None:
        """--queue=redis creates RedisQueue."""
        from agent_forge.cli import _run_agent_queued

        with (
            patch("agent_forge.cli._make_task_runner") as mock_make_runner,
            patch("agent_forge.orchestration.redis_queue.RedisQueue") as mock_redis_cls,
            patch("agent_forge.orchestration.worker.Worker.start", new_callable=AsyncMock),
            patch("agent_forge.orchestration.worker.Worker.stop", new_callable=AsyncMock),
        ):
            # Mock RedisQueue instance
            mock_queue = AsyncMock()
            mock_queue.enqueue = AsyncMock(return_value="task-123")
            mock_queue.get_status = AsyncMock(return_value=TaskStatus.COMPLETED)
            mock_queue.close = AsyncMock()
            mock_redis_cls.return_value = mock_queue

            async def fake_runner(task: Task) -> None:
                pass

            mock_make_runner.return_value = fake_runner

            await _run_agent_queued(
                "fix bug", "/tmp/repo", _fake_config(), "gemini", "key",
                queue_backend="redis",
                redis_url="redis://custom:6380/1",
                max_concurrent_runs=5,
            )

            mock_redis_cls.assert_called_once_with(
                redis_url="redis://custom:6380/1",
                max_concurrent_runs=5,
            )
            mock_queue.close.assert_called_once()


# ---------------------------------------------------------------------------
# _create_llm
# ---------------------------------------------------------------------------

class TestCreateLLM:
    """_create_llm creates provider by name."""

    def test_gemini_provider(self) -> None:
        from agent_forge.cli import _create_llm

        with patch("agent_forge.llm.gemini.GeminiProvider") as mock_cls:
            mock_cls.return_value = MagicMock()
            result = _create_llm("gemini", "test-key")
            mock_cls.assert_called_once_with(api_key="test-key")
            assert result is mock_cls.return_value

    def test_unknown_provider_exits(self) -> None:
        from agent_forge.cli import _create_llm

        with pytest.raises(SystemExit):
            _create_llm("unknown", "key")


# ---------------------------------------------------------------------------
# _make_task_runner
# ---------------------------------------------------------------------------

class TestMakeTaskRunner:
    """_make_task_runner builds an async callable for Worker."""

    @pytest.mark.asyncio
    async def test_runner_calls_react_loop(self) -> None:
        from agent_forge.cli import _make_task_runner

        with (
            patch("agent_forge.cli._create_llm") as mock_llm_factory,
            patch("agent_forge.agent.core.react_loop", new_callable=AsyncMock) as mock_react,
            patch("agent_forge.sandbox.docker.DockerSandbox") as mock_sandbox_cls,
        ):
            mock_sandbox = AsyncMock()
            mock_sandbox_cls.return_value = mock_sandbox
            mock_llm = AsyncMock()
            mock_llm_factory.return_value = mock_llm

            event_bus = EventBus()
            runner = _make_task_runner(_fake_config(), "gemini", "key", event_bus)

            task = Task(
                id="t-1",
                task_description="fix login",
                repo_path="/tmp/repo",
                config=MagicMock(),
            )

            await runner(task)

            mock_react.assert_called_once()
            call_kwargs = mock_react.call_args
            assert call_kwargs.kwargs["event_bus"] is event_bus
            mock_sandbox.start.assert_called_once()
            mock_sandbox.stop.assert_called_once()
            mock_llm.close.assert_called_once()
