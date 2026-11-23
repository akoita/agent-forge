"""FastAPI app for the hosted agent-forge service contract."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import importlib
import json
import logging
import os
import shutil
import tarfile
import uuid
import zipfile
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, cast
from urllib.parse import ParseResult, urlparse

try:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import JSONResponse, StreamingResponse
except ImportError as exc:
    msg = (
        "Hosted service requires optional dependencies. "
        "Install with: pip install 'agent-forge[service]'"
    )
    raise ImportError(msg) from exc

from agent_forge import __version__
from agent_forge.agent.budget import CancellationToken
from agent_forge.agent.contracts import (
    CompletionContract,
    ContractEvaluation,
    JsonArtifactClause,
    artifact_report_contract,
    evaluate_json_artifact_raw,
    resolve_completion_contract,
)
from agent_forge.agent.core import react_loop
from agent_forge.agent.models import AgentConfig, AgentRun
from agent_forge.agent.runner import run_agent_task
from agent_forge.config import USER_CONFIG_DIR, ForgeConfig, load_config
from agent_forge.llm.factory import create_provider, provider_names
from agent_forge.orchestration.event_stream import (
    JournalEventSink,
    RunEventStream,
    StreamEventPage,
)
from agent_forge.orchestration.events import EventBus
from agent_forge.orchestration.queue import InMemoryQueue, Task, TaskStatus
from agent_forge.orchestration.worker import Worker
from agent_forge.profiles import AgentProfile, load_profiles
from agent_forge.sandbox.factory import create_sandbox
from agent_forge.service.models import (
    ErrorCode,
    ErrorResponse,
    HealthResponse,
    LogsResponse,
    ReportProvenance,
    ReportTarget,
    RunArtifactRef,
    RunError,
    RunReport,
    RunRequest,
    RunStatus,
    TargetRef,
)
from agent_forge.service.security import ServiceClientPolicy, load_client_registry
from agent_forge.service.store import (
    TERMINAL_RUN_STATUSES,
    HostedRunStore,
    IdempotencyConflictError,
    PersistedRun,
)
from agent_forge.tools import create_default_registry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from agent_forge.orchestration.event_stream import StreamEvent
    from agent_forge.sandbox.base import Sandbox

_logger = logging.getLogger(__name__)
_REPORT_RELATIVE_PATH = Path(".agent-forge/report.json")

_REPORT_REQUIRED_FIELDS = {"schema_version", "run_id", "summary"}


@dataclass
class HostedRunRecord:
    """Record for a hosted run mirrored from durable persistence."""

    run_id: str
    request: RunRequest
    workspace_dir: Path
    created_at: datetime
    owner_service_id: str | None = None
    idempotency_key: str | None = None
    status: str = "accepted"
    request_origin: str | None = None
    user_agent: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: RunError | None = None
    report_json: str | None = None

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
        runs_dir: Path | None = None,
        config: ForgeConfig | None = None,
        instance_id: str | None = None,
        persona: str | None = None,
    ) -> None:
        self._config = config or load_config()
        base_root = service_root or Path(self._config.service.root_dir).expanduser()
        self._instance_id = instance_id
        self._persona_id = persona
        # Isolate workspace per instance when instance_id is set
        self._service_root = base_root / instance_id if instance_id else base_root
        self._runs_dir = Path(runs_dir) if runs_dir is not None else (USER_CONFIG_DIR / "runs")
        self._queue = InMemoryQueue()
        self._event_bus = EventBus()
        self._event_sink = JournalEventSink(base_dir=self._runs_dir)
        self._store = HostedRunStore(self._service_root / "runs.db")
        self._store_initialized = False
        self._worker_id = f"{instance_id or 'service'}-{uuid.uuid4().hex[:8]}"
        self._records: dict[str, HostedRunRecord] = {}
        self._cancel_tokens: dict[str, CancellationToken] = {}
        self._active_sandboxes: dict[str, Sandbox] = {}
        self._client_policies: dict[str, ServiceClientPolicy] = {}
        self._profile_registry: dict[str, AgentProfile] = {}
        self._resolved_persona: AgentProfile | None = None
        self._access_log_path = self._service_root / "audit" / "events.jsonl"  # boundary-ok
        self._worker = Worker(
            queue=self._queue,
            event_bus=self._event_bus,
            task_runner=self._make_task_runner(),
            poll_interval=0.05,
        )
        self._recovery_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start persistence hydration, dispatch recovery, and the background worker."""
        self._service_root.mkdir(parents=True, exist_ok=True)
        self._ensure_store_initialized()
        self._hydrate_records_from_store()
        clients_path = Path(self._config.service.clients_path).expanduser()
        self._client_policies = load_client_registry(clients_path)
        # Load core + plugin profiles (auto-discover from plugins/ directory)
        plugin_profile_dirs = list(
            (Path(__file__).resolve().parents[2] / "plugins").glob("*/profiles")
        )
        self._profile_registry = load_profiles(plugin_profile_dirs if plugin_profile_dirs else None)
        # Validate and resolve persona if set
        if self._persona_id is not None:
            if self._persona_id not in self._profile_registry:
                available = ", ".join(sorted(self._profile_registry)) or "(none)"
                msg = (
                    f"unknown persona profile: '{self._persona_id}'. "
                    f"Available profiles: {available}"
                )
                raise ValueError(msg)
            self._resolved_persona = self._profile_registry[self._persona_id]
        await self._event_sink.attach(self._event_bus)
        await self.recover_runs(reclaim_all_running=True)
        await self._worker.start()
        self._recovery_task = asyncio.create_task(self._recovery_loop())

    async def stop(self) -> None:
        """Stop background recovery and worker loops cleanly."""
        if self._recovery_task is not None:
            self._recovery_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._recovery_task
            self._recovery_task = None
        try:
            await self._worker.stop()
        finally:
            await self._event_sink.detach(self._event_bus)

    async def create_run(
        self,
        request: RunRequest,
        *,
        headers: dict[str, str] | None = None,
        request_origin: str | None = None,
        user_agent: str | None = None,
    ) -> RunStatus:
        """Accept a new hosted run request, persist it durably, and enqueue dispatch."""
        self._ensure_store_initialized()
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

        owner_service_id = client_service_id or request.client.service_id
        owner_key = owner_service_id or request.client.name
        idempotency_key = self._resolve_idempotency_key(request, headers)
        request_fingerprint = self._request_fingerprint(request)
        if idempotency_key is not None:
            existing = self._store.get_by_idempotency_key(owner_key, idempotency_key)
            if existing is not None:
                if existing.request_fingerprint != request_fingerprint:
                    self._raise_idempotency_conflict()
                existing_record = self._hydrate_record(existing)
                return await self._status_for_record(existing_record)

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
        created_at = datetime.now(UTC)
        try:
            persisted, created = self._store.create_or_get(
                run_id=run_id,
                owner_service_id=owner_service_id,
                owner_key=owner_key,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
                request_json=request.model_dump_json(),
                workspace_dir=workspace_dir,
                task_prompt=prompt,
                agent_config_json=self._serialize_agent_config(agent_config),
                request_origin=request_origin,
                user_agent=user_agent,
                created_at=created_at,
            )
        except IdempotencyConflictError:
            shutil.rmtree(workspace_dir, ignore_errors=True)
            self._raise_idempotency_conflict()

        if not created:
            shutil.rmtree(workspace_dir, ignore_errors=True)
            existing_record = self._hydrate_record(persisted)
            return await self._status_for_record(existing_record)

        record = self._hydrate_record(persisted)
        self._append_access_event(
            event="run.accepted",
            client_service_id=owner_service_id,
            run_id=run_id,
            request_id=request.client.request_id,
            profile_id=request.profile.id,
            submitted_at=record.created_at,
            final_status="accepted",
            request_origin=request_origin,
            user_agent=user_agent,
        )
        task = Task(
            id=run_id,
            task_description=prompt,
            repo_path=str(workspace_dir),
            config=agent_config,
        )
        await self._dispatch_new_task(record, task)
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
        record = self._require_record(run_id)
        self._authorize_run_access(
            record,
            client_service_id=client_service_id,
            request_origin=request_origin,
            user_agent=user_agent,
        )
        return await self._status_for_record(record)

    async def cancel_run(
        self,
        run_id: str,
        *,
        headers: dict[str, str] | None = None,
        request_origin: str | None = None,
        user_agent: str | None = None,
    ) -> RunStatus:
        """Cancel a queued or active run, stop its sandbox, and clean up workspace."""
        client_service_id, _ = self._authenticate_headers(
            headers,
            request_origin=request_origin,
            user_agent=user_agent,
        )
        self._ensure_store_initialized()
        record = self._require_record(run_id)
        self._authorize_run_access(
            record,
            client_service_id=client_service_id,
            request_origin=request_origin,
            user_agent=user_agent,
        )

        token = self._cancel_tokens.get(run_id)
        if token is not None:
            token.cancel("Cancelled by operator")

        now = datetime.now(UTC)
        persisted, transitioned = self._store.cancel_run(run_id, completed_at=now)
        if persisted is not None:
            record = self._hydrate_record(persisted)
        elif record.status not in TERMINAL_RUN_STATUSES:
            record.status = "cancelled"
            record.completed_at = now
            transitioned = True

        with contextlib.suppress(KeyError):
            await self._queue.cancel(run_id)

        active_sandbox = self._active_sandboxes.pop(run_id, None)
        if active_sandbox is not None:
            with contextlib.suppress(Exception):
                await active_sandbox.stop()

        self._cleanup_ephemeral_workspace(record.workspace_dir)

        if transitioned:
            self._append_access_event(
                event="run.cancelled",
                client_service_id=record.owner_service_id or record.request.client.service_id,
                run_id=record.run_id,
                request_id=record.request.client.request_id,
                profile_id=record.request.profile.id,
                submitted_at=record.created_at,
                final_status="cancelled",
                reason="cancelled via API",
                request_origin=request_origin,
                user_agent=user_agent,
            )

        return await self._status_for_record(record)

    def _cleanup_ephemeral_workspace(self, workspace_dir: Path) -> None:
        """Remove staged workspace only when strictly inside service sources root."""
        sources_root = (self._service_root / "sources").resolve()
        try:
            resolved = workspace_dir.resolve()
        except OSError:
            return
        if not resolved.is_relative_to(sources_root) or resolved == sources_root:
            return
        shutil.rmtree(resolved, ignore_errors=True)
        # Also remove the run container directory under sources/<run_id> if empty/owned
        parent = resolved.parent
        if parent != sources_root and parent.is_relative_to(sources_root):
            shutil.rmtree(parent, ignore_errors=True)

    def get_report(
        self,
        run_id: str,
        *,
        headers: dict[str, str] | None = None,
        request_origin: str | None = None,
        user_agent: str | None = None,
    ) -> RunReport:
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
        if record.status == "cancelled":
            raise RuntimeError("run_cancelled")
        if record.error is not None or record.status == "failed":
            raise RuntimeError("run_failed")
        if record.report_json is not None:
            payload = json.loads(record.report_json)
        elif record.report_path.exists():
            payload = json.loads(record.report_path.read_text(encoding="utf-8"))
        else:
            raise RuntimeError("run_not_completed")
        if "schema_version" not in payload:
            payload["schema_version"] = record.request.profile.report_schema
        if "run_id" not in payload:
            payload["run_id"] = run_id
        if "target" not in payload:
            payload["target"] = self._report_target(request=record.request).model_dump()
        if "provenance" not in payload:
            payload["provenance"] = ReportProvenance(
                profile_id=record.request.profile.id,
                source_digest=record.request.source.source_digest,
            ).model_dump()
        return RunReport.model_validate(payload)

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
            "stream_jsonl": str(self._runs_dir / run_id / "stream.jsonl"),
            "summary_json": str(record.run_dir / "summary.json"),
            "report_json": str(record.report_path),
        }
        return LogsResponse(run_id=run_id, logs_url=None, inline=None, artifacts=artifacts)

    def _authorize_stream_access(
        self,
        run_id: str,
        *,
        headers: dict[str, str] | None = None,
        request_origin: str | None = None,
        user_agent: str | None = None,
    ) -> HostedRunRecord:
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
        return record

    def get_events(
        self,
        run_id: str,
        *,
        after_seq: int = -1,
        limit: int = 100,
        headers: dict[str, str] | None = None,
        request_origin: str | None = None,
        user_agent: str | None = None,
    ) -> StreamEventPage:
        """Return a paginated slice of typed lifecycle events for a run."""
        self._authorize_stream_access(
            run_id,
            headers=headers,
            request_origin=request_origin,
            user_agent=user_agent,
        )
        return RunEventStream(run_id, base_dir=self._runs_dir).read_page(
            after_seq=after_seq,
            limit=limit,
        )

    def stream_events(
        self,
        run_id: str,
        *,
        after_seq: int = -1,
        page_size: int = 100,
        poll_interval: float = 0.05,
        headers: dict[str, str] | None = None,
        request_origin: str | None = None,
        user_agent: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Authorize access synchronously and return a live event iterator."""
        self._authorize_stream_access(
            run_id,
            headers=headers,
            request_origin=request_origin,
            user_agent=user_agent,
        )
        return RunEventStream(run_id, base_dir=self._runs_dir).follow(
            after_seq=after_seq,
            page_size=page_size,
            poll_interval=poll_interval,
        )

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
            self._append_access_event(
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
            self._append_access_event(
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
            self._append_access_event(
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
                message=(f"report schema not allowed for client: {request.profile.report_schema}"),
                request_origin=request_origin,
                user_agent=user_agent,
            )

        if request.source.kind not in client_policy.allowed_source_kinds:
            self._append_access_event(
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
            self._append_access_event(
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
            self._append_access_event(
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
        owner = record.owner_service_id or record.request.client.service_id
        if owner == client_service_id:
            return
        self._append_access_event(
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

        active_runs, daily_runs = self._owner_usage_counts(client_service_id)

        if active_runs >= policy.max_active_runs:
            self._append_access_event(
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
            self._append_access_event(
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

    def _owner_usage_counts(self, client_service_id: str) -> tuple[int, int]:
        now = datetime.now(UTC)
        active_runs = 0
        daily_runs = 0
        seen_ids: set[str] = set()
        for record in self._records.values():
            owner = record.owner_service_id or record.request.client.service_id
            if owner != client_service_id:
                continue
            seen_ids.add(record.run_id)
            is_active = record.completed_at is None and record.status not in TERMINAL_RUN_STATUSES
            active_runs += int(is_active)
            daily_runs += int((now - record.created_at).total_seconds() <= 86_400)
        if not self._store_initialized:
            return active_runs, daily_runs
        for persisted in self._store.list_all():
            if persisted.run_id in seen_ids or persisted.owner_service_id != client_service_id:
                continue
            is_active = (
                persisted.completed_at is None and persisted.status not in TERMINAL_RUN_STATUSES
            )
            active_runs += int(is_active)
            daily_runs += int((now - persisted.created_at).total_seconds() <= 86_400)
        return active_runs, daily_runs

    def _source_size_bytes(self, source_uri: str) -> int:
        parsed = urlparse(source_uri)
        if parsed.scheme == "gs":
            return self._gcs_source_size_bytes(parsed)
        path = self._local_path_from_uri(source_uri)
        if path.is_dir():
            return sum(child.stat().st_size for child in path.rglob("*") if child.is_file())
        return path.stat().st_size

    def _append_access_event(
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
        self._access_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._access_log_path.open("a", encoding="utf-8") as handle:
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
        record = self._sync_record_from_store(run_id)
        if record is None:
            raise KeyError(run_id)
        return record

    def _validate_request(self, request: RunRequest) -> None:
        if request.profile.id not in self._profile_registry:
            available = ", ".join(sorted(self._profile_registry)) or "(none)"
            raise HTTPException(
                status_code=400,
                detail=ErrorResponse(
                    error=RunError(
                        code="unsupported_profile",
                        message=(
                            f"unsupported profile: {request.profile.id}. Available: {available}"
                        ),
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
        source_root = self._service_root / "sources" / run_id
        source_root.mkdir(parents=True, exist_ok=True)
        uri_path = self._materialize_source_uri(request.source.uri, source_root / "_downloads")
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
        self._raise_unsupported_source_uri(uri)
        raise AssertionError("unreachable")

    def _materialize_source_uri(self, uri: str, download_root: Path) -> Path:
        parsed = urlparse(uri)
        if parsed.scheme in {"", "file"}:
            return self._local_path_from_uri(uri)
        if parsed.scheme == "gs":
            return self._download_gcs_uri(parsed, download_root)
        self._raise_unsupported_source_uri(uri)
        raise AssertionError("unreachable")

    def _gcs_source_size_bytes(self, parsed: ParseResult) -> int:
        bucket_name, object_name = self._parse_gcs_uri(parsed)
        try:
            storage = importlib.import_module("google.cloud.storage")
        except ImportError as exc:
            self._raise_source_fetch_failed(
                "gs:// URIs require google-cloud-storage support. "
                "Install with: pip install 'agent-forge[gcs]'"
            )
            raise AssertionError("unreachable") from exc

        blob = storage.Client().bucket(bucket_name).get_blob(object_name)
        if blob is None or blob.size is None:
            self._raise_source_fetch_failed(
                f"GCS object not found: gs://{bucket_name}/{object_name}"
            )
        return int(blob.size)

    def _download_gcs_uri(self, parsed: ParseResult, download_root: Path) -> Path:
        bucket_name, object_name = self._parse_gcs_uri(parsed)
        filename = Path(object_name).name
        destination = (download_root / filename).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            storage = importlib.import_module("google.cloud.storage")
            google_exceptions = importlib.import_module("google.api_core.exceptions")
        except ImportError as exc:
            self._raise_source_fetch_failed(
                "gs:// URIs require google-cloud-storage support. "
                "Install with: pip install 'agent-forge[gcs]'"
            )
            raise AssertionError("unreachable") from exc

        blob = storage.Client().bucket(bucket_name).blob(object_name)
        try:
            blob.download_to_filename(str(destination))
        except google_exceptions.NotFound:
            self._raise_source_fetch_failed(
                f"GCS object not found: gs://{bucket_name}/{object_name}"
            )
        except google_exceptions.GoogleAPIError as exc:
            self._raise_source_fetch_failed(
                f"failed to download GCS source: gs://{bucket_name}/{object_name} ({exc})"
            )
        return destination

    def _parse_gcs_uri(self, parsed: ParseResult) -> tuple[str, str]:
        bucket_name = parsed.netloc
        object_name = parsed.path.lstrip("/")
        if bucket_name and object_name:
            return bucket_name, object_name
        self._raise_source_fetch_failed(f"invalid GCS source URI: {parsed.geturl()}")
        raise AssertionError("unreachable")

    def _raise_unsupported_source_uri(self, uri: str) -> NoReturn:
        self._raise_source_fetch_failed(
            f"only local, file://, and gs:// URIs are supported in this service build: {uri}"
        )

    def _raise_source_fetch_failed(self, message: str) -> NoReturn:
        raise HTTPException(
            status_code=400,
            detail=ErrorResponse(
                error=RunError(
                    code="source_fetch_failed",
                    message=message,
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
        entry_target = request.source.entry_contract
        contract_address = request.target.contract_address if request.target else None
        focus_parts: list[str] = []
        if entry_target:
            focus_parts.append(f"Focus on {entry_target}")
        if contract_address:
            focus_parts.append(f"target address {contract_address}")
        focus_clause = f" {' at '.join(focus_parts)}." if focus_parts else ""
        return (
            f"Analyze the workspace repository using profile {request.profile.id}."
            f"{focus_clause} "
            "Follow the active profile scope and extension instructions. "
            "Write the required valid JSON report to .agent-forge/report.json with "
            f'schema_version "{request.profile.report_schema}" and include run_id '
            "and summary along with any schema-required fields."
        )

    def _build_run_completion_contract(
        self,
        record: HostedRunRecord,
        resolved_profile: AgentProfile | None,
    ) -> CompletionContract:
        contract = resolve_completion_contract(
            profile=resolved_profile,
            report_schema=record.request.profile.report_schema,
        )
        if contract is not None:
            return contract
        return artifact_report_contract(
            relative_path=str(_REPORT_RELATIVE_PATH),
            required_fields=sorted(_REPORT_REQUIRED_FIELDS),
            expected_schema_version=record.request.profile.report_schema,
            max_remediation_attempts=1,
        )

    def _validate_report_artifact(
        self,
        record: HostedRunRecord,
        *,
        contract: CompletionContract | None = None,
    ) -> None:
        """Validate the report file via the active JSON artifact contract clause."""
        clause: JsonArtifactClause | None = None
        if contract is not None:
            clause = next(
                (item for item in contract.clauses if isinstance(item, JsonArtifactClause)),
                None,
            )

        raw = (
            record.report_path.read_text(encoding="utf-8") if record.report_path.exists() else None
        )
        result = evaluate_json_artifact_raw(
            raw,
            relative_path=clause.relative_path
            if clause is not None
            else str(_REPORT_RELATIVE_PATH),
            required_fields=(
                clause.required_fields if clause is not None else tuple(_REPORT_REQUIRED_FIELDS)
            ),
            expected_schema_version=clause.expected_schema_version if clause is not None else None,
            validator=clause.validator if clause is not None else None,
            missing_error_code=(
                clause.missing_error_code if clause is not None else "report_generation_failed"
            ),
            invalid_json_error_code=(
                clause.invalid_json_error_code if clause is not None else "report_invalid_json"
            ),
            schema_error_code=(
                clause.schema_error_code if clause is not None else "report_schema_invalid"
            ),
        )
        if not result.passed:
            record.error = RunError(
                code=cast("ErrorCode", result.error_code or "report_generation_failed"),
                message=result.detail,
                retryable=False,
            )
            raise RuntimeError(record.error.message)

    def _make_task_runner(self) -> Any:
        return self._execute_leased_task

    async def _execute_leased_task(self, task: Task) -> None:
        self._ensure_store_initialized()
        record = self._require_record(task.id)
        self._ensure_record_persisted(record, task)
        lease = self._store.try_claim_lease(
            task.id,
            worker_id=self._worker_id,
            lease_ttl_seconds=self._config.service.lease_ttl_seconds,
        )
        if lease is None:
            self._append_access_event(
                event="run.duplicate_delivery_ignored",
                client_service_id=record.owner_service_id or record.request.client.service_id,
                run_id=record.run_id,
                request_id=record.request.client.request_id,
                profile_id=record.request.profile.id,
                submitted_at=record.created_at,
                final_status=record.status,
                reason="active lease held or run already finalized",
                request_origin=record.request_origin,
                user_agent=record.user_agent,
            )
            return

        record.status = "running"
        if record.started_at is None:
            record.started_at = datetime.now(UTC)

        cancel_token = CancellationToken()
        self._cancel_tokens[task.id] = cancel_token
        heartbeat_task = asyncio.create_task(self._lease_heartbeat(task.id, lease.lease_token))
        try:
            await self._run_leased_task_agent(record, task, cancel_token)
            if cancel_token.is_cancelled or self._is_run_cancelled(task.id):
                self._sync_record_from_store(task.id)
                return
            self._complete_leased_run(record, task, lease.lease_token)
        except BaseException as exc:
            if cancel_token.is_cancelled or self._is_run_cancelled(task.id):
                self._sync_record_from_store(task.id)
                return
            if not isinstance(exc, Exception):
                raise
            self._fail_leased_run(record, task, lease.lease_token, exc)
            raise
        finally:
            self._cancel_tokens.pop(task.id, None)
            self._active_sandboxes.pop(task.id, None)
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task

    async def _run_leased_task_agent(
        self,
        record: HostedRunRecord,
        task: Task,
        cancel_token: CancellationToken,
    ) -> None:
        resolved_profile = self._profile_registry.get(record.request.profile.id)
        prompt_scope = resolved_profile.prompt_scope if resolved_profile else None
        provider_name, api_key = self._resolve_provider_credentials(record, resolved_profile)
        model_name = (
            resolved_profile.llm_model
            if resolved_profile and resolved_profile.llm_model
            else task.config.model
        )
        agent_config = AgentConfig(
            model=model_name,
            max_iterations=task.config.max_iterations,
            max_tokens_per_run=task.config.max_tokens_per_run,
            temperature=task.config.temperature,
            profile_id=resolved_profile.id if resolved_profile else None,
        )

        completion_contract = self._build_run_completion_contract(record, resolved_profile)

        async def _post_run_hook(
            agent_run: AgentRun,
            llm: Any,
            tools: Any,
            sandbox: Any,
        ) -> None:
            del llm, tools, sandbox
            if cancel_token.is_cancelled or self._is_run_cancelled(task.id):
                return
            self._verify_run_contract(record, agent_run, completion_contract)

        def _on_sandbox_started(sandbox: Sandbox) -> None:
            self._active_sandboxes[task.id] = sandbox
            if cancel_token.is_cancelled or self._is_run_cancelled(task.id):
                raise asyncio.CancelledError

        await run_agent_task(
            task.task_description,
            task.repo_path,
            self._config,
            agent_config,
            provider_name,
            api_key,
            run_id=task.id,
            prompt_scope=prompt_scope,
            report_schema=record.request.profile.report_schema,
            discover_extensions=True,
            event_bus=self._event_bus,
            cancellation_token=cancel_token,
            completion_contract=completion_contract,
            on_sandbox_started=_on_sandbox_started,
            post_run_hook=_post_run_hook,
            tools_factory=create_default_registry,
            sandbox_factory=create_sandbox,
            provider_factory=create_provider,
            react_loop_fn=react_loop,
        )

    def _complete_leased_run(
        self,
        record: HostedRunRecord,
        task: Task,
        lease_token: str,
    ) -> None:
        completed_at = datetime.now(UTC)
        report_json = record.report_path.read_text(encoding="utf-8")
        finalized = self._store.finalize_run(
            task.id,
            lease_token=lease_token,
            status="completed",
            completed_at=completed_at,
            report_json=report_json,
        )
        if not finalized:
            self._handle_stale_lease(record)
            return
        record.status = "completed"
        record.completed_at = completed_at
        record.report_json = report_json
        self._append_access_event(
            event="run.completed",
            client_service_id=record.owner_service_id or record.request.client.service_id,
            run_id=record.run_id,
            request_id=record.request.client.request_id,
            profile_id=record.request.profile.id,
            submitted_at=record.created_at,
            final_status="completed",
            request_origin=record.request_origin,
            user_agent=record.user_agent,
        )

    def _fail_leased_run(
        self,
        record: HostedRunRecord,
        task: Task,
        lease_token: str,
        exc: Exception,
    ) -> None:
        run_error = record.error or RunError(
            code="sandbox_execution_failed",
            message=str(exc),
            retryable=False,
        )
        completed_at = datetime.now(UTC)
        finalized = self._store.finalize_run(
            task.id,
            lease_token=lease_token,
            status="failed",
            completed_at=completed_at,
            error_json=run_error.model_dump_json(),
        )
        if not finalized:
            self._handle_stale_lease(record)
            return
        record.error = run_error
        record.status = "failed"
        record.completed_at = completed_at
        self._append_access_event(
            event="run.failed",
            client_service_id=record.owner_service_id or record.request.client.service_id,
            run_id=record.run_id,
            request_id=record.request.client.request_id,
            profile_id=record.request.profile.id,
            submitted_at=record.created_at,
            final_status="failed",
            reason=run_error.message,
            request_origin=record.request_origin,
            user_agent=record.user_agent,
        )

    def _is_run_cancelled(self, run_id: str) -> bool:
        persisted = self._store.get(run_id)
        return persisted is not None and persisted.status == "cancelled"

    def _ensure_store_initialized(self) -> None:
        if not self._store_initialized:
            self._store.initialize()
            self._store_initialized = True

    def _hydrate_record(self, persisted: PersistedRun) -> HostedRunRecord:
        existing = self._records.get(persisted.run_id)
        request = RunRequest.model_validate_json(persisted.request_json)
        error = RunError.model_validate_json(persisted.error_json) if persisted.error_json else None
        if existing is None:
            record = HostedRunRecord(
                run_id=persisted.run_id,
                request=request,
                workspace_dir=persisted.workspace_dir,
                created_at=persisted.created_at,
                owner_service_id=persisted.owner_service_id,
                idempotency_key=persisted.idempotency_key,
                status=persisted.status,
                request_origin=persisted.request_origin,
                user_agent=persisted.user_agent,
                started_at=persisted.started_at,
                completed_at=persisted.completed_at,
                error=error,
                report_json=persisted.report_json,
            )
            self._records[persisted.run_id] = record
            return record

        existing.workspace_dir = persisted.workspace_dir
        existing.owner_service_id = persisted.owner_service_id
        existing.idempotency_key = persisted.idempotency_key
        existing.status = persisted.status
        existing.started_at = persisted.started_at or existing.started_at
        existing.completed_at = persisted.completed_at or existing.completed_at
        if error is not None:
            existing.error = error
        if persisted.report_json is not None:
            existing.report_json = persisted.report_json
        return existing

    def _hydrate_records_from_store(self) -> None:
        for persisted in self._store.list_all():
            self._hydrate_record(persisted)

    def _sync_record_from_store(self, run_id: str) -> HostedRunRecord | None:
        if self._store_initialized:
            persisted = self._store.get(run_id)
            if persisted is not None:
                return self._hydrate_record(persisted)
        return self._records.get(run_id)

    def _serialize_agent_config(self, config: AgentConfig) -> str:
        return json.dumps(asdict(config))

    def _deserialize_agent_config(self, raw: str) -> AgentConfig:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            msg = "persisted agent_config must be a JSON object"
            raise ValueError(msg)
        return AgentConfig(**payload)

    def _ensure_record_persisted(self, record: HostedRunRecord, task: Task) -> None:
        if self._store.get(record.run_id) is not None:
            return
        agent_config = (
            task.config
            if isinstance(task.config, AgentConfig)
            else AgentConfig(model=str(task.config.model))
        )
        owner_service_id = record.owner_service_id or record.request.client.service_id
        owner_key = owner_service_id or record.request.client.name
        self._store.create_or_get(
            run_id=record.run_id,
            owner_service_id=owner_service_id,
            owner_key=owner_key,
            idempotency_key=record.idempotency_key,
            request_fingerprint=self._request_fingerprint(record.request),
            request_json=record.request.model_dump_json(),
            workspace_dir=record.workspace_dir,
            task_prompt=task.task_description,
            agent_config_json=self._serialize_agent_config(agent_config),
            request_origin=record.request_origin,
            user_agent=record.user_agent,
            created_at=record.created_at,
        )

    def _resolve_idempotency_key(
        self,
        request: RunRequest,
        headers: dict[str, str] | None,
    ) -> str | None:
        header_key: str | None = None
        if headers:
            lowered = {k.lower(): v for k, v in headers.items()}
            raw_header = lowered.get("idempotency-key")
            if raw_header is not None:
                header_key = raw_header.strip()
                if not header_key:
                    self._deny(
                        status_code=400,
                        code="invalid_request",
                        message="Idempotency-Key header must not be empty",
                        request_origin=None,
                        user_agent=None,
                    )
        body_key = request.idempotency_key.strip() if request.idempotency_key is not None else None
        if body_key is not None and not body_key:
            self._deny(
                status_code=400,
                code="invalid_request",
                message="idempotency_key must not be empty",
                request_origin=None,
                user_agent=None,
            )
        if header_key is not None and body_key is not None and header_key != body_key:
            self._deny(
                status_code=400,
                code="invalid_request",
                message="conflicting Idempotency-Key header and request.idempotency_key",
                request_origin=None,
                user_agent=None,
            )
        return header_key or body_key

    def _request_fingerprint(self, request: RunRequest) -> str:
        payload = request.model_dump(mode="json", exclude={"idempotency_key"})
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _raise_idempotency_conflict(self) -> NoReturn:
        raise HTTPException(
            status_code=409,
            detail=ErrorResponse(
                error=RunError(
                    code="invalid_request",
                    message="idempotency key already used with a different request payload",
                    retryable=False,
                )
            ).model_dump(),
        )

    async def _dispatch_new_task(self, record: HostedRunRecord, task: Task) -> None:
        try:
            await self._queue.enqueue(task)
        except Exception as exc:  # noqa: BLE001 — preserve accepted record for recovery loop
            self._append_access_event(
                event="run.dispatch_failed",
                client_service_id=record.owner_service_id or record.request.client.service_id,
                run_id=record.run_id,
                request_id=record.request.client.request_id,
                profile_id=record.request.profile.id,
                submitted_at=record.created_at,
                final_status="accepted",
                reason=str(exc),
                request_origin=record.request_origin,
                user_agent=record.user_agent,
            )
            return
        self._store.mark_dispatched(record.run_id)
        record.status = "queued"

    async def _status_for_record(self, record: HostedRunRecord) -> RunStatus:
        if record.status in TERMINAL_RUN_STATUSES or record.status == "running":
            return self._build_status(record, status=record.status)
        try:
            queue_status = await self._queue.get_status(record.run_id)
            mapped = self._map_status(queue_status)
        except KeyError:
            mapped = record.status
        return self._build_status(record, status=mapped)

    def _resolve_provider_credentials(
        self,
        record: HostedRunRecord,
        resolved_profile: AgentProfile | None,
    ) -> tuple[str, str]:
        provider_name = (
            resolved_profile.llm_provider
            if resolved_profile and resolved_profile.llm_provider
            else self._config.agent.default_provider
        )
        provider_cfg = self._config.providers.get(provider_name)
        if provider_cfg is None or provider_name not in provider_names():
            record.error = RunError(
                code="invalid_request",
                message=f"unknown provider: {provider_name}",
                retryable=False,
            )
            raise RuntimeError(record.error.message)
        api_key = os.environ.get(provider_cfg.api_key_env, "") if provider_cfg.api_key_env else ""
        from agent_forge.llm.qualification import is_api_key_required

        if (
            is_api_key_required(
                self._config,
                provider_name,
                base_url=provider_cfg.base_url,
            )
            and not api_key
        ):
            record.error = RunError(
                code="unauthorized",
                message=f"missing provider API key env: {provider_cfg.api_key_env}",
                retryable=False,
            )
            raise RuntimeError(record.error.message)
        return provider_name, api_key

    def _verify_run_contract(
        self,
        record: HostedRunRecord,
        agent_run: AgentRun,
        completion_contract: CompletionContract,
    ) -> None:
        """Enforce the run's evaluated completion contract and report artifact rules."""
        evaluation = getattr(agent_run, "contract_evaluation", None)
        if isinstance(evaluation, ContractEvaluation) and not evaluation.passed:
            unmet = evaluation.unmet_clauses[0]
            record.error = RunError(
                code=cast("ErrorCode", unmet.error_code or "report_generation_failed"),
                message=unmet.detail or evaluation.summary_reason,
                retryable=False,
            )
            raise RuntimeError(record.error.message)
        self._validate_report_artifact(record, contract=completion_contract)

    def _handle_stale_lease(self, record: HostedRunRecord) -> None:
        self._sync_record_from_store(record.run_id)
        self._append_access_event(
            event="run.stale_lease_ignored",
            client_service_id=record.owner_service_id or record.request.client.service_id,
            run_id=record.run_id,
            request_id=record.request.client.request_id,
            profile_id=record.request.profile.id,
            submitted_at=record.created_at,
            final_status=record.status,
            reason="fencing token rejected after lease expiry or takeover",
            request_origin=record.request_origin,
            user_agent=record.user_agent,
        )

    async def _lease_heartbeat(self, run_id: str, lease_token: str) -> None:
        ttl = self._config.service.lease_ttl_seconds
        interval = max(0.05, min(ttl / 2.0, 30.0))
        while True:
            await asyncio.sleep(interval)
            if not self._store.renew_lease(
                run_id,
                lease_token=lease_token,
                lease_ttl_seconds=ttl,
            ):
                return

    async def recover_runs(
        self,
        *,
        now: datetime | None = None,
        reclaim_all_running: bool = False,
    ) -> list[str]:
        """Recover pending dispatches and expired leases into the worker queue."""
        self._ensure_store_initialized()
        check_time = now.astimezone(UTC) if now is not None else datetime.now(UTC)
        recoverable = self._store.list_recoverable(
            now=check_time,
            reclaim_all_running=reclaim_all_running,
        )
        recovered: list[str] = []
        for persisted in recoverable:
            is_expired_running = persisted.status == "running" and (
                reclaim_all_running
                or persisted.lease_expires_at is None
                or persisted.lease_expires_at <= check_time
            )
            if not is_expired_running and await self._is_queued_or_processing(persisted.run_id):
                continue

            if not self._store.requeue_for_recovery(
                persisted.run_id,
                expected_attempt=persisted.attempt,
            ):
                continue

            record = self._hydrate_record(persisted)
            record.status = "queued"
            if not record.workspace_dir.exists():
                record.workspace_dir = self._materialize_source(record.run_id, record.request)
            agent_config = self._deserialize_agent_config(persisted.agent_config_json)
            task = Task(
                id=record.run_id,
                task_description=persisted.task_prompt,
                repo_path=str(record.workspace_dir),
                config=agent_config,
            )
            try:
                await self._queue.enqueue(task)
            except Exception as exc:  # noqa: BLE001 — keep persisted run recoverable
                _logger.warning("Failed to recover run %s: %s", record.run_id, exc)
                continue
            recovered.append(record.run_id)
        return recovered

    async def _is_queued_or_processing(self, run_id: str) -> bool:
        try:
            queue_status = await self._queue.get_status(run_id)
        except KeyError:
            return False
        return queue_status in (TaskStatus.QUEUED, TaskStatus.PROCESSING)

    async def _recovery_loop(self) -> None:
        interval = self._config.service.recovery_interval_seconds
        while True:
            await asyncio.sleep(interval)
            with contextlib.suppress(Exception):
                await self.recover_runs()

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
        include_logs = record.request.artifacts is None or bool(
            record.request.artifacts.include_logs
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


def _resolve_after_seq(after_seq: int | None, http_request: Request) -> int:
    if after_seq is not None:
        return after_seq
    last_event_id = http_request.headers.get("last-event-id")
    if last_event_id is None:
        return -1
    try:
        return int(last_event_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"invalid Last-Event-ID header: {last_event_id}",
        ) from exc


def create_app(  # noqa: C901
    *,
    service_root: Path | None = None,
    runs_dir: Path | None = None,
    config: ForgeConfig | None = None,
    instance_id: str | None = None,
    persona: str | None = None,
) -> FastAPI:
    """Create the hosted service ASGI app."""
    resolved_config = config or load_config()
    service = HostedRunService(
        service_root=service_root,
        runs_dir=runs_dir,
        config=resolved_config,
        instance_id=instance_id,
        persona=persona,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> Any:
        await service.start()
        try:
            yield
        finally:
            await service.stop()

    app = FastAPI(title="agent-forge hosted service", version=__version__, lifespan=lifespan)
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
        resolved_profile = service._resolved_persona
        return HealthResponse(
            status="ok",
            service_root=str(service._service_root),
            queue_backend=resolved_config.queue.backend,
            sandbox_image=resolved_config.sandbox.image,
            instance_id=service._instance_id,
            persona=service._persona_id,
            capabilities=(resolved_profile.capabilities if resolved_profile else []),
            llm_provider=(resolved_profile.llm_provider if resolved_profile else None),
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

    @app.post("/v1/runs/{run_id}/cancel", response_model=RunStatus)
    async def cancel_run(run_id: str, http_request: Request) -> RunStatus:
        try:
            return await service.cancel_run(
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

    @app.get("/v1/runs/{run_id}/report", response_model=RunReport)
    async def get_report(run_id: str, http_request: Request) -> RunReport:
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

    @app.get("/v1/runs/{run_id}/events", response_model=None)
    async def get_events(
        run_id: str,
        http_request: Request,
        after_seq: int | None = None,
        limit: int = 100,
        follow: bool = False,
    ) -> StreamEventPage | StreamingResponse:
        effective_after_seq = _resolve_after_seq(after_seq, http_request)
        wants_sse = follow or "text/event-stream" in http_request.headers.get("accept", "").lower()
        headers = dict(http_request.headers)
        origin = _request_origin(http_request)
        user_agent = http_request.headers.get("user-agent")
        try:
            if not wants_sse:
                return service.get_events(
                    run_id,
                    after_seq=effective_after_seq,
                    limit=limit,
                    headers=headers,
                    request_origin=origin,
                    user_agent=user_agent,
                )
            event_iter = service.stream_events(
                run_id,
                after_seq=effective_after_seq,
                page_size=limit,
                headers=headers,
                request_origin=origin,
                user_agent=user_agent,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"unknown run id: {run_id}") from exc

        async def _sse_chunks() -> AsyncIterator[str]:
            async for event in event_iter:
                yield event.to_sse()

        return StreamingResponse(
            _sse_chunks(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app
