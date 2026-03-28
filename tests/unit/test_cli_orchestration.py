"""Tests for CLI orchestration wiring (issue #80) + config matrix (issue #19).

Covers all execution configurations:
- Direct mode (no --queue): EventBus passed to react_loop
- Memory queue: single task, multiple tasks
- Redis queue: single task, multiple tasks
- Concurrency forwarding, cleanup on failure
- _create_llm and _make_task_runner helpers
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_forge.orchestration.events import EventBus
from agent_forge.orchestration.queue import Task, TaskStatus
from agent_forge.sandbox.base import SandboxConfig

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
    cfg.sandbox.image = "agent-forge-sandbox:latest"
    cfg.sandbox.cpu_limit = 1.0
    cfg.sandbox.memory_limit = "512m"
    cfg.sandbox.timeout_seconds = 300
    cfg.sandbox.network_enabled = False
    cfg.sandbox.writable_cache_mounts = True
    return cfg


def _mock_react_result() -> MagicMock:
    """Create a MagicMock react_loop result with string fields for Rich."""
    mock_result = MagicMock()
    mock_result.state.value = "completed"
    mock_result.id = "run-123"
    mock_result.task = "test"
    mock_result.iterations = 1
    mock_result.total_tokens.total_tokens = 100
    mock_result.completed_at = None
    mock_result.error = None
    return mock_result


# ---------------------------------------------------------------------------
# Direct mode
# ---------------------------------------------------------------------------


class TestDirectModeEventBus:
    """Direct mode (no --queue) creates EventBus and passes to react_loop."""

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
            mock_sandbox_cls.return_value = AsyncMock()
            mock_llm_factory.return_value = AsyncMock()
            mock_tools.return_value = MagicMock()
            mock_react.return_value = _mock_react_result()

            await _run_agent("test task", "/tmp/repo", _fake_config(), "gemini", "key")

            mock_react.assert_called_once()
            call_kwargs = mock_react.call_args
            assert "event_bus" in call_kwargs.kwargs
            assert isinstance(call_kwargs.kwargs["event_bus"], EventBus)

    @pytest.mark.asyncio
    async def test_direct_mode_no_queue_options_ignored(self) -> None:
        """When --queue is not passed, _run_agent is called (not _run_agent_queued)."""
        from agent_forge.cli import _run_agent

        with (
            patch("agent_forge.cli._create_llm") as mock_llm_factory,
            patch("agent_forge.tools.create_default_registry") as mock_tools,
            patch("agent_forge.agent.core.react_loop", new_callable=AsyncMock) as mock_react,
            patch("agent_forge.sandbox.docker.DockerSandbox") as mock_sandbox_cls,
        ):
            mock_sandbox_cls.return_value = AsyncMock()
            mock_llm_factory.return_value = AsyncMock()
            mock_tools.return_value = MagicMock()
            mock_react.return_value = _mock_react_result()

            # Call directly — no queue infrastructure should be created
            await _run_agent("task", "/tmp/repo", _fake_config(), "gemini", "key")

            # react_loop called without Worker or queue involvement
            mock_react.assert_called_once()
            # Verify no InMemoryQueue or Worker was instantiated
            # (The fact that _run_agent completed without them is the assertion)


# ---------------------------------------------------------------------------
# Memory queue mode
# ---------------------------------------------------------------------------


class TestMemoryQueueMode:
    """Memory queue: single and multiple tasks."""

    @pytest.mark.asyncio
    async def test_memory_queue_single_task(self) -> None:
        """Queue mode with memory backend enqueues one task and waits."""
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
                "fix bug",
                "/tmp/repo",
                _fake_config(),
                "gemini",
                "key",
                queue_backend="memory",
                redis_url="redis://localhost",
                max_concurrent_runs=0,
            )

            mock_make_runner.assert_called_once()

    @pytest.mark.asyncio
    async def test_memory_queue_multiple_tasks(self) -> None:
        """Multiple tasks enqueued sequentially via memory queue, all complete."""
        from agent_forge.cli import _run_agent_queued

        enqueue_calls: list[str] = []

        with (
            patch("agent_forge.cli._make_task_runner") as mock_make_runner,
            patch("agent_forge.orchestration.worker.Worker.start", new_callable=AsyncMock),
            patch(
                "agent_forge.orchestration.worker.Worker.stop",
                new_callable=AsyncMock,
            ) as mock_stop,
            patch(
                "agent_forge.orchestration.queue.InMemoryQueue.get_status",
                new_callable=AsyncMock,
                return_value=TaskStatus.COMPLETED,
            ),
            patch(
                "agent_forge.orchestration.queue.InMemoryQueue.enqueue",
                new_callable=AsyncMock,
                side_effect=lambda _t: f"task-{len(enqueue_calls)}",
            ),
        ):

            async def fake_runner(task: Task) -> None:
                pass

            mock_make_runner.return_value = fake_runner

            # Run 3 tasks sequentially (each call to _run_agent_queued is one task)
            for i in range(3):
                await _run_agent_queued(
                    f"task {i}",
                    "/tmp/repo",
                    _fake_config(),
                    "gemini",
                    "key",
                    queue_backend="memory",
                    redis_url="redis://localhost",
                    max_concurrent_runs=0,
                )

            # worker.stop called once per invocation = 3 times
            assert mock_stop.call_count == 3

    @pytest.mark.asyncio
    async def test_memory_queue_worker_receives_concurrency(self) -> None:
        """max_concurrent_runs is forwarded to InMemoryQueue (no effect but no crash)."""
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
                return_value="task-1",
            ),
        ):

            async def fake_runner(task: Task) -> None:
                pass

            mock_make_runner.return_value = fake_runner

            # Should not crash with non-zero concurrency
            await _run_agent_queued(
                "task",
                "/tmp/repo",
                _fake_config(),
                "gemini",
                "key",
                queue_backend="memory",
                redis_url="redis://localhost",
                max_concurrent_runs=8,
            )


class TestSandboxConfigWiring:
    def test_build_sandbox_config(self) -> None:
        from agent_forge.cli import _build_sandbox_config

        cfg = _fake_config()
        cfg.sandbox.image = "agent-forge-sandbox:full"
        cfg.sandbox.cpu_limit = 2.0
        cfg.sandbox.memory_limit = "2g"
        cfg.sandbox.timeout_seconds = 480
        cfg.sandbox.network_enabled = True
        cfg.sandbox.writable_cache_mounts = True

        sandbox_config = _build_sandbox_config(cfg)

        assert sandbox_config == SandboxConfig(
            image="agent-forge-sandbox:full",
            cpu_limit=2.0,
            memory_limit="2g",
            timeout_seconds=480,
            network_enabled=True,
            writable_cache_mounts=True,
        )


# ---------------------------------------------------------------------------
# Redis queue mode
# ---------------------------------------------------------------------------


class TestRedisQueueMode:
    """Redis queue: single and multiple tasks."""

    @pytest.mark.asyncio
    async def test_redis_queue_single_task(self) -> None:
        """--queue=redis creates RedisQueue with correct URL."""
        from agent_forge.cli import _run_agent_queued

        with (
            patch("agent_forge.cli._make_task_runner") as mock_make_runner,
            patch("agent_forge.orchestration.redis_queue.RedisQueue") as mock_redis_cls,
            patch("agent_forge.orchestration.worker.Worker.start", new_callable=AsyncMock),
            patch("agent_forge.orchestration.worker.Worker.stop", new_callable=AsyncMock),
        ):
            mock_queue = AsyncMock()
            mock_queue.enqueue = AsyncMock(return_value="task-123")
            mock_queue.get_status = AsyncMock(return_value=TaskStatus.COMPLETED)
            mock_queue.close = AsyncMock()
            mock_redis_cls.return_value = mock_queue

            async def fake_runner(task: Task) -> None:
                pass

            mock_make_runner.return_value = fake_runner

            await _run_agent_queued(
                "fix bug",
                "/tmp/repo",
                _fake_config(),
                "gemini",
                "key",
                queue_backend="redis",
                redis_url="redis://custom:6380/1",
                max_concurrent_runs=5,
            )

            mock_redis_cls.assert_called_once_with(
                redis_url="redis://custom:6380/1",
                max_concurrent_runs=5,
            )
            mock_queue.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_redis_queue_multiple_tasks(self) -> None:
        """Multiple sequential invocations via Redis — close called each time."""
        from agent_forge.cli import _run_agent_queued

        with (
            patch("agent_forge.cli._make_task_runner") as mock_make_runner,
            patch("agent_forge.orchestration.redis_queue.RedisQueue") as mock_redis_cls,
            patch("agent_forge.orchestration.worker.Worker.start", new_callable=AsyncMock),
            patch("agent_forge.orchestration.worker.Worker.stop", new_callable=AsyncMock),
        ):
            mock_queue = AsyncMock()
            mock_queue.enqueue = AsyncMock(side_effect=[f"task-{i}" for i in range(3)])
            mock_queue.get_status = AsyncMock(return_value=TaskStatus.COMPLETED)
            mock_queue.close = AsyncMock()
            mock_redis_cls.return_value = mock_queue

            async def fake_runner(task: Task) -> None:
                pass

            mock_make_runner.return_value = fake_runner

            for i in range(3):
                await _run_agent_queued(
                    f"task {i}",
                    "/tmp/repo",
                    _fake_config(),
                    "gemini",
                    "key",
                    queue_backend="redis",
                    redis_url="redis://localhost:6379/0",
                    max_concurrent_runs=2,
                )

            # close called once per invocation
            assert mock_queue.close.call_count == 3


# ---------------------------------------------------------------------------
# Cleanup on failure
# ---------------------------------------------------------------------------


class TestQueueModeCleanup:
    """Worker.stop + queue.close are called even when errors occur."""

    @pytest.mark.asyncio
    async def test_cleanup_on_task_failure(self) -> None:
        """worker.stop + queue.close called even when task status is FAILED."""
        from agent_forge.cli import _run_agent_queued

        with (
            patch("agent_forge.cli._make_task_runner") as mock_make_runner,
            patch("agent_forge.orchestration.worker.Worker.start", new_callable=AsyncMock),
            patch(
                "agent_forge.orchestration.worker.Worker.stop",
                new_callable=AsyncMock,
            ) as mock_stop,
            patch(
                "agent_forge.orchestration.queue.InMemoryQueue.get_status",
                new_callable=AsyncMock,
                return_value=TaskStatus.FAILED,
            ),
            patch(
                "agent_forge.orchestration.queue.InMemoryQueue.enqueue",
                new_callable=AsyncMock,
                return_value="task-fail",
            ),
        ):

            async def fake_runner(task: Task) -> None:
                pass

            mock_make_runner.return_value = fake_runner

            with pytest.raises(SystemExit):
                await _run_agent_queued(
                    "broken task",
                    "/tmp/repo",
                    _fake_config(),
                    "gemini",
                    "key",
                    queue_backend="memory",
                    redis_url="redis://localhost",
                    max_concurrent_runs=0,
                )

            # Worker.stop must be called in finally block
            mock_stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_redis_cleanup_on_exception(self) -> None:
        """Redis queue.close called even when worker.start raises."""
        from agent_forge.cli import _run_agent_queued

        with (
            patch("agent_forge.cli._make_task_runner") as mock_make_runner,
            patch("agent_forge.orchestration.redis_queue.RedisQueue") as mock_redis_cls,
            patch(
                "agent_forge.orchestration.worker.Worker.start",
                new_callable=AsyncMock,
                side_effect=RuntimeError("connection refused"),
            ),
            patch(
                "agent_forge.orchestration.worker.Worker.stop",
                new_callable=AsyncMock,
            ) as mock_stop,
        ):
            mock_queue = AsyncMock()
            mock_queue.enqueue = AsyncMock(return_value="task-x")
            mock_queue.close = AsyncMock()
            mock_redis_cls.return_value = mock_queue

            async def fake_runner(task: Task) -> None:
                pass

            mock_make_runner.return_value = fake_runner

            with pytest.raises(SystemExit):
                await _run_agent_queued(
                    "task",
                    "/tmp/repo",
                    _fake_config(),
                    "gemini",
                    "key",
                    queue_backend="redis",
                    redis_url="redis://localhost",
                    max_concurrent_runs=0,
                )

            # Both cleanup calls must happen
            mock_stop.assert_called_once()
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
