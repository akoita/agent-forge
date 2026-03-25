"""FastAPI app for the hosted agent-forge service contract."""

from __future__ import annotations

import json
import os
import shutil
import tarfile
import uuid
import zipfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException

from agent_forge.agent.core import react_loop
from agent_forge.agent.models import AgentConfig, AgentRun
from agent_forge.cli import _create_llm
from agent_forge.config import USER_CONFIG_DIR, load_config
from agent_forge.orchestration.events import EventBus
from agent_forge.orchestration.queue import InMemoryQueue, Task, TaskStatus
from agent_forge.orchestration.worker import Worker
from agent_forge.sandbox.docker import DockerSandbox
from agent_forge.service.models import (
    ErrorResponse,
    LogsResponse,
    ProofOfAuditReport,
    ReportProvenance,
    ReportStats,
    ReportTarget,
    RunArtifactRef,
    RunError,
    RunRequest,
    RunStatus,
    SeverityBreakdown,
    SeverityLevel,
    TargetRef,
)
from agent_forge.tools import create_default_registry

_REPORT_RELATIVE_PATH = Path(".agent-forge/report.json")


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

    @property
    def report_path(self) -> Path:
        """Return the expected machine report path for the run."""
        return self.workspace_dir / _REPORT_RELATIVE_PATH

    @property
    def run_dir(self) -> Path:
        """Return the persisted agent run directory."""
        return USER_CONFIG_DIR / "runs" / self.run_id


