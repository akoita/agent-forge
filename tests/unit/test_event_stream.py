"""Unit tests for typed durable run-event streaming (#27)."""

from __future__ import annotations

import asyncio
import inspect
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from click.testing import CliRunner

from agent_forge.agent.core import react_loop
from agent_forge.agent.models import AgentConfig, AgentRun, RunState
from agent_forge.agent.persistence import save_run
from agent_forge.cli import main as cli_main
from agent_forge.config import ForgeConfig, ServiceSettings
from agent_forge.llm.base import LLMResponse, TokenUsage, ToolCall
from agent_forge.orchestration.event_stream import (
    STREAM_SCHEMA_VERSION,
    EventJournal,
    JournalEventSink,
    RunEventStream,
    StreamEvent,
    StreamEventPage,
    sanitize_event_data,
)
from agent_forge.orchestration.events import Event, EventBus, EventType
from agent_forge.sandbox.base import ExecResult, Sandbox, SandboxConfig
from agent_forge.service.app import create_app
from agent_forge.tools.base import ToolDefinition, ToolResult

if TYPE_CHECKING:
    from pathlib import Path

    from agent_forge.orchestration.queue import Task
    from agent_forge.tools import ToolRegistry


def _runner() -> CliRunner:
    kwargs: dict[str, Any] = {}
    if "mix_stderr" in inspect.signature(CliRunner).parameters:
        kwargs["mix_stderr"] = False
    return CliRunner(**kwargs)


class _NoopSandbox(Sandbox):
    async def start(self, repo_path: str, config: SandboxConfig | None = None) -> None:
        del repo_path, config

    async def stop(self) -> None:
        return None

    async def is_alive(self) -> bool:
        return True

    async def read_file(self, path: str) -> str:
        raise FileNotFoundError(path)

    async def write_file(self, path: str, content: str) -> None:
        del path, content

    async def exec(self, command: str, *, timeout_seconds: int = 30) -> ExecResult:
        del command, timeout_seconds
        return ExecResult(exit_code=0, stdout="", stderr="")


def _tool_registry() -> ToolRegistry:
    registry = AsyncMock()
    registry.list_definitions = lambda: [
        ToolDefinition(
            name="read_file",
            description="Read file",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}},
        )
    ]
    tool = AsyncMock()
    tool.execute.return_value = ToolResult(output="ok", exit_code=0)
    registry.get = lambda _name: tool
    return registry  # type: ignore[return-value]


