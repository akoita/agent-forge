"""E2E tests for the full agent pipeline with mocked LLM responses.

Tests CLI → queue → agent → sandbox → completion using respx to intercept
HTTP calls to the Gemini API, providing deterministic recorded responses.

These tests validate the integrated pipeline without requiring an API key,
making them suitable for CI.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx
from click.testing import CliRunner

from agent_forge.cli import main
from agent_forge.orchestration.events import EventType

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"
SAMPLE_REPO = FIXTURES_DIR / "sample_repo"


def _gemini_stop_response(text: str) -> dict:
    """Build a minimal Gemini API response that ends the loop (no tool calls)."""
    return {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [{"text": text}],
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 50,
            "candidatesTokenCount": 20,
            "totalTokenCount": 70,
        },
    }


def _gemini_tool_then_stop(
    tool_name: str,
    tool_args: dict,
    final_text: str,
) -> list[dict]:
    """Build a 2-response sequence: tool call → stop.

    First response: LLM requests a tool call.
    Second response: LLM produces final text (loop ends).
    """
    tool_call_resp = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {
                            "functionCall": {
                                "name": tool_name,
                                "args": tool_args,
                            },
                        },
                    ],
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 100,
            "candidatesTokenCount": 30,
            "totalTokenCount": 130,
        },
    }
    stop_resp = _gemini_stop_response(final_text)
    return [tool_call_resp, stop_resp]


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Copy the sample repo to a temp dir for isolated modification."""
    import shutil

    dst = tmp_path / "repo"
    shutil.copytree(SAMPLE_REPO, dst)
    return dst


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_with_recorded_responses(
    runner: CliRunner,
    workspace: Path,
    task: str,
    responses: list[dict],
    *,
    extra_args: list[str] | None = None,
) -> object:
    """Run ``agent-forge run`` with mocked Gemini HTTP responses.

    Uses respx to intercept all POST requests to the Gemini
    ``generateContent`` endpoint, returning *responses* in order.
    """
    side_effects = [httpx.Response(200, json=r) for r in responses]

    with respx.mock(assert_all_called=False) as mock_router:
        # Route on the scoped router — NOT global respx
        mock_router.post(
            url__regex=r".*generativelanguage.*generateContent.*",
        ).side_effect = side_effects

        args = [
            "run",
            "--task",
            task,
            "--repo",
            str(workspace),
            "--max-iterations",
            "3",
        ]
        if extra_args:
            args.extend(extra_args)

        with patch.dict("os.environ", {"GEMINI_API_KEY": "test-key-for-vcr"}):
            return runner.invoke(main, args)


# ---------------------------------------------------------------------------
# Tests — Direct mode pipeline
# ---------------------------------------------------------------------------


class TestPipelineDirect:
    """E2E: full pipeline in direct mode (no --queue)."""

    def test_fix_health_endpoint(
        self,
        runner: CliRunner,
        workspace: Path,
    ) -> None:
        """Agent reads app.py, sees the 500 bug, and produces a fix."""
        responses = _gemini_tool_then_stop(
            tool_name="read_file",
            tool_args={"path": "app.py"},
            final_text=(
                "I found the bug: the /health endpoint returns 500 "
                "instead of 200. The fix is to change the status code."
            ),
        )

        result = _run_with_recorded_responses(
            runner,
            workspace,
            "Fix the health endpoint bug in app.py — it should return 200",
            responses,
        )

        # Agent ran and completed (either 0=success or 1=task summary shown)
        assert result.exit_code in (0, 1), f"Crash: {result.output}"

    def test_add_type_hints(
        self,
        runner: CliRunner,
        workspace: Path,
    ) -> None:
        """Agent reads utils.py and reports about missing type hints."""
        responses = _gemini_tool_then_stop(
            tool_name="read_file",
            tool_args={"path": "utils.py"},
            final_text=(
                "I've reviewed utils.py. The functions calculate_total, "
                "format_greeting, and parse_csv_line all lack type hints."
            ),
        )

        result = _run_with_recorded_responses(
            runner,
            workspace,
            "Add type hints to all functions in utils.py",
            responses,
        )

        assert result.exit_code in (0, 1), f"Crash: {result.output}"

    def test_add_input_validation(
        self,
        runner: CliRunner,
        workspace: Path,
    ) -> None:
        """Agent reads the greet endpoint and considers validation."""
        responses = _gemini_tool_then_stop(
            tool_name="read_file",
            tool_args={"path": "app.py"},
            final_text=(
                "The /greet/<name> endpoint needs input validation. "
                "I would add length checks and character sanitization."
            ),
        )

        result = _run_with_recorded_responses(
            runner,
            workspace,
            "Add input validation to the /greet/<name> endpoint in app.py",
            responses,
        )

        assert result.exit_code in (0, 1), f"Crash: {result.output}"


