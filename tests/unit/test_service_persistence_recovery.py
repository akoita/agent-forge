"""Unit tests for durable hosted run persistence and recovery (#156)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from agent_forge.agent.models import AgentRun, RunState
from agent_forge.config import ForgeConfig, ProviderSettings, ServiceSettings
from agent_forge.orchestration.queue import Task
from agent_forge.profiles import load_profiles
from agent_forge.service import HostedServiceClient, create_app
from agent_forge.service.app import HostedRunService
from agent_forge.service.models import RunRequest
from agent_forge.service.security import load_client_registry


def _write_clients_file(path: Path) -> None:
    path.write_text(
        """
[clients.client-a]
api_key_env = "CLIENT_A_KEY"
allowed_profiles = ["gemini", "full-spectrum"]
allowed_report_schemas = ["agent-forge-report-v1"]
allowed_source_kinds = ["local_path"]
max_active_runs = 1
max_runs_per_day = 10
allow_local_path = true

[clients.client-b]
api_key_env = "CLIENT_B_KEY"
allowed_profiles = ["gemini", "full-spectrum"]
allowed_report_schemas = ["agent-forge-report-v1"]
allowed_source_kinds = ["local_path"]
max_active_runs = 1
max_runs_per_day = 10
allow_local_path = true
""".lstrip(),
        encoding="utf-8",
    )


def _service_config(
    tmp_path: Path,
    *,
    clients_path: Path,
    lease_ttl_seconds: float = 30.0,
    recovery_interval_seconds: float = 10.0,
) -> ForgeConfig:
    return ForgeConfig(
        providers={"gemini": ProviderSettings(api_key_env="GEMINI_API_KEY", default_model="m")},
        service=ServiceSettings(
            root_dir=str(tmp_path / "service-root"),
            auth_enabled=True,
            clients_path=str(clients_path),
            allow_local_path_sources=True,
            lease_ttl_seconds=lease_ttl_seconds,
            recovery_interval_seconds=recovery_interval_seconds,
        ),
    )


def _make_payload(
    repo: Path,
    *,
    request_id: str,
    service_id: str = "client-a",
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "agent-forge-run-request-v1",
        "client": {"name": "caller", "request_id": request_id, "service_id": service_id},
        "profile": {"id": "gemini", "report_schema": "agent-forge-report-v1"},
        "source": {"kind": "local_path", "uri": str(repo)},
    }
    if idempotency_key is not None:
        payload["idempotency_key"] = idempotency_key
    return payload


def _write_report(workspace_dir: Path, run_id: str, summary: str) -> None:
    report_path = workspace_dir / ".agent-forge" / "report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(
            {
                "schema_version": "agent-forge-report-v1",
                "run_id": run_id,
                "summary": summary,
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_idempotency_and_ownership_persist_across_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Idempotency keys dedupe per owner, reject mismatched payloads, and survive restarts."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    clients_path = tmp_path / "clients.toml"
    _write_clients_file(clients_path)
    monkeypatch.setenv("CLIENT_A_KEY", "key-a")
    monkeypatch.setenv("CLIENT_B_KEY", "key-b")
    monkeypatch.setenv("GEMINI_API_KEY", "gem-key")

    cfg = _service_config(tmp_path, clients_path=clients_path)
    app1 = create_app(config=cfg)
    app1.state.service._worker._task_runner = AsyncMock(return_value=None)

    async with (
        app1.router.lifespan_context(app1),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app1),
            base_url="http://testserver",
        ) as http_client,
    ):
        first = await http_client.post(
            "/v1/runs",
            json=_make_payload(repo, request_id="req-1"),
            headers={"X-Agent-Forge-API-Key": "key-a", "Idempotency-Key": "idem-42"},
        )
        assert first.status_code == 202
        run_a1 = first.json()["run_id"]

        # Same owner + same key returns original run despite max_active_runs=1
        duplicate = await http_client.post(
            "/v1/runs",
            json=_make_payload(repo, request_id="req-1"),
            headers={"X-Agent-Forge-API-Key": "key-a", "Idempotency-Key": "idem-42"},
        )
        assert duplicate.status_code == 202
        assert duplicate.json()["run_id"] == run_a1

        # Same owner + same key + different payload => 409 Conflict
        conflict = await http_client.post(
            "/v1/runs",
            json=_make_payload(repo, request_id="req-different"),
            headers={"X-Agent-Forge-API-Key": "key-a", "Idempotency-Key": "idem-42"},
        )
        assert conflict.status_code == 409

        # Different owner may reuse the same idempotency key independently
        other_owner = await http_client.post(
            "/v1/runs",
            json=_make_payload(repo, request_id="req-1", service_id="client-b"),
            headers={"X-Agent-Forge-API-Key": "key-b", "Idempotency-Key": "idem-42"},
        )
        assert other_owner.status_code == 202
        assert other_owner.json()["run_id"] != run_a1

    # Restart against the same service_root: idempotency & ownership remain enforced
    app2 = create_app(config=cfg)
    app2.state.service._worker._task_runner = AsyncMock(return_value=None)
    async with (
        app2.router.lifespan_context(app2),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app2),
            base_url="http://testserver",
        ) as http_client,
    ):
        replayed = await http_client.post(
            "/v1/runs",
            json=_make_payload(repo, request_id="req-1"),
            headers={"X-Agent-Forge-API-Key": "key-a", "Idempotency-Key": "idem-42"},
        )
        assert replayed.status_code == 202
        assert replayed.json()["run_id"] == run_a1

        forbidden = await http_client.get(
            f"/v1/runs/{run_a1}",
            headers={"X-Agent-Forge-API-Key": "key-b"},
        )
        assert forbidden.status_code == 403


@pytest.mark.asyncio
async def test_failed_dispatch_recovers_once_queue_returns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run whose initial queue dispatch failed stays persisted and recovers."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    clients_path = tmp_path / "clients.toml"
    _write_clients_file(clients_path)
    monkeypatch.setenv("CLIENT_A_KEY", "key-a")
    monkeypatch.setenv("GEMINI_API_KEY", "gem-key")

    cfg = _service_config(tmp_path, clients_path=clients_path)
    app = create_app(config=cfg)
    service: HostedRunService = app.state.service

    async def fake_run_agent_task(
        task: str,
        repo_path: str,
        cfg: ForgeConfig,
        agent_config: Any,
        provider_name: str,
        api_key: str,
        **kwargs: Any,
    ) -> AgentRun:
        run_id = kwargs["run_id"]
        _write_report(Path(repo_path), run_id, "recovered dispatch summary")
        run = AgentRun(task=task, repo_path=repo_path, config=agent_config, id=run_id)
        run.state = RunState.COMPLETED
        hook = kwargs["post_run_hook"]
        await hook(run, AsyncMock(), AsyncMock(), AsyncMock())
        return run

    with patch("agent_forge.service.app.run_agent_task", side_effect=fake_run_agent_task):
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as http_client,
        ):
            original_enqueue = service._queue.enqueue
            with patch.object(
                service._queue,
                "enqueue",
                new=AsyncMock(side_effect=RuntimeError("transient queue error")),
            ):
                created = await http_client.post(
                    "/v1/runs",
                    json=_make_payload(repo, request_id="dispatch-fail-1"),
                    headers={"X-Agent-Forge-API-Key": "key-a"},
                )
            assert created.status_code == 202
            run_id = created.json()["run_id"]
            assert created.json()["status"] == "accepted"

            # Restore queue and trigger recovery
            service._queue.enqueue = original_enqueue  # type: ignore[method-assign]
            recovered = await service.recover_runs()
            assert recovered == [run_id]

            client = HostedServiceClient(
                base_url="http://testserver",
                api_key="key-a",
                http_client=http_client,
            )
            report = await client.wait_for_report(run_id, poll_interval_seconds=0.01)

    assert report.run_id == run_id
    assert report.summary == "recovered dispatch summary"


@pytest.mark.asyncio
async def test_duplicate_delivery_executes_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent and post-completion duplicate queue deliveries run the task once."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    clients_path = tmp_path / "clients.toml"
    _write_clients_file(clients_path)
    monkeypatch.setenv("CLIENT_A_KEY", "key-a")
    monkeypatch.setenv("GEMINI_API_KEY", "gem-key")

    cfg = _service_config(tmp_path, clients_path=clients_path)
    app = create_app(config=cfg)
    service: HostedRunService = app.state.service

    exec_count = 0
    entered = asyncio.Event()
    release = asyncio.Event()

    async def fake_run_agent_task(
        task: str,
        repo_path: str,
        cfg: ForgeConfig,
        agent_config: Any,
        provider_name: str,
        api_key: str,
        **kwargs: Any,
    ) -> AgentRun:
        nonlocal exec_count
        exec_count += 1
        run_id = kwargs["run_id"]
        entered.set()
        await release.wait()
        _write_report(Path(repo_path), run_id, "single authoritative execution")
        run = AgentRun(task=task, repo_path=repo_path, config=agent_config, id=run_id)
        run.state = RunState.COMPLETED
        await kwargs["post_run_hook"](run, AsyncMock(), AsyncMock(), AsyncMock())
        return run

    with patch("agent_forge.service.app.run_agent_task", side_effect=fake_run_agent_task):
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as http_client,
        ):
            created = await http_client.post(
                "/v1/runs",
                json=_make_payload(repo, request_id="dup-1"),
                headers={"X-Agent-Forge-API-Key": "key-a"},
            )
            run_id = created.json()["run_id"]
            await entered.wait()

            # Deliver the exact same task twice while first lease is active
            runner = service._make_task_runner()
            dup_task = Task(
                id=run_id,
                task_description="dup",
                repo_path=str(service._records[run_id].workspace_dir),
                config=service._worker._queue._tasks[run_id].config,  # type: ignore[attr-defined]
            )
            await runner(dup_task)

            release.set()
            client = HostedServiceClient(
                base_url="http://testserver",
                api_key="key-a",
                http_client=http_client,
            )
            report = await client.wait_for_report(run_id, poll_interval_seconds=0.01)

            # And re-deliver once already terminal
            await runner(dup_task)

    assert exec_count == 1
    assert report.summary == "single authoritative execution"


@pytest.mark.asyncio
async def test_expired_lease_fences_stale_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When lease A expires and worker B completes, stale worker A cannot overwrite."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    clients_path = tmp_path / "clients.toml"
    _write_clients_file(clients_path)
    monkeypatch.setenv("CLIENT_A_KEY", "key-a")
    monkeypatch.setenv("GEMINI_API_KEY", "gem-key")

    cfg = _service_config(
        tmp_path,
        clients_path=clients_path,
        lease_ttl_seconds=1.0,
        recovery_interval_seconds=60.0,
    )
    app = create_app(config=cfg)
    svc_a: HostedRunService = app.state.service
    svc_a._worker_id = "worker-a"

    svc_b = HostedRunService(config=cfg)
    svc_b._worker_id = "worker-b"
    svc_b._store = svc_a._store
    svc_b._store_initialized = True

    worker_a_started = asyncio.Event()
    release_worker_a = asyncio.Event()
    active_worker = "a"

    async def fake_run(
        task: str,
        repo_path: str,
        _cfg: ForgeConfig,
        agent_config: Any,
        provider_name: str,
        api_key: str,
        **kwargs: Any,
    ) -> AgentRun:
        run_id = kwargs["run_id"]
        if active_worker == "a":
            worker_a_started.set()
            await release_worker_a.wait()
            _write_report(Path(repo_path), run_id, "stale worker A output")
        else:
            _write_report(Path(repo_path), run_id, "authoritative worker B output")
        run = AgentRun(task=task, repo_path=repo_path, config=agent_config, id=run_id)
        run.state = RunState.COMPLETED
        await kwargs["post_run_hook"](run, AsyncMock(), AsyncMock(), AsyncMock())
        return run

    with patch("agent_forge.service.app.run_agent_task", side_effect=fake_run):
        svc_a._ensure_store_initialized()
        svc_a._client_policies = load_client_registry(clients_path)
        svc_b._client_policies = svc_a._client_policies
        svc_a._profile_registry = load_profiles()
        svc_b._profile_registry = svc_a._profile_registry

        status = await svc_a.create_run(
            RunRequest.model_validate(_make_payload(repo, request_id="lease-1")),
            headers={"X-Agent-Forge-API-Key": "key-a"},
        )
        run_id = status.run_id
        task_a = await svc_a._queue.dequeue()
        assert task_a is not None

        # Prevent Worker A heartbeat from renewing while it stalls
        with patch.object(svc_a, "_lease_heartbeat", new=AsyncMock()):
            worker_a_coro = asyncio.create_task(svc_a._make_task_runner()(task_a))
            await worker_a_started.wait()

            # Simulate lease expiry and Worker B recovering + executing to completion
            active_worker = "b"
            future = datetime.now(UTC) + timedelta(seconds=60)
            recovered = await svc_b.recover_runs(now=future)
            assert recovered == [run_id]
            task_b = await svc_b._queue.dequeue()
            assert task_b is not None
            await svc_b._make_task_runner()(task_b)

            # Now stalled Worker A wakes up and attempts to overwrite on disk & DB
            release_worker_a.set()
            await worker_a_coro

        report = svc_a.get_report(run_id, headers={"X-Agent-Forge-API-Key": "key-a"})

    assert report.summary == "authoritative worker B output"


@pytest.mark.asyncio
async def test_api_restart_preserves_completed_and_recovers_in_flight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """API/worker restart serves completed results and finishes interrupted runs."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    clients_path = tmp_path / "clients.toml"
    _write_clients_file(clients_path)
    monkeypatch.setenv("CLIENT_A_KEY", "key-a")
    monkeypatch.setenv("GEMINI_API_KEY", "gem-key")

    cfg = _service_config(tmp_path, clients_path=clients_path)

    async def fake_run_agent_task(
        task: str,
        repo_path: str,
        _cfg: ForgeConfig,
        agent_config: Any,
        provider_name: str,
        api_key: str,
        **kwargs: Any,
    ) -> AgentRun:
        run_id = kwargs["run_id"]
        _write_report(Path(repo_path), run_id, f"completed-{run_id}")
        run = AgentRun(task=task, repo_path=repo_path, config=agent_config, id=run_id)
        run.state = RunState.COMPLETED
        await kwargs["post_run_hook"](run, AsyncMock(), AsyncMock(), AsyncMock())
        return run

    # Service instance #1 completes run1, and leaves run2 queued/running across crash
    app1 = create_app(config=cfg)
    svc1: HostedRunService = app1.state.service
    with patch("agent_forge.service.app.run_agent_task", side_effect=fake_run_agent_task):
        async with (
            app1.router.lifespan_context(app1),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app1),
                base_url="http://testserver",
            ) as http1,
        ):
            client1 = HostedServiceClient(
                base_url="http://testserver",
                api_key="key-a",
                http_client=http1,
            )
            r1 = await http1.post(
                "/v1/runs",
                json=_make_payload(repo, request_id="done-req", idempotency_key="done-key"),
                headers={"X-Agent-Forge-API-Key": "key-a"},
            )
            run1_id = r1.json()["run_id"]
            report1 = await client1.wait_for_report(run1_id, poll_interval_seconds=0.01)
            assert report1.summary == f"completed-{run1_id}"

            # Stop worker before run2 can run, and simulate an interrupted active lease
            await svc1._worker.stop()
            r2 = await http1.post(
                "/v1/runs",
                json=_make_payload(repo, request_id="inflight-req"),
                headers={"X-Agent-Forge-API-Key": "key-a"},
            )
            run2_id = r2.json()["run_id"]
            lease = svc1._store.try_claim_lease(
                run2_id,
                worker_id="crashed-worker",
                lease_ttl_seconds=300.0,
            )
            assert lease is not None

    # Service instance #2 starts fresh against same service_root
    app2 = create_app(config=cfg)
    with patch("agent_forge.service.app.run_agent_task", side_effect=fake_run_agent_task):
        async with (
            app2.router.lifespan_context(app2),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app2),
                base_url="http://testserver",
            ) as http2,
        ):
            client2 = HostedServiceClient(
                base_url="http://testserver",
                api_key="key-a",
                http_client=http2,
            )
            status1 = await client2.get_status(run1_id)
            fetched_report1 = await client2.get_report(run1_id)
            assert status1.status == "completed"
            assert fetched_report1.summary == f"completed-{run1_id}"

            # Interrupted run2 must recover on startup and produce one authoritative result
            fetched_report2 = await client2.wait_for_report(run2_id, poll_interval_seconds=0.01)
            assert fetched_report2.summary == f"completed-{run2_id}"
