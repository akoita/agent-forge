from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx
import pytest

from agent_forge.config import ForgeConfig, ServiceSettings
from agent_forge.service import (
    HostedServiceClientError,
    ProofOfAuditHostedClient,
    build_proof_of_audit_request,
    create_app,
)

if TYPE_CHECKING:
    from pathlib import Path

    from agent_forge.orchestration.queue import Task


def _service_config(
    tmp_path: Path,
    *,
    clients_path: Path,
    allow_local_path_sources: bool = True,
) -> ForgeConfig:
    return ForgeConfig(
        service=ServiceSettings(
            root_dir=str(tmp_path / "service-root"),
            auth_enabled=True,
            clients_path=str(clients_path),
            allow_local_path_sources=allow_local_path_sources,
        )
    )


def _write_clients_file(path: Path) -> None:
    path.write_text(
        """
[clients.proof-of-audit-auditor]
api_key_env = "POA_SERVICE_API_KEY"
allowed_profiles = ["proof-of-audit-solidity-v1"]
allowed_source_kinds = ["local_path", "archive_uri"]
max_active_runs = 1
max_runs_per_day = 5
allow_local_path = true
""".lstrip(),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_proof_of_audit_client_completes_hosted_flow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Vault.sol").write_text("contract Vault {}\n", encoding="utf-8")
    clients_path = tmp_path / "clients.toml"
    _write_clients_file(clients_path)
    monkeypatch.setenv("POA_SERVICE_API_KEY", "test-service-key")

    app = create_app(config=_service_config(tmp_path, clients_path=clients_path))
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
                    "summary": "Potential reentrancy issue detected",
                    "confidence": "high",
                    "target": {
                        "submission_kind": "deployed_address",
                        "network": "base-sepolia",
                        "chain_id": 84532,
                        "contract_address": "0xabc",
                        "entry_contract": "Vault",
                    },
                    "findings": [
                        {
                            "finding_id": "POA-1",
                            "title": "Reentrancy on withdraw",
                            "severity": "high",
                            "category": "reentrancy",
                            "description": "External call before balance update.",
                            "impact": "Attacker can drain funds.",
                            "recommendation": "Use checks-effects-interactions.",
                            "confidence": "high",
                        }
                    ],
                    "stats": {
                        "finding_count": 1,
                        "max_severity": "high",
                        "severity_breakdown": {
                            "critical": 0,
                            "high": 1,
                            "medium": 0,
                            "low": 0,
                        },
                    },
                }
            ),
            encoding="utf-8",
        )

    service._worker._task_runner = fake_runner

    request = build_proof_of_audit_request(
        request_id="audit-123",
        service_id="proof-of-audit-auditor",
        source_uri=str(repo),
        source_kind="local_path",
        entry_contract="Vault",
        contract_address="0xabc",
        network="base-sepolia",
        chain_id=84532,
        source_digest="sha256:test",
    )

    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as http_client,
    ):
        client = ProofOfAuditHostedClient(
            base_url="http://testserver",
            api_key="test-service-key",
            http_client=http_client,
        )
        submitted = await client.submit_run(request)
        report = await client.wait_for_report(submitted.run_id)

    assert submitted.status in {"accepted", "queued", "running", "completed"}
    assert report.schema_version == "proof-of-audit-report-v1"
    assert report.target is not None
    assert report.target.network == "base-sepolia"
    assert report.stats.finding_count == 1
    assert report.findings[0].category == "reentrancy"


@pytest.mark.asyncio
async def test_proof_of_audit_client_maps_auth_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Vault.sol").write_text("contract Vault {}\n", encoding="utf-8")
    clients_path = tmp_path / "clients.toml"
    _write_clients_file(clients_path)
    monkeypatch.setenv("POA_SERVICE_API_KEY", "expected-key")

    app = create_app(config=_service_config(tmp_path, clients_path=clients_path))
    request = build_proof_of_audit_request(
        request_id="audit-123",
        service_id="proof-of-audit-auditor",
        source_uri=str(repo),
        source_kind="local_path",
        entry_contract="Vault",
        contract_address="0xabc",
        network="base-sepolia",
        chain_id=84532,
    )

    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as http_client,
    ):
        client = ProofOfAuditHostedClient(
            base_url="http://testserver",
            api_key="wrong-key",
            http_client=http_client,
        )
        with pytest.raises(HostedServiceClientError) as exc_info:
            await client.submit_run(request)

    assert exc_info.value.status_code == 401
    assert exc_info.value.code == "unauthorized"


@pytest.mark.asyncio
async def test_proof_of_audit_client_maps_failed_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Vault.sol").write_text("contract Vault {}\n", encoding="utf-8")
    clients_path = tmp_path / "clients.toml"
    _write_clients_file(clients_path)
    monkeypatch.setenv("POA_SERVICE_API_KEY", "test-service-key")

    app = create_app(config=_service_config(tmp_path, clients_path=clients_path))
    service = app.state.service

    async def failing_runner(task: Task) -> None:
        record = service._records[task.id]
        record.started_at = record.created_at
        record.completed_at = record.created_at
        msg = "sandbox rejected the analysis request"
        record.error = {
            "code": "sandbox_execution_failed",
            "message": msg,
            "retryable": False,
        }
        raise RuntimeError(msg)

    service._worker._task_runner = failing_runner

    request = build_proof_of_audit_request(
        request_id="audit-123",
        service_id="proof-of-audit-auditor",
        source_uri=str(repo),
        source_kind="local_path",
        entry_contract="Vault",
        contract_address="0xabc",
        network="base-sepolia",
        chain_id=84532,
    )

    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as http_client,
    ):
        client = ProofOfAuditHostedClient(
            base_url="http://testserver",
            api_key="test-service-key",
            http_client=http_client,
        )
        submitted = await client.submit_run(request)
        with pytest.raises(HostedServiceClientError) as exc_info:
            await client.wait_for_report(submitted.run_id, poll_interval_seconds=0.01)

    assert exc_info.value.code == "sandbox_execution_failed"
