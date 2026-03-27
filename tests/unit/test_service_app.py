from __future__ import annotations

import asyncio
import json
import zipfile
from typing import TYPE_CHECKING

import httpx
import pytest

from agent_forge.config import ForgeConfig, ServiceSettings
from agent_forge.service.app import create_app

if TYPE_CHECKING:
    from pathlib import Path

    from agent_forge.orchestration.queue import Task


def _request_payload(source_uri: str) -> dict[str, object]:
    return {
        "schema_version": "agent-forge-run-request-v1",
        "client": {
            "name": "proof-of-audit",
            "request_id": "audit-123",
            "service_id": "proof-of-audit-auditor",
        },
        "profile": {
            "id": "proof-of-audit-solidity-v1",
            "report_schema": "proof-of-audit-report-v1",
            "max_iterations": 3,
        },
        "source": {
            "kind": "local_path",
            "uri": source_uri,
            "entry_contract": "Vault",
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


def _service_config(
    tmp_path: Path,
    *,
    auth_enabled: bool,
    clients_path: Path | None = None,
    allow_local_path_sources: bool = False,
    max_source_size_bytes: int = 50_000_000,
) -> ForgeConfig:
    return ForgeConfig(
        service=ServiceSettings(
            root_dir=str(tmp_path / "service-root"),
            auth_enabled=auth_enabled,
            clients_path=str(clients_path or (tmp_path / "clients.toml")),
            allow_local_path_sources=allow_local_path_sources,
            max_source_size_bytes=max_source_size_bytes,
        )
    )


def _write_clients_file(
    path: Path,
    *,
    allow_local_path: bool = False,
    include_secondary_client: bool = False,
) -> None:
    secondary = """
[clients.other-client]
api_key_env = "OTHER_SERVICE_API_KEY"
allowed_profiles = ["proof-of-audit-solidity-v1"]
allowed_report_schemas = ["proof-of-audit-report-v1"]
allowed_source_kinds = ["local_path"]
max_active_runs = 1
max_runs_per_day = 5
allow_local_path = true
""" if include_secondary_client else ""
    path.write_text(
        f"""
[clients.proof-of-audit-auditor]
api_key_env = "POA_SERVICE_API_KEY"
allowed_profiles = ["proof-of-audit-solidity-v1"]
allowed_report_schemas = ["proof-of-audit-report-v1"]
allowed_source_kinds = ["local_path", "archive_uri"]
max_active_runs = 1
max_runs_per_day = 5
allow_local_path = {"true" if allow_local_path else "false"}
{secondary}
""".lstrip(),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_service_create_run_and_return_report(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Vault.sol").write_text("contract Vault {}\n", encoding="utf-8")

    app = create_app(service_root=tmp_path / "service-root")
    service = app.state.service

    async def fake_runner(task: Task) -> None:
        record = service._records[task.id]
        record.started_at = record.created_at
        record.report_path.parent.mkdir(parents=True, exist_ok=True)
        record.report_path.write_text(
            json.dumps(
                {
                    "schema_version": "proof-of-audit-report-v1",
                    "run_id": task.id,
                    "summary": "Potential issue found",
                    "confidence": "medium",
                    "findings": [],
                    "stats": {
                        "finding_count": 0,
                        "max_severity": None,
                        "severity_breakdown": {
                            "critical": 0,
                            "high": 0,
                            "medium": 0,
                            "low": 0,
                        },
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        record.completed_at = record.created_at

    service._worker._task_runner = fake_runner
    transport = httpx.ASGITransport(app=app)

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as client,
    ):
        response = await client.post("/v1/runs", json=_request_payload(str(repo)))
        assert response.status_code == 202
        run_id = response.json()["run_id"]

        for _ in range(40):
            status_response = await client.get(f"/v1/runs/{run_id}")
            assert status_response.status_code == 200
            if status_response.json()["status"] == "completed":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("run did not complete")

        report_response = await client.get(f"/v1/runs/{run_id}/report")
        assert report_response.status_code == 200
        assert report_response.json()["schema_version"] == "proof-of-audit-report-v1"

        logs_response = await client.get(f"/v1/runs/{run_id}/logs")
        assert logs_response.status_code == 200
        report_json = logs_response.json()["artifacts"]["report_json"]
        assert report_json.endswith("/.agent-forge/report.json")


@pytest.mark.asyncio
async def test_service_materializes_zip_sources(tmp_path: Path) -> None:
    archive_path = tmp_path / "bundle.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("wrapped/src/Vault.sol", "contract Vault {}\n")

    app = create_app(service_root=tmp_path / "service-root")
    service = app.state.service

    async def fake_runner(task: Task) -> None:
        record = service._records[task.id]
        record.started_at = record.created_at
        record.completed_at = record.created_at

    service._worker._task_runner = fake_runner

    payload = _request_payload(str(archive_path))
    payload["source"] = {
        "kind": "archive_uri",
        "uri": str(archive_path),
        "archive_format": "zip",
        "entry_contract": "Vault",
        "source_digest": "sha256:test",
    }

    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as client,
    ):
        response = await client.post("/v1/runs", json=payload)
        assert response.status_code == 202
        run_id = response.json()["run_id"]
        record = service._records[run_id]
        assert (record.workspace_dir / "src" / "Vault.sol").exists()


@pytest.mark.asyncio
async def test_service_rejects_unknown_run_id(tmp_path: Path) -> None:
    app = create_app(service_root=tmp_path / "service-root")
    transport = httpx.ASGITransport(app=app)

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as client,
    ):
        response = await client.get("/v1/runs/run_missing")
        assert response.status_code == 404
        detail = response.json()["detail"]
        assert detail["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_service_rejects_unsupported_profile(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    app = create_app(service_root=tmp_path / "service-root")

    payload = _request_payload(str(repo))
    payload["profile"] = {
        "id": "generic-coding-agent-v1",
        "report_schema": "proof-of-audit-report-v1",
    }

    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as client,
    ):
        response = await client.post("/v1/runs", json=payload)
        assert response.status_code == 400


@pytest.mark.asyncio
async def test_service_healthcheck_uses_config_path(tmp_path: Path) -> None:
    app = create_app(service_root=tmp_path / "service-root")
    transport = httpx.ASGITransport(app=app)

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as client,
    ):
        response = await client.get("/healthz")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_service_requires_api_key_when_auth_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    clients_path = tmp_path / "clients.toml"
    _write_clients_file(clients_path, allow_local_path=True)
    monkeypatch.setenv("POA_SERVICE_API_KEY", "test-service-key")

    app = create_app(
        config=_service_config(
            tmp_path,
            auth_enabled=True,
            clients_path=clients_path,
            allow_local_path_sources=True,
        )
    )
    transport = httpx.ASGITransport(app=app)

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as client,
    ):
        response = await client.post("/v1/runs", json=_request_payload(str(repo)))
        assert response.status_code == 401
        assert response.json()["detail"]["error"]["code"] == "unauthorized"


@pytest.mark.asyncio
async def test_service_denies_local_paths_by_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    clients_path = tmp_path / "clients.toml"
    _write_clients_file(clients_path, allow_local_path=False)
    monkeypatch.setenv("POA_SERVICE_API_KEY", "test-service-key")

    app = create_app(
        config=_service_config(
            tmp_path,
            auth_enabled=True,
            clients_path=clients_path,
            allow_local_path_sources=False,
        )
    )
    transport = httpx.ASGITransport(app=app)

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as client,
    ):
        response = await client.post(
            "/v1/runs",
            json=_request_payload(str(repo)),
            headers={"X-Agent-Forge-API-Key": "test-service-key"},
        )
        assert response.status_code == 403
        assert response.json()["detail"]["error"]["code"] == "policy_denied"


@pytest.mark.asyncio
async def test_service_denies_disallowed_report_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    clients_path = tmp_path / "clients.toml"
    _write_clients_file(clients_path, allow_local_path=True)
    clients_path.write_text(
        clients_path.read_text(encoding="utf-8").replace(
            'allowed_report_schemas = ["proof-of-audit-report-v1"]',
            'allowed_report_schemas = ["some-other-report-v1"]',
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("POA_SERVICE_API_KEY", "test-service-key")

    app = create_app(
        config=_service_config(
            tmp_path,
            auth_enabled=True,
            clients_path=clients_path,
            allow_local_path_sources=True,
        )
    )
    transport = httpx.ASGITransport(app=app)

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as client,
    ):
        response = await client.post(
            "/v1/runs",
            json=_request_payload(str(repo)),
            headers={"X-Agent-Forge-API-Key": "test-service-key"},
        )
        assert response.status_code == 403
        assert response.json()["detail"]["error"]["code"] == "policy_denied"


@pytest.mark.asyncio
async def test_service_enforces_active_run_quota(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    clients_path = tmp_path / "clients.toml"
    _write_clients_file(clients_path, allow_local_path=True)
    monkeypatch.setenv("POA_SERVICE_API_KEY", "test-service-key")

    app = create_app(
        config=_service_config(
            tmp_path,
            auth_enabled=True,
            clients_path=clients_path,
            allow_local_path_sources=True,
        )
    )

    async def slow_runner(task: Task) -> None:
        await asyncio.sleep(0.1)
        record = app.state.service._records[task.id]
        record.started_at = record.created_at
        record.completed_at = record.created_at
        record.report_path.parent.mkdir(parents=True, exist_ok=True)
        record.report_path.write_text(
            json.dumps(
                {
                    "schema_version": "proof-of-audit-report-v1",
                    "run_id": task.id,
                    "summary": "ok",
                    "confidence": "high",
                    "findings": [],
                    "stats": {
                        "finding_count": 0,
                        "max_severity": None,
                        "severity_breakdown": {
                            "critical": 0,
                            "high": 0,
                            "medium": 0,
                            "low": 0,
                        },
                    },
                }
            ),
            encoding="utf-8",
        )

    app.state.service._worker._task_runner = slow_runner
    transport = httpx.ASGITransport(app=app)

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as client,
    ):
        headers = {"X-Agent-Forge-API-Key": "test-service-key"}
        first = await client.post("/v1/runs", json=_request_payload(str(repo)), headers=headers)
        assert first.status_code == 202

        second = await client.post("/v1/runs", json=_request_payload(str(repo)), headers=headers)
        assert second.status_code == 429
        assert second.json()["detail"]["error"]["code"] == "quota_exceeded"


@pytest.mark.asyncio
async def test_service_prevents_cross_client_run_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    clients_path = tmp_path / "clients.toml"
    _write_clients_file(clients_path, allow_local_path=True, include_secondary_client=True)
    monkeypatch.setenv("POA_SERVICE_API_KEY", "test-service-key")
    monkeypatch.setenv("OTHER_SERVICE_API_KEY", "other-service-key")

    app = create_app(
        config=_service_config(
            tmp_path,
            auth_enabled=True,
            clients_path=clients_path,
            allow_local_path_sources=True,
        )
    )
    service = app.state.service

    async def fake_runner(task: Task) -> None:
        record = service._records[task.id]
        record.started_at = record.created_at
        record.completed_at = record.created_at
        record.report_path.parent.mkdir(parents=True, exist_ok=True)
        record.report_path.write_text(
            json.dumps(
                {
                    "schema_version": "proof-of-audit-report-v1",
                    "run_id": task.id,
                    "summary": "ok",
                    "confidence": "high",
                    "findings": [],
                    "stats": {
                        "finding_count": 0,
                        "max_severity": None,
                        "severity_breakdown": {
                            "critical": 0,
                            "high": 0,
                            "medium": 0,
                            "low": 0,
                        },
                    },
                }
            ),
            encoding="utf-8",
        )

    service._worker._task_runner = fake_runner
    transport = httpx.ASGITransport(app=app)

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as client,
    ):
        create_response = await client.post(
            "/v1/runs",
            json=_request_payload(str(repo)),
            headers={"X-Agent-Forge-API-Key": "test-service-key"},
        )
        assert create_response.status_code == 202
        run_id = create_response.json()["run_id"]

        status_response = await client.get(
            f"/v1/runs/{run_id}",
            headers={"X-Agent-Forge-API-Key": "other-service-key"},
        )
        assert status_response.status_code == 403
        assert status_response.json()["detail"]["error"]["code"] == "policy_denied"


@pytest.mark.asyncio
async def test_service_writes_audit_log_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Vault.sol").write_text("contract Vault {}\n", encoding="utf-8")
    clients_path = tmp_path / "clients.toml"
    _write_clients_file(clients_path, allow_local_path=True)
    monkeypatch.setenv("POA_SERVICE_API_KEY", "test-service-key")

    app = create_app(
        config=_service_config(
            tmp_path,
            auth_enabled=True,
            clients_path=clients_path,
            allow_local_path_sources=True,
            max_source_size_bytes=1,
        )
    )
    transport = httpx.ASGITransport(app=app)

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as client,
    ):
        denied = await client.post(
            "/v1/runs",
            json=_request_payload(str(repo)),
            headers={
                "X-Agent-Forge-API-Key": "test-service-key",
                "User-Agent": "pytest-service-app",
                "X-Forwarded-For": "203.0.113.7",
            },
        )
        assert denied.status_code == 403

    audit_log = tmp_path / "service-root" / "audit" / "events.jsonl"
    lines = audit_log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["event"] == "run.denied"
    assert payload["client_service_id"] == "proof-of-audit-auditor"
    assert payload["profile_id"] == "proof-of-audit-solidity-v1"
    assert payload["final_status"] == "rejected"
    assert payload["reason"] == "source exceeds max size: 18"
    assert payload["request_origin"] == "203.0.113.7"
    assert payload["user_agent"] == "pytest-service-app"
    assert payload["submitted_at"] is not None
