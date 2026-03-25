"""FastAPI app for the hosted agent-forge service contract."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException

from agent_forge.orchestration.events import EventBus
from agent_forge.orchestration.queue import InMemoryQueue, Task, TaskStatus
from agent_forge.orchestration.worker import Worker
from agent_forge.service.models import (
    ErrorResponse,
    RunArtifactRef,
    RunError,
    RunRequest,
    RunStatus,
)


@dataclass
class HostedRunRecord:
    """Persistent in-memory record for a hosted run."""

    run_id: str
    request: RunRequest
    workspace_dir: Path
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: RunError | None = None


class HostedRunService:
    """In-process implementation of the versioned hosted run API."""

    def __init__(self, *, service_root: Path | None = None) -> None:
        self._service_root = service_root or Path.cwd() / ".agent-forge-service"
        self._queue = InMemoryQueue()
        self._event_bus = EventBus()
        self._records: dict[str, HostedRunRecord] = {}
        self._worker = Worker(
            queue=self._queue,
            event_bus=self._event_bus,
            task_runner=self._make_task_runner(),
            poll_interval=0.05,
        )

    async def start(self) -> None:
        """Start the background worker for hosted runs."""
        self._service_root.mkdir(parents=True, exist_ok=True)
        await self._worker.start()

    async def stop(self) -> None:
        """Stop the background worker for hosted runs."""
        await self._worker.stop()

    async def create_run(self, request: RunRequest) -> RunStatus:
        """Accept a new hosted run request and enqueue it."""
        run_id = f"run_{uuid.uuid4().hex[:16]}"
        workspace_dir = self._service_root / "runs" / run_id
        workspace_dir.mkdir(parents=True, exist_ok=True)
        record = HostedRunRecord(
            run_id=run_id,
            request=request,
            workspace_dir=workspace_dir,
            created_at=datetime.now(UTC),
        )
        self._records[run_id] = record
        await self._queue.enqueue(
            Task(
                id=run_id,
                task_description=f"Hosted run for profile {request.profile.id}",
                repo_path=str(workspace_dir),
                config=None,
            )
        )
        return self._build_status(record, status="accepted")

    async def get_status(self, run_id: str) -> RunStatus:
        """Return the current lifecycle document for a run."""
        record = self._records.get(run_id)
        if record is None:
            raise KeyError(run_id)
        queue_status = await self._queue.get_status(run_id)
        return self._build_status(record, status=self._map_status(queue_status))

    def _make_task_runner(self) -> Any:
        async def _runner(task: Task) -> None:
            record = self._records[task.id]
            record.started_at = datetime.now(UTC)
            # Keep the contract worker generic in v1; later issues plug in
            # headless execution and artifact writers behind the same surface.
            await asyncio.sleep(0.01)
            record.completed_at = datetime.now(UTC)

        return _runner

    def _build_status(self, record: HostedRunRecord, *, status: str) -> RunStatus:
        return RunStatus(
            run_id=record.run_id,
            status=status,  # type: ignore[arg-type]
            created_at=record.created_at.isoformat(),
            started_at=record.started_at.isoformat() if record.started_at else None,
            completed_at=record.completed_at.isoformat() if record.completed_at else None,
            client=record.request.client,
            profile=record.request.profile,
            status_url=f"/v1/runs/{record.run_id}",
            artifacts=self._artifact_refs(record),
            error=record.error,
        )

    def _artifact_refs(self, record: HostedRunRecord) -> list[RunArtifactRef]:
        include_logs = (
            record.request.artifacts is None
            or bool(record.request.artifacts.include_logs)
        )
        artifacts = [
            RunArtifactRef(
                kind="report",
                url=f"/v1/runs/{record.run_id}/report",
                available=record.completed_at is not None and record.error is None,
                content_type="application/json",
            ),
            RunArtifactRef(
                kind="run_metadata",
                url=f"/v1/runs/{record.run_id}/metadata",
                available=True,
                content_type="application/json",
            ),
        ]
        if include_logs:
            artifacts.append(
                RunArtifactRef(
                    kind="logs",
                    url=f"/v1/runs/{record.run_id}/logs",
                    available=record.completed_at is not None,
                    content_type="application/json",
                )
            )
        return artifacts

    def _map_status(self, queue_status: TaskStatus) -> str:
        mapping = {
            TaskStatus.QUEUED: "queued",
            TaskStatus.PROCESSING: "running",
            TaskStatus.COMPLETED: "completed",
            TaskStatus.FAILED: "failed",
        }
        return mapping[queue_status]


def create_app(*, service_root: Path | None = None) -> FastAPI:
    """Create the hosted service ASGI app."""
    service = HostedRunService(service_root=service_root)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> Any:
        await service.start()
        try:
            yield
        finally:
            await service.stop()

    app = FastAPI(title="agent-forge hosted service", version="0.1.0", lifespan=lifespan)
    app.state.service = service

    @app.post("/v1/runs", response_model=RunStatus, status_code=202)
    async def create_run(request: RunRequest) -> RunStatus:
        return await service.create_run(request)

    @app.get("/v1/runs/{run_id}", response_model=RunStatus)
    async def get_run(run_id: str) -> RunStatus:
        try:
            return await service.get_status(run_id)
        except KeyError as exc:
            error = ErrorResponse(
                error=RunError(
                    code="invalid_request",
                    message=f"unknown run id: {run_id}",
                    retryable=False,
                )
            )
            raise HTTPException(status_code=404, detail=error.model_dump()) from exc

    return app
