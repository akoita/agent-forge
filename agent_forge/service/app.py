"""FastAPI app for the hosted agent-forge service contract."""

from __future__ import annotations

import hmac
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
from typing import Any, NoReturn, cast
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from agent_forge.agent.core import react_loop
from agent_forge.agent.models import AgentConfig, AgentRun
from agent_forge.cli import _create_llm
from agent_forge.config import USER_CONFIG_DIR, ForgeConfig, load_config
from agent_forge.orchestration.events import EventBus
from agent_forge.orchestration.queue import InMemoryQueue, Task, TaskStatus
from agent_forge.orchestration.worker import Worker
from agent_forge.sandbox.docker import DockerSandbox
from agent_forge.service.models import (
    ErrorResponse,
    HealthResponse,
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
from agent_forge.service.security import ServiceClientPolicy, load_client_registry
from agent_forge.tools import create_default_registry

_REPORT_RELATIVE_PATH = Path(".agent-forge/report.json")


@dataclass
class HostedRunRecord:
    """Persistent in-memory record for a hosted run."""

    run_id: str
    request: RunRequest
    workspace_dir: Path
    created_at: datetime
    request_origin: str | None = None
    user_agent: str | None = None
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

    def __init__(
        self,
        *,
        service_root: Path | None = None,
        config: ForgeConfig | None = None,
    ) -> None:
        self._config = config or load_config()
        self._service_root = service_root or Path(self._config.service.root_dir).expanduser()
        self._queue = InMemoryQueue()
        self._event_bus = EventBus()
        self._records: dict[str, HostedRunRecord] = {}
        self._client_policies: dict[str, ServiceClientPolicy] = {}
        self._audit_log_path = self._service_root / "audit" / "events.jsonl"
        self._worker = Worker(
            queue=self._queue,
            event_bus=self._event_bus,
            task_runner=self._make_task_runner(),
            poll_interval=0.05,
        )

    async def start(self) -> None:
        """Start the background worker for hosted runs."""
        self._service_root.mkdir(parents=True, exist_ok=True)
        clients_path = Path(self._config.service.clients_path).expanduser()
        self._client_policies = load_client_registry(clients_path)
        await self._worker.start()

    async def stop(self) -> None:
        """Stop the background worker for hosted runs."""
        await self._worker.stop()

    async def create_run(
        self,
        request: RunRequest,
        *,
        headers: dict[str, str] | None = None,
        request_origin: str | None = None,
        user_agent: str | None = None,
    ) -> RunStatus:
        """Accept a new hosted run request and enqueue it."""
        client_service_id, client_policy = self._authenticate_headers(
            headers,
            request_origin=request_origin,
            user_agent=user_agent,
        )
        self._authorize_submission(
            request=request,
            client_service_id=client_service_id,
            client_policy=client_policy,
            request_origin=request_origin,
            user_agent=user_agent,
        )
        self._validate_request(request)
        self._enforce_quotas(
            client_service_id,
            request=request,
            request_origin=request_origin,
            user_agent=user_agent,
        )
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
            request_origin=request_origin,
            user_agent=user_agent,
        )
        self._records[run_id] = record
        self._append_audit_event(
            event="run.accepted",
            client_service_id=request.client.service_id,
            run_id=run_id,
            request_id=request.client.request_id,
            profile_id=request.profile.id,
            submitted_at=record.created_at,
            final_status="accepted",
            request_origin=request_origin,
            user_agent=user_agent,
        )
        await self._queue.enqueue(
            Task(
                id=run_id,
                task_description=prompt,
                repo_path=str(workspace_dir),
                config=agent_config,
            )
        )
        return self._build_status(record, status="accepted")

    async def get_status(
        self,
        run_id: str,
        *,
        headers: dict[str, str] | None = None,
        request_origin: str | None = None,
        user_agent: str | None = None,
    ) -> RunStatus:
        """Return the current lifecycle document for a run."""
        client_service_id, _ = self._authenticate_headers(
            headers,
            request_origin=request_origin,
            user_agent=user_agent,
        )
        record = self._records.get(run_id)
        if record is None:
            raise KeyError(run_id)
        self._authorize_run_access(
            record,
            client_service_id=client_service_id,
            request_origin=request_origin,
            user_agent=user_agent,
        )
        queue_status = await self._queue.get_status(run_id)
        return self._build_status(record, status=self._map_status(queue_status))

    def get_report(
        self,
        run_id: str,
        *,
        headers: dict[str, str] | None = None,
        request_origin: str | None = None,
        user_agent: str | None = None,
    ) -> ProofOfAuditReport:
        """Return the machine report for a completed run."""
        client_service_id, _ = self._authenticate_headers(
            headers,
            request_origin=request_origin,
            user_agent=user_agent,
        )
        record = self._require_record(run_id)
        self._authorize_run_access(
            record,
            client_service_id=client_service_id,
            request_origin=request_origin,
            user_agent=user_agent,
        )
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

    def get_logs(
        self,
        run_id: str,
        *,
        headers: dict[str, str] | None = None,
        request_origin: str | None = None,
        user_agent: str | None = None,
    ) -> LogsResponse:
        """Return the persisted artifact references for a run."""
        client_service_id, _ = self._authenticate_headers(
            headers,
            request_origin=request_origin,
            user_agent=user_agent,
        )
        record = self._require_record(run_id)
        self._authorize_run_access(
            record,
            client_service_id=client_service_id,
            request_origin=request_origin,
            user_agent=user_agent,
        )
        artifacts = {
            "run_dir": str(record.run_dir),
            "run_json": str(record.run_dir / "run.json"),
            "messages_jsonl": str(record.run_dir / "messages.jsonl"),
            "events_jsonl": str(record.run_dir / "events.jsonl"),
            "summary_json": str(record.run_dir / "summary.json"),
            "report_json": str(record.report_path),
        }
        return LogsResponse(run_id=run_id, logs_url=None, inline=None, artifacts=artifacts)

    def _authenticate_headers(
        self,
        headers: dict[str, str] | None,
        *,
        request_origin: str | None = None,
        user_agent: str | None = None,
    ) -> tuple[str | None, ServiceClientPolicy | None]:
        if not self._config.service.auth_enabled:
            return None, None

        header_name = self._config.service.api_key_header
        normalized_headers = {key.lower(): value for key, value in (headers or {}).items()}
        provided_key = normalized_headers.get(header_name.lower())
        if not provided_key:
            self._deny(
                status_code=401,
                code="unauthorized",
                message=f"missing service API key header: {header_name}",
                request_origin=request_origin,
                user_agent=user_agent,
            )

        for client_service_id, policy in self._client_policies.items():
            expected_key = os.environ.get(policy.api_key_env, "")
            if expected_key and hmac.compare_digest(provided_key, expected_key):
                return client_service_id, policy

        self._deny(
            status_code=401,
            code="unauthorized",
            message="invalid service API key",
            request_origin=request_origin,
            user_agent=user_agent,
        )
        raise AssertionError("unreachable")

    def _authorize_submission(
        self,
        *,
        request: RunRequest,
        client_service_id: str | None,
        client_policy: ServiceClientPolicy | None,
        request_origin: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        if client_service_id is None or client_policy is None:
            return

        if request.client.service_id != client_service_id:
            self._append_audit_event(
                event="run.denied",
                client_service_id=client_service_id,
                request_id=request.client.request_id,
                profile_id=request.profile.id,
                submitted_at=datetime.now(UTC),
                final_status="rejected",
                reason="client service_id does not match authenticated API key",
                request_origin=request_origin,
                user_agent=user_agent,
            )
            self._deny(
                status_code=403,
                code="unauthorized",
                message="request client does not match authenticated service client",
                request_origin=request_origin,
                user_agent=user_agent,
            )

        if request.profile.id not in client_policy.allowed_profiles:
            self._append_audit_event(
                event="run.denied",
                client_service_id=client_service_id,
                request_id=request.client.request_id,
                profile_id=request.profile.id,
                submitted_at=datetime.now(UTC),
                final_status="rejected",
                reason=f"profile not allowed: {request.profile.id}",
                request_origin=request_origin,
                user_agent=user_agent,
            )
            self._deny(
                status_code=403,
                code="policy_denied",
                message=f"profile not allowed for client: {request.profile.id}",
                request_origin=request_origin,
                user_agent=user_agent,
            )

        if request.profile.report_schema not in client_policy.allowed_report_schemas:
            self._append_audit_event(
                event="run.denied",
                client_service_id=client_service_id,
                request_id=request.client.request_id,
                profile_id=request.profile.id,
                submitted_at=datetime.now(UTC),
                final_status="rejected",
                reason=f"report schema not allowed: {request.profile.report_schema}",
                request_origin=request_origin,
                user_agent=user_agent,
            )
            self._deny(
                status_code=403,
                code="policy_denied",
                message=(
                    "report schema not allowed for client: "
                    f"{request.profile.report_schema}"
                ),
                request_origin=request_origin,
                user_agent=user_agent,
            )

        if request.source.kind not in client_policy.allowed_source_kinds:
            self._append_audit_event(
                event="run.denied",
                client_service_id=client_service_id,
                request_id=request.client.request_id,
                profile_id=request.profile.id,
                submitted_at=datetime.now(UTC),
                final_status="rejected",
                reason=f"source kind not allowed: {request.source.kind}",
                request_origin=request_origin,
                user_agent=user_agent,
            )
            self._deny(
                status_code=403,
                code="policy_denied",
                message=f"source kind not allowed for client: {request.source.kind}",
                request_origin=request_origin,
                user_agent=user_agent,
            )

        local_path_allowed = (
            self._config.service.allow_local_path_sources and client_policy.allow_local_path
        )
        if request.source.kind == "local_path" and not local_path_allowed:
            self._append_audit_event(
                event="run.denied",
                client_service_id=client_service_id,
                request_id=request.client.request_id,
                profile_id=request.profile.id,
                submitted_at=datetime.now(UTC),
                final_status="rejected",
                reason="local_path sources disabled by service policy",
                request_origin=request_origin,
                user_agent=user_agent,
            )
            self._deny(
                status_code=403,
                code="policy_denied",
                message="local_path sources are disabled for hosted clients",
                request_origin=request_origin,
                user_agent=user_agent,
            )

        source_size_bytes = self._source_size_bytes(request.source.uri)
        if source_size_bytes > self._config.service.max_source_size_bytes:
            self._append_audit_event(
                event="run.denied",
                client_service_id=client_service_id,
                request_id=request.client.request_id,
                profile_id=request.profile.id,
                submitted_at=datetime.now(UTC),
                final_status="rejected",
                reason=f"source exceeds max size: {source_size_bytes}",
                request_origin=request_origin,
                user_agent=user_agent,
            )
            self._deny(
                status_code=403,
                code="policy_denied",
                message="source exceeds configured size limit",
                request_origin=request_origin,
                user_agent=user_agent,
            )

    def _authorize_run_access(
        self,
        record: HostedRunRecord,
        *,
        client_service_id: str | None,
        request_origin: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        if client_service_id is None:
            return
        if record.request.client.service_id == client_service_id:
            return
        self._append_audit_event(
            event="run.denied",
            client_service_id=client_service_id,
            run_id=record.run_id,
            request_id=record.request.client.request_id,
            profile_id=record.request.profile.id,
            submitted_at=record.created_at,
            final_status="rejected",
            reason="attempted to access another client's run",
            request_origin=request_origin,
            user_agent=user_agent,
        )
        self._deny(
            status_code=403,
            code="policy_denied",
            message="run does not belong to authenticated client",
            request_origin=request_origin,
            user_agent=user_agent,
        )

    def _enforce_quotas(
        self,
        client_service_id: str | None,
        *,
        request: RunRequest,
        request_origin: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        if client_service_id is None:
            return
        policy = self._client_policies.get(client_service_id)
        if policy is None:
            self._deny(
                status_code=401,
                code="unauthorized",
                message=f"no client policy configured for: {client_service_id}",
                request_origin=request_origin,
                user_agent=user_agent,
            )

        now = datetime.now(UTC)
        active_runs = 0
        daily_runs = 0
        for record in self._records.values():
            if record.request.client.service_id != client_service_id:
                continue
            if record.completed_at is None:
                active_runs += 1
            if (now - record.created_at).total_seconds() <= 86_400:
                daily_runs += 1

        if active_runs >= policy.max_active_runs:
            self._append_audit_event(
                event="run.denied",
                client_service_id=client_service_id,
                request_id=request.client.request_id,
                profile_id=request.profile.id,
                submitted_at=datetime.now(UTC),
                final_status="rejected",
                reason=f"max active runs exceeded: {active_runs}",
                request_origin=request_origin,
                user_agent=user_agent,
            )
            self._deny(
                status_code=429,
                code="quota_exceeded",
                message="client has reached the active run limit",
                request_origin=request_origin,
                user_agent=user_agent,
            )

        if daily_runs >= policy.max_runs_per_day:
            self._append_audit_event(
                event="run.denied",
                client_service_id=client_service_id,
                request_id=request.client.request_id,
                profile_id=request.profile.id,
                submitted_at=datetime.now(UTC),
                final_status="rejected",
                reason=f"max daily runs exceeded: {daily_runs}",
                request_origin=request_origin,
                user_agent=user_agent,
            )
            self._deny(
                status_code=429,
                code="quota_exceeded",
                message="client has reached the daily run limit",
                request_origin=request_origin,
                user_agent=user_agent,
            )

    def _source_size_bytes(self, source_uri: str) -> int:
        path = self._local_path_from_uri(source_uri)
        if path.is_dir():
            return sum(
                child.stat().st_size
                for child in path.rglob("*")
                if child.is_file()
            )
        return path.stat().st_size

    def _append_audit_event(
        self,
        *,
        event: str,
        client_service_id: str | None,
        run_id: str | None = None,
        request_id: str | None = None,
        profile_id: str | None = None,
        submitted_at: datetime | None = None,
        final_status: str | None = None,
        reason: str | None = None,
        request_origin: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event": event,
            "client_service_id": client_service_id,
            "run_id": run_id,
            "request_id": request_id,
            "profile_id": profile_id,
            "submitted_at": submitted_at.isoformat() if submitted_at else None,
            "final_status": final_status,
            "reason": reason,
            "request_origin": request_origin,
            "user_agent": user_agent,
        }
        self._audit_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._audit_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")

    def _deny(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        request_origin: str | None = None,
        user_agent: str | None = None,
    ) -> NoReturn:
        _ = request_origin, user_agent
        raise HTTPException(
            status_code=status_code,
            detail=ErrorResponse(
                error=RunError(
                    code=cast("Any", code),
                    message=message,
                    retryable=False,
                )
            ).model_dump(),
        )

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
                self._append_audit_event(
                    event="run.completed",
                    client_service_id=record.request.client.service_id,
                    run_id=record.run_id,
                    request_id=record.request.client.request_id,
                    profile_id=record.request.profile.id,
                    submitted_at=record.created_at,
                    final_status="completed",
                    request_origin=record.request_origin,
                    user_agent=record.user_agent,
                )
            except Exception as exc:
                if record.error is None:
                    record.error = RunError(
                        code="sandbox_execution_failed",
                        message=str(exc),
                        retryable=False,
                    )
                record.completed_at = datetime.now(UTC)
                self._append_audit_event(
                    event="run.failed",
                    client_service_id=record.request.client.service_id,
                    run_id=record.run_id,
                    request_id=record.request.client.request_id,
                    profile_id=record.request.profile.id,
                    submitted_at=record.created_at,
                    final_status="failed",
                    reason=record.error.message,
                    request_origin=record.request_origin,
                    user_agent=record.user_agent,
                )
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


def create_app(  # noqa: C901
    *,
    service_root: Path | None = None,
    config: ForgeConfig | None = None,
) -> FastAPI:
    """Create the hosted service ASGI app."""
    resolved_config = config or load_config()
    service = HostedRunService(service_root=service_root, config=resolved_config)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> Any:
        await service.start()
        try:
            yield
        finally:
            await service.stop()

    app = FastAPI(title="agent-forge hosted service", version="0.1.0", lifespan=lifespan)
    app.state.service = service

    def _request_origin(http_request: Request) -> str | None:
        forwarded_for = http_request.headers.get("x-forwarded-for")
        if forwarded_for:
            return forwarded_for.split(",", 1)[0].strip() or None
        if http_request.client is not None:
            return http_request.client.host
        return None

    @app.middleware("http")
    async def require_hosted_auth(http_request: Request, call_next: Any) -> Any:
        if not resolved_config.service.auth_enabled:
            return await call_next(http_request)
        if not http_request.url.path.startswith("/v1/runs"):
            return await call_next(http_request)

        try:
            service._authenticate_headers(
                dict(http_request.headers),
                request_origin=_request_origin(http_request),
                user_agent=http_request.headers.get("user-agent"),
            )
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

        return await call_next(http_request)

    @app.get(resolved_config.service.healthcheck_path, response_model=HealthResponse)
    async def healthcheck() -> HealthResponse:
        return HealthResponse(
            status="ok",
            service_root=str(service._service_root),
            queue_backend=resolved_config.queue.backend,
            sandbox_image=resolved_config.sandbox.image,
        )

    @app.post("/v1/runs", response_model=RunStatus, status_code=202)
    async def create_run(request: RunRequest, http_request: Request) -> RunStatus:
        return await service.create_run(
            request,
            headers=dict(http_request.headers),
            request_origin=_request_origin(http_request),
            user_agent=http_request.headers.get("user-agent"),
        )

    @app.get("/v1/runs/{run_id}", response_model=RunStatus)
    async def get_run(run_id: str, http_request: Request) -> RunStatus:
        try:
            return await service.get_status(
                run_id,
                headers=dict(http_request.headers),
                request_origin=_request_origin(http_request),
                user_agent=http_request.headers.get("user-agent"),
            )
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
    async def get_report(run_id: str, http_request: Request) -> ProofOfAuditReport:
        try:
            return service.get_report(
                run_id,
                headers=dict(http_request.headers),
                request_origin=_request_origin(http_request),
                user_agent=http_request.headers.get("user-agent"),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"unknown run id: {run_id}") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/v1/runs/{run_id}/logs", response_model=LogsResponse)
    async def get_logs(run_id: str, http_request: Request) -> LogsResponse:
        try:
            return service.get_logs(
                run_id,
                headers=dict(http_request.headers),
                request_origin=_request_origin(http_request),
                user_agent=http_request.headers.get("user-agent"),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"unknown run id: {run_id}") from exc

    return app