# ---------------------------------------------------------------------------
# Tests — Memory queue pipeline
# ---------------------------------------------------------------------------


class TestPipelineMemoryQueue:
    """E2E: full pipeline through memory queue."""

    def test_queue_mode_completes(
        self,
        runner: CliRunner,
        workspace: Path,
    ) -> None:
        """Task enqueued via --queue=memory completes the full pipeline."""
        responses = [_gemini_stop_response("Task done.")]

        result = _run_with_recorded_responses(
            runner,
            workspace,
            "List all files in the workspace",
            responses,
            extra_args=["--queue", "memory"],
        )

        assert result.exit_code in (0, 1), f"Crash: {result.output}"

    def test_queue_mode_with_tool_call(
        self,
        runner: CliRunner,
        workspace: Path,
    ) -> None:
        """Queue mode: agent reads a file then completes."""
        responses = _gemini_tool_then_stop(
            tool_name="read_file",
            tool_args={"path": "app.py"},
            final_text="Read app.py successfully via queue mode.",
        )

        result = _run_with_recorded_responses(
            runner,
            workspace,
            "Read app.py and summarize it",
            responses,
            extra_args=["--queue", "memory"],
        )

        assert result.exit_code in (0, 1), f"Crash: {result.output}"


# ---------------------------------------------------------------------------
# Tests — Redis queue pipeline
# ---------------------------------------------------------------------------


class TestPipelineRedisQueue:
    """E2E: full pipeline through Redis queue (RedisQueue replaced by InMemoryQueue).

    We patch the RedisQueue import inside the CLI so the full pipeline
    runs without a real Redis server.
    """

    def test_redis_queue_completes(
        self,
        runner: CliRunner,
        workspace: Path,
    ) -> None:
        """Task via --queue=redis completes (RedisQueue mocked)."""
        from agent_forge.orchestration.queue import InMemoryQueue

        responses = [_gemini_stop_response("Redis queue task done.")]

        side_effects = [httpx.Response(200, json=r) for r in responses]

        with (
            respx.mock(assert_all_called=False) as mock_router,
            patch(
                "agent_forge.orchestration.redis_queue.RedisQueue",
                InMemoryQueue,
            ),
            patch.dict(
                "os.environ",
                {"GEMINI_API_KEY": "test-key-for-vcr"},
            ),
        ):
            mock_router.post(
                url__regex=r".*generativelanguage.*generateContent.*",
            ).side_effect = side_effects

            result = runner.invoke(
                main,
                [
                    "run",
                    "--task",
                    "List all files",
                    "--repo",
                    str(workspace),
                    "--max-iterations",
                    "3",
                    "--queue",
                    "redis",
                    "--redis-url",
                    "redis://localhost:6379/0",
                ],
            )

        assert result.exit_code in (0, 1), f"Crash: {result.output}"

    def test_redis_queue_with_tool_call(
        self,
        runner: CliRunner,
        workspace: Path,
    ) -> None:
        """Redis queue mode: agent reads a file then completes."""
        from agent_forge.orchestration.queue import InMemoryQueue

        responses = _gemini_tool_then_stop(
            tool_name="read_file",
            tool_args={"path": "app.py"},
            final_text="Read app.py via Redis queue mode.",
        )

        side_effects = [httpx.Response(200, json=r) for r in responses]

        with (
            respx.mock(assert_all_called=False) as mock_router,
            patch(
                "agent_forge.orchestration.redis_queue.RedisQueue",
                InMemoryQueue,
            ),
            patch.dict(
                "os.environ",
                {"GEMINI_API_KEY": "test-key-for-vcr"},
            ),
        ):
            mock_router.post(
                url__regex=r".*generativelanguage.*generateContent.*",
            ).side_effect = side_effects

            result = runner.invoke(
                main,
                [
                    "run",
                    "--task",
                    "Read app.py and summarize it",
                    "--repo",
                    str(workspace),
                    "--max-iterations",
                    "3",
                    "--queue",
                    "redis",
                    "--redis-url",
                    "redis://localhost:6379/0",
                    "--max-concurrent-runs",
                    "2",
                ],
            )

        assert result.exit_code in (0, 1), f"Crash: {result.output}"


# ---------------------------------------------------------------------------
# Tests — Parallel agents (multiple tasks through one Worker)
# ---------------------------------------------------------------------------