class TestJournalAndBackpressure:
    """Durable EventJournal, payload bounds, and pagination."""

    @pytest.mark.asyncio
    async def test_journal_sink_records_react_loop_events(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runs_dir = tmp_path / "runs"
        monkeypatch.setattr(
            "agent_forge.agent.core.save_run", lambda r: save_run(r, base_dir=runs_dir)
        )
        monkeypatch.setattr(
            "agent_forge.observability.cost.save_summary",
            lambda *_args: tmp_path / "summary.json",
        )

        bus = EventBus()
        run = AgentRun(task="Inspect README", repo_path=str(tmp_path), config=AgentConfig())
        sink = JournalEventSink(base_dir=runs_dir, run_id=run.id)
        await sink.attach(bus)

        llm = AsyncMock()
        llm.complete.side_effect = [
            LLMResponse(
                content="Reading file",
                tool_calls=[ToolCall(id="tc-1", name="read_file", arguments={"path": "README.md"})],
                usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content="Done.",
                tool_calls=[],
                usage=TokenUsage(prompt_tokens=8, completion_tokens=4, total_tokens=12),
                finish_reason="stop",
            ),
        ]

        try:
            await react_loop(run, llm, _tool_registry(), _NoopSandbox(), event_bus=bus)
        finally:
            await sink.detach(bus)

        assert run.state == RunState.COMPLETED
        stream = RunEventStream(run.id, base_dir=runs_dir)
        page = stream.read_page(after_seq=-1, limit=50)

        assert page.run_id == run.id
        assert page.terminal is True
        assert page.has_more is False
        assert [ev.seq for ev in page.events] == list(range(len(page.events)))
        assert all(ev.schema_version == STREAM_SCHEMA_VERSION for ev in page.events)

        event_types = [ev.type for ev in page.events]
        assert event_types[0] == EventType.RUN_STARTED.value
        assert EventType.TOOL_CALLED.value in event_types
        assert EventType.TOOL_COMPLETED.value in event_types
        assert EventType.TOKEN_USAGE.value in event_types
        assert event_types[-1] == EventType.RUN_COMPLETED.value

    def test_sanitize_event_data_truncates_large_strings_and_lists(self) -> None:
        raw = {
            "output": "x" * 200,
            "items": list(range(15)),
            "nested": {"detail": "y" * 120},
        }
        cleaned, truncated = sanitize_event_data(raw, max_field_chars=50, max_list_items=5)
        assert truncated is True
        assert cleaned["output"].endswith("... [truncated]")
        assert len(cleaned["output"]) < 80
        assert cleaned["items"] == [0, 1, 2, 3, 4]
        assert cleaned["nested"]["detail"].endswith("... [truncated]")

    def test_journal_pagination_and_bounds(self, tmp_path: Path) -> None:
        journal = EventJournal("run-page", base_dir=tmp_path, max_field_chars=20, max_list_items=3)
        journal.append(
            Event(
                type=EventType.RUN_STARTED,
                run_id="run-page",
                data={"note": "short"},
            )
        )
        journal.append(
            Event(
                type=EventType.TOOL_CALLED,
                run_id="run-page",
                data={"blob": "A" * 100, "tags": ["a", "b", "c", "d"]},
            )
        )
        journal.append(Event(type=EventType.ITERATION_COMPLETED, run_id="run-page", data={"i": 1}))
        journal.append(Event(type=EventType.RUN_COMPLETED, run_id="run-page"))

        first = journal.read_page(after_seq=-1, limit=2)
        assert [e.seq for e in first.events] == [0, 1]
        assert first.next_after_seq == 1
        assert first.has_more is True
        assert first.terminal is True
        assert first.events[0].truncated is False
        assert first.events[1].truncated is True

        second = journal.read_page(after_seq=first.next_after_seq, limit=2)
        assert [e.seq for e in second.events] == [2, 3]
        assert second.next_after_seq == 3
        assert second.has_more is False


class TestResumableFollow:
    """Disconnect/reconnect and pause/resume replay semantics."""

    @pytest.mark.asyncio
    async def test_disconnect_reconnect_mid_run_loses_no_events(self, tmp_path: Path) -> None:
        journal = EventJournal("run-resume", base_dir=tmp_path)
        for idx, ev_type in enumerate(
            [
                EventType.RUN_STARTED,
                EventType.ITERATION_STARTED,
                EventType.TOOL_CALLED,
                EventType.TOOL_COMPLETED,
            ]
        ):
            journal.append(Event(type=ev_type, run_id="run-resume", data={"step": idx}))

        stream = RunEventStream("run-resume", base_dir=tmp_path)
        first_consumer_seqs: list[int] = []
        async for ev in stream.follow(after_seq=-1, page_size=2, poll_interval=0.01):
            first_consumer_seqs.append(ev.seq)
            if ev.seq == 2:
                break

        assert first_consumer_seqs == [0, 1, 2]
        last_seen_seq = first_consumer_seqs[-1]

        async def _emit_remaining() -> None:
            await asyncio.sleep(0.03)
            journal.append(Event(type=EventType.TOKEN_USAGE, run_id="run-resume", data={"step": 4}))
            journal.append(
                Event(type=EventType.ITERATION_COMPLETED, run_id="run-resume", data={"step": 5})
            )
            journal.append(
                Event(type=EventType.RUN_COMPLETED, run_id="run-resume", data={"step": 6})
            )

        producer = asyncio.create_task(_emit_remaining())
        resumed_events: list[StreamEvent] = []
        async for ev in stream.follow(after_seq=last_seen_seq, page_size=2, poll_interval=0.01):
            resumed_events.append(ev)
        await producer

        assert [ev.seq for ev in resumed_events] == [3, 4, 5, 6]
        assert resumed_events[-1].type == EventType.RUN_COMPLETED.value

    @pytest.mark.asyncio
    async def test_replay_does_not_stop_early_at_historical_pause(self, tmp_path: Path) -> None:
        journal = EventJournal("run-paused-resumed", base_dir=tmp_path)
        for ev_type in [
            EventType.RUN_STARTED,
            EventType.CHECKPOINT_SAVED,
            EventType.RUN_PAUSED,
            EventType.RUN_STARTED,
            EventType.ITERATION_COMPLETED,
            EventType.RUN_COMPLETED,
        ]:
            journal.append(Event(type=ev_type, run_id="run-paused-resumed"))

        stream = RunEventStream("run-paused-resumed", base_dir=tmp_path)
        replayed = [ev async for ev in stream.follow(after_seq=-1, page_size=2, poll_interval=0.01)]
        assert [ev.type for ev in replayed] == [
            EventType.RUN_STARTED.value,
            EventType.CHECKPOINT_SAVED.value,
            EventType.RUN_PAUSED.value,
            EventType.RUN_STARTED.value,
            EventType.ITERATION_COMPLETED.value,
            EventType.RUN_COMPLETED.value,
        ]


class TestCliEventsAndStatusFollow:
    """CLI commands `events` and `status --follow` read from RunEventStream."""

    def test_cli_events_json_and_follow_and_status_follow(self, tmp_path: Path) -> None:
        runs_dir = tmp_path / "runs"
        run = AgentRun(task="Stream test", repo_path=str(tmp_path), config=AgentConfig())
        run.state = RunState.COMPLETED
        run.iterations = 1
        run.completed_at = datetime.now(UTC)
        save_run(run, base_dir=runs_dir)

        journal = EventJournal(run.id, base_dir=runs_dir)
        journal.append(Event(type=EventType.RUN_STARTED, run_id=run.id, data={"iter": 0}))
        journal.append(Event(type=EventType.TOOL_CALLED, run_id=run.id, data={"tool": "read_file"}))
        journal.append(Event(type=EventType.RUN_COMPLETED, run_id=run.id, data={"iter": 1}))

        runner = _runner()
        with patch("agent_forge.agent.persistence._default_runs_dir", return_value=runs_dir):
            page_res = runner.invoke(
                cli_main,
                ["events", run.id, "--limit", "2", "--output-format", "json"],
            )
            follow_res = runner.invoke(
                cli_main,
                ["events", run.id, "--after-seq", "0", "--follow", "--output-format", "json"],
            )
            status_follow_res = runner.invoke(
                cli_main,
                ["status", run.id, "--follow"],
            )
            missing_res = runner.invoke(cli_main, ["events", "missing-run"])

        assert page_res.exit_code == 0
        page = StreamEventPage.model_validate_json(page_res.output)
        assert [ev.seq for ev in page.events] == [0, 1]
        assert page.has_more is True
        assert page.next_after_seq == 1

        assert follow_res.exit_code == 0
        ndjson_lines = [line for line in follow_res.output.strip().splitlines() if line.strip()]
        streamed = [StreamEvent.model_validate_json(line) for line in ndjson_lines]
        assert [ev.seq for ev in streamed] == [1, 2]
        assert streamed[-1].type == EventType.RUN_COMPLETED.value

        assert status_follow_res.exit_code == 0
        assert "run.started" in status_follow_res.output
        assert "tool.called" in status_follow_res.output
        assert "run.completed" in status_follow_res.output

        assert missing_res.exit_code == 1


class TestHostedServiceEventStream:
    """Hosted FastAPI service exposes JSON pagination and SSE Last-Event-ID resume."""

    @staticmethod
    def _request_payload(source_uri: str) -> dict[str, object]:
        return {
            "schema_version": "agent-forge-run-request-v1",
            "client": {
                "name": "client-app",
                "request_id": "req-1",
                "service_id": "test-client",
            },
            "profile": {
                "id": "proof-of-audit-solidity-v1",
                "report_schema": "proof-of-audit-report-v1",
                "max_iterations": 2,
            },
            "source": {
                "kind": "local_path",
                "uri": source_uri,
                "source_digest": "sha256:test",
            },
            "target": {
                "submission_kind": "deployed_address",
                "network": "base-sepolia",
                "chain_id": 84532,
                "contract_address": "0xabc",
            },
            "artifacts": {
                "result_delivery": "pull",
                "include_logs": True,
            },
        }

    @pytest.mark.asyncio
    async def test_service_events_json_pagination_and_sse_reconnect(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
        runs_dir = tmp_path / "runs"

        app = create_app(
            service_root=tmp_path / "service-root",
            runs_dir=runs_dir,
            config=ForgeConfig(
                service=ServiceSettings(
                    root_dir=str(tmp_path / "service-root"),
                    auth_enabled=False,
                    allow_local_path_sources=True,
                )
            ),
        )
        service = app.state.service

        async def fake_runner(task: Task) -> None:
            record = service._records[task.id]
            record.started_at = record.created_at
            await service._event_bus.publish(
                Event(type=EventType.ITERATION_STARTED, run_id=task.id, data={"iteration": 1})
            )
            await service._event_bus.publish(
                Event(
                    type=EventType.TOOL_CALLED,
                    run_id=task.id,
                    data={"tool_name": "read_file", "arguments": {"path": "app.py"}},
                )
            )
            await service._event_bus.publish(
                Event(type=EventType.TOOL_COMPLETED, run_id=task.id, data={"duration_ms": 5})
            )
            record.report_path.parent.mkdir(parents=True, exist_ok=True)
            record.report_path.write_text(
                json.dumps(
                    {
                        "schema_version": "proof-of-audit-report-v1",
                        "run_id": task.id,
                        "summary": "ok",
                        "confidence": "high",
                        "findings": [],
                    }
                ),
                encoding="utf-8",
            )
            record.completed_at = datetime.now(UTC)

        service._worker._task_runner = fake_runner

        transport = httpx.ASGITransport(app=app)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=transport, base_url="http://testserver") as client,
        ):
            create_resp = await client.post("/v1/runs", json=self._request_payload(str(repo)))
            assert create_resp.status_code == 202
            run_id = create_resp.json()["run_id"]

            for _ in range(40):
                status_resp = await client.get(f"/v1/runs/{run_id}")
                if status_resp.json()["status"] == "completed":
                    break
                await asyncio.sleep(0.025)
            assert status_resp.json()["status"] == "completed"

            # Verify logs artifact includes stream_jsonl
            logs_resp = await client.get(f"/v1/runs/{run_id}/logs")
            assert logs_resp.status_code == 200
            assert logs_resp.json()["artifacts"]["stream_jsonl"].endswith(f"{run_id}/stream.jsonl")

            # Paginated JSON read
            page1_resp = await client.get(f"/v1/runs/{run_id}/events?after_seq=-1&limit=2")
            assert page1_resp.status_code == 200
            page1 = StreamEventPage.model_validate(page1_resp.json())
            assert [ev.seq for ev in page1.events] == [0, 1]
            assert page1.has_more is True
            assert page1.terminal is True

            # SSE resume using Last-Event-ID header
            sse_resp = await client.get(
                f"/v1/runs/{run_id}/events",
                headers={"Accept": "text/event-stream", "Last-Event-ID": "1"},
            )
            assert sse_resp.status_code == 200
            assert sse_resp.headers["content-type"].startswith("text/event-stream")

            frames = [
                chunk.strip() for chunk in sse_resp.text.strip().split("\n\n") if chunk.strip()
            ]
            parsed_events: list[StreamEvent] = []
            frame_ids: list[int] = []
            for frame in frames:
                for line in frame.splitlines():
                    if line.startswith("id: "):
                        frame_ids.append(int(line[4:]))
                    elif line.startswith("data: "):
                        parsed_events.append(StreamEvent.model_validate_json(line[6:]))

            assert frame_ids == [2, 3, 4]
            assert [ev.seq for ev in parsed_events] == [2, 3, 4]
            assert parsed_events[-1].type == EventType.RUN_COMPLETED.value

            # Unknown run and bad Last-Event-ID return 404 / 400
            assert (await client.get("/v1/runs/run_missing/events")).status_code == 404
            assert (
                await client.get(f"/v1/runs/{run_id}/events", headers={"Last-Event-ID": "abc"})
            ).status_code == 400

    @pytest.mark.asyncio
    async def test_service_events_enforces_auth_when_enabled(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        clients_path = tmp_path / "clients.toml"
        clients_path.write_text(
            """
[clients.test-client]
api_key_env = "TEST_CLIENT_API_KEY"
allowed_profiles = ["proof-of-audit-solidity-v1"]
allowed_report_schemas = ["proof-of-audit-report-v1"]
allowed_source_kinds = ["local_path"]
max_active_runs = 2
max_runs_per_day = 10
allow_local_path = true
""".lstrip(),
            encoding="utf-8",
        )
        monkeypatch.setenv("TEST_CLIENT_API_KEY", "secret-token")

        app = create_app(
            service_root=tmp_path / "service-root",
            runs_dir=tmp_path / "runs",
            config=ForgeConfig(
                service=ServiceSettings(
                    root_dir=str(tmp_path / "service-root"),
                    auth_enabled=True,
                    clients_path=str(clients_path),
                    allow_local_path_sources=True,
                )
            ),
        )
        service = app.state.service

        async def noop_runner(task: Task) -> None:
            record = service._records[task.id]
            record.completed_at = datetime.now(UTC)

        service._worker._task_runner = noop_runner
        headers = {"X-Agent-Forge-API-Key": "secret-token"}

        transport = httpx.ASGITransport(app=app)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=transport, base_url="http://testserver") as client,
        ):
            create_resp = await client.post(
                "/v1/runs",
                json=self._request_payload(str(repo)),
                headers=headers,
            )
            assert create_resp.status_code == 202
            run_id = create_resp.json()["run_id"]

            for _ in range(40):
                status_resp = await client.get(f"/v1/runs/{run_id}", headers=headers)
                if status_resp.json()["status"] in {"completed", "failed"}:
                    break
                await asyncio.sleep(0.025)

            unauth_resp = await client.get(f"/v1/runs/{run_id}/events?follow=true")
            assert unauth_resp.status_code == 401

            auth_sse = await client.get(f"/v1/runs/{run_id}/events?follow=true", headers=headers)
            assert auth_sse.status_code == 200
            assert "event: run.completed" in auth_sse.text