class HostedRunService:
    """In-process implementation of the versioned hosted run API."""

    def __init__(self, *, service_root: Path | None = None) -> None:
        self._service_root = service_root or USER_CONFIG_DIR / "service"
        self._config = load_config()
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
        self._validate_request(request)
        run_id = f"run_{uuid.uuid4().hex[:16]}"
        workspace_dir = self._materialize_source(run_id, request)
        prompt = self._build_task_prompt(request)
        agent_config = AgentConfig(
            model=self._config.agent.default_model,
            max_iterations=request.profile.max_iterations or self._config.agent.max_iterations,
            max_tokens_per_run=self._config.agent.max_tokens_per_run,
            temperature=self._config.agent.temperature,
        )
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
                task_description=prompt,
                repo_path=str(workspace_dir),
                config=agent_config,
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

    def get_report(self, run_id: str) -> ProofOfAuditReport:
        """Return the machine report for a completed run."""
        record = self._require_record(run_id)
        if record.error is not None:
            raise RuntimeError("run_failed")
        if not record.report_path.exists():
            raise RuntimeError("run_not_completed")
        payload = json.loads(record.report_path.read_text(encoding="utf-8"))
        if "schema_version" not in payload:
            payload["schema_version"] = "proof-of-audit-report-v1"
        if "run_id" not in payload:
            payload["run_id"] = run_id
        if "target" not in payload:
            payload["target"] = self._report_target(request=record.request).model_dump()
        if "provenance" not in payload:
            payload["provenance"] = ReportProvenance(
                profile_id=record.request.profile.id,
                source_digest=record.request.source.source_digest,
            ).model_dump()
        if "stats" not in payload:
            payload["stats"] = self._compute_stats(payload.get("findings", []))
        return ProofOfAuditReport.model_validate(payload)

    def get_logs(self, run_id: str) -> LogsResponse:
        """Return the persisted artifact references for a run."""
        record = self._require_record(run_id)
        artifacts = {
            "run_dir": str(record.run_dir),
            "run_json": str(record.run_dir / "run.json"),
            "messages_jsonl": str(record.run_dir / "messages.jsonl"),
            "events_jsonl": str(record.run_dir / "events.jsonl"),
            "summary_json": str(record.run_dir / "summary.json"),
            "report_json": str(record.report_path),
        }
        return LogsResponse(run_id=run_id, logs_url=None, inline=None, artifacts=artifacts)

    def _require_record(self, run_id: str) -> HostedRunRecord:
        record = self._records.get(run_id)
        if record is None:
            raise KeyError(run_id)
        return record

    def _validate_request(self, request: RunRequest) -> None:
        if request.profile.id != "proof-of-audit-solidity-v1":
            raise HTTPException(
                status_code=400,
                detail=ErrorResponse(
                    error=RunError(
                        code="unsupported_profile",
                        message=f"unsupported profile: {request.profile.id}",
                        retryable=False,
                    )
                ).model_dump(),
            )
        if request.source.kind not in {"archive_uri", "repository_uri", "local_path"}:
            raise HTTPException(
                status_code=400,
                detail=ErrorResponse(
                    error=RunError(
                        code="unsupported_source_kind",
                        message=f"unsupported source kind: {request.source.kind}",
                        retryable=False,
                    )
                ).model_dump(),
            )

    def _materialize_source(self, run_id: str, request: RunRequest) -> Path:
        uri_path = self._local_path_from_uri(request.source.uri)
        source_root = self._service_root / "sources" / run_id
        source_root.mkdir(parents=True, exist_ok=True)
        repo_root = source_root / "repo"

        if uri_path.is_dir():
            shutil.copytree(uri_path, repo_root)
            return repo_root

        if uri_path.suffix == ".sol":
            repo_root.mkdir(parents=True, exist_ok=True)
            shutil.copy2(uri_path, repo_root / uri_path.name)
            return repo_root

        if request.source.kind == "archive_uri" or uri_path.suffix == ".zip":
            repo_root.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(uri_path) as archive:
                self._extract_zip(archive, repo_root)
            return self._normalize_repo_root(repo_root)

        if uri_path.suffixes[-2:] == [".tar", ".gz"]:
            repo_root.mkdir(parents=True, exist_ok=True)
            with tarfile.open(uri_path, "r:gz") as archive:
                self._extract_tar_gz(archive, repo_root)
            return self._normalize_repo_root(repo_root)

        raise HTTPException(
            status_code=400,
            detail=ErrorResponse(
                error=RunError(
                    code="source_fetch_failed",
                    message=f"unsupported local source material: {uri_path}",
                    retryable=False,
                )
            ).model_dump(),
        )

    def _normalize_repo_root(self, root: Path) -> Path:
        children = [child for child in root.iterdir() if child.name != "__MACOSX"]
        if len(children) == 1 and children[0].is_dir():
            return children[0]
        return root

    def _local_path_from_uri(self, uri: str) -> Path:
        parsed = urlparse(uri)
        if parsed.scheme in {"", "file"}:
            raw_path = parsed.path if parsed.scheme == "file" else uri
            return Path(raw_path).expanduser().resolve()
        raise HTTPException(
            status_code=400,
            detail=ErrorResponse(
                error=RunError(
                    code="source_fetch_failed",
                    message=(
                        "only local and file:// URIs are supported in this service "
                        f"build: {uri}"
                    ),
                    retryable=False,
                )
            ).model_dump(),
        )

    def _extract_zip(self, archive: zipfile.ZipFile, destination: Path) -> None:
        for member in archive.infolist():
            target_path = self._safe_join(destination, member.filename)
            if member.is_dir():
                target_path.mkdir(parents=True, exist_ok=True)
                continue
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as src, target_path.open("wb") as dst:
                shutil.copyfileobj(src, dst)

    def _extract_tar_gz(self, archive: tarfile.TarFile, destination: Path) -> None:
        for member in archive.getmembers():
            target_path = self._safe_join(destination, member.name)
            if member.isdir():
                target_path.mkdir(parents=True, exist_ok=True)
                continue
            extracted = archive.extractfile(member)
            if extracted is None:
                continue
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with extracted, target_path.open("wb") as dst:
                shutil.copyfileobj(extracted, dst)

    def _safe_join(self, root: Path, relative_path: str) -> Path:
        target_path = (root / relative_path).resolve()
        root_path = root.resolve()
        if root_path == target_path or root_path in target_path.parents:
            return target_path
        msg = f"archive entry escapes destination: {relative_path}"
        raise HTTPException(
            status_code=400,
            detail=ErrorResponse(
                error=RunError(
                    code="source_fetch_failed",
                    message=msg,
                    retryable=False,
                )
            ).model_dump(),
        )

    def _build_task_prompt(self, request: RunRequest) -> str:
        entry_contract = request.source.entry_contract or "the primary contract"
        target = request.target.contract_address if request.target else None
        target_context = f" at deployed address {target}" if target else ""
        return (
            "Audit this smart contract repository for three issue classes: "
            "reentrancy, access control, and unchecked external calls. "
            f"Focus first on the contract named {entry_contract}{target_context}. "
            "Do not modify the source code except for writing a valid JSON report to "
            ".agent-forge/report.json. The report must use schema_version "
            "\"proof-of-audit-report-v1\" and include the fields run_id, summary, "
            "confidence, findings, stats, optional benchmark_id, optional target, and "
            "optional provenance. Each finding must include finding_id, title, severity, "
            "category, description, impact, recommendation, confidence, and optional "
            "detector, affected_function, source_path, start_line, end_line, and evidence_uri. "
            "If no finding is confirmed, write an empty findings array and matching stats."
        )

    def _make_task_runner(self) -> Any:
        async def _runner(task: Task) -> None:
            record = self._records[task.id]
            record.started_at = datetime.now(UTC)
            provider_name = self._config.agent.default_provider
            provider_cfg = self._config.providers.get(provider_name)
            if provider_cfg is None:
                record.error = RunError(
                    code="invalid_request",
                    message=f"unknown provider: {provider_name}",
                    retryable=False,
                )
                raise RuntimeError(record.error.message)
            api_key = os.environ.get(provider_cfg.api_key_env, "")
            if not api_key:
                record.error = RunError(
                    code="unauthorized",
                    message=f"missing provider API key env: {provider_cfg.api_key_env}",
                    retryable=False,
                )
                raise RuntimeError(record.error.message)

            llm = _create_llm(provider_name, api_key)
            tools = create_default_registry()
            sandbox = DockerSandbox()
            agent_run = AgentRun(
                task=task.task_description,
                repo_path=task.repo_path,
                config=task.config,
                id=task.id,
            )
            try:
                await sandbox.start(repo_path=task.repo_path)
                await react_loop(agent_run, llm, tools, sandbox, event_bus=self._event_bus)
                if not record.report_path.exists():
                    record.error = RunError(
                        code="report_generation_failed",
                        message="run completed without writing .agent-forge/report.json",
                        retryable=False,
                    )
                    raise RuntimeError(record.error.message)
                record.completed_at = datetime.now(UTC)
            except Exception as exc:
                if record.error is None:
                    record.error = RunError(
                        code="sandbox_execution_failed",
                        message=str(exc),
                        retryable=False,
                    )
                record.completed_at = datetime.now(UTC)
                raise
            finally:
                await sandbox.stop()
                await llm.close()

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

    def _report_target(self, *, request: RunRequest) -> ReportTarget:
        target = request.target or TargetRef()
        return ReportTarget(
            submission_kind=target.submission_kind,
            network=target.network,
            chain_id=target.chain_id,
            contract_address=target.contract_address,
            entry_contract=request.source.entry_contract,
        )

    def _compute_stats(self, findings: list[dict[str, Any]]) -> dict[str, Any]:
        counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        for finding in findings:
            severity = finding.get("severity")
            if severity in counts:
                counts[severity] += 1
        max_severity: SeverityLevel | None = None
        for severity in ("critical", "high", "medium", "low"):
            if counts[severity] > 0:
                max_severity = cast("SeverityLevel", severity)
                break
        return ReportStats(
            finding_count=len(findings),
            max_severity=max_severity,
            severity_breakdown=SeverityBreakdown(
                critical=counts["critical"],
                high=counts["high"],
                medium=counts["medium"],
                low=counts["low"],
            ),
        ).model_dump()


def create_app(*, service_root: Path | None = None) -> FastAPI:  # noqa: C901
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

    @app.get("/v1/runs/{run_id}/report", response_model=ProofOfAuditReport)
    async def get_report(run_id: str) -> ProofOfAuditReport:
        try:
            return service.get_report(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"unknown run id: {run_id}") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/v1/runs/{run_id}/logs", response_model=LogsResponse)
    async def get_logs(run_id: str) -> LogsResponse:
        try:
            return service.get_logs(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"unknown run id: {run_id}") from exc

    return app