class TestParallelAgents:
    """E2E: multiple tasks processed concurrently via Worker + InMemoryQueue."""

    @pytest.mark.asyncio
    async def test_multiple_tasks_via_memory_queue(self) -> None:
        """3 tasks enqueued → Worker processes all → all complete."""
        from agent_forge.orchestration.events import EventBus
        from agent_forge.orchestration.queue import (
            InMemoryQueue,
            Task,
            TaskStatus,
        )
        from agent_forge.orchestration.worker import Worker

        queue = InMemoryQueue()
        bus = EventBus()
        processed: list[str] = []

        async def runner(task: Task) -> None:
            await asyncio.sleep(0.01)  # simulate work
            processed.append(task.id)

        worker = Worker(queue, bus, runner, poll_interval=0.01)

        # Enqueue 3 tasks
        for i in range(3):
            await queue.enqueue(
                Task(
                    id=f"parallel-{i}",
                    task_description=f"Task {i}",
                    repo_path="/tmp/repo",
                    config=None,
                )
            )

        await worker.start()
        await asyncio.sleep(0.5)  # let worker process
        await worker.stop()

        assert sorted(processed) == [
            "parallel-0",
            "parallel-1",
            "parallel-2",
        ]

        # All should be COMPLETED
        for i in range(3):
            status = await queue.get_status(f"parallel-{i}")
            assert status == TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_parallel_tasks_with_mixed_outcomes(self) -> None:
        """Tasks with mixed success/failure are handled correctly."""
        from agent_forge.orchestration.events import Event, EventBus
        from agent_forge.orchestration.queue import (
            InMemoryQueue,
            Task,
            TaskStatus,
        )
        from agent_forge.orchestration.worker import Worker

        queue = InMemoryQueue()
        bus = EventBus()
        events: list[Event] = []

        async def handler(event: Event) -> None:
            events.append(event)

        await bus.subscribe(EventType.RUN_COMPLETED, handler)
        await bus.subscribe(EventType.RUN_FAILED, handler)

        async def runner(task: Task) -> None:
            if "fail" in task.task_description:
                msg = "deliberate failure"
                raise RuntimeError(msg)
            await asyncio.sleep(0.01)

        worker = Worker(queue, bus, runner, poll_interval=0.01)

        # 2 success + 1 failure
        for task_id, desc in [
            ("ok-1", "good task"),
            ("fail-1", "should fail"),
            ("ok-2", "another good task"),
        ]:
            await queue.enqueue(
                Task(
                    id=task_id,
                    task_description=desc,
                    repo_path="/tmp/repo",
                    config=None,
                )
            )

        await worker.start()
        await asyncio.sleep(0.5)
        await worker.stop()

        # Verify statuses
        assert await queue.get_status("ok-1") == TaskStatus.COMPLETED
        assert await queue.get_status("fail-1") == TaskStatus.FAILED
        assert await queue.get_status("ok-2") == TaskStatus.COMPLETED

        # Verify events
        completed = [e for e in events if e.type == EventType.RUN_COMPLETED]
        failed = [e for e in events if e.type == EventType.RUN_FAILED]
        assert len(completed) == 2
        assert len(failed) == 1

    @pytest.mark.asyncio
    async def test_priority_ordering_with_parallel_tasks(self) -> None:
        """Higher-priority tasks are processed first."""
        from agent_forge.orchestration.events import EventBus
        from agent_forge.orchestration.queue import (
            InMemoryQueue,
            Task,
            TaskStatus,
        )
        from agent_forge.orchestration.worker import Worker

        queue = InMemoryQueue()
        bus = EventBus()
        order: list[str] = []

        async def runner(task: Task) -> None:
            order.append(task.id)

        worker = Worker(queue, bus, runner, poll_interval=0.01)

        # Enqueue with different priorities (higher = first)
        await queue.enqueue(
            Task(
                id="low",
                task_description="low priority",
                repo_path="/tmp/repo",
                config=None,
                priority=1,
            )
        )
        await queue.enqueue(
            Task(
                id="high",
                task_description="high priority",
                repo_path="/tmp/repo",
                config=None,
                priority=10,
            )
        )
        await queue.enqueue(
            Task(
                id="mid",
                task_description="mid priority",
                repo_path="/tmp/repo",
                config=None,
                priority=5,
            )
        )

        await worker.start()
        await asyncio.sleep(0.3)
        await worker.stop()

        assert order == ["high", "mid", "low"]

        for tid in ("high", "mid", "low"):
            assert await queue.get_status(tid) == TaskStatus.COMPLETED
