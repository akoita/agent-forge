from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from fastapi.testclient import TestClient

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


def test_service_create_run_and_return_status_contract(tmp_path: Path) -> None:
    app = create_app(service_root=tmp_path / "service-root")
    service = app.state.service

    async def fake_runner(task: Task) -> None:
        record = service._records[task.id]
        record.started_at = record.created_at
        await asyncio.sleep(0.01)
        record.completed_at = record.created_at

    service._worker._task_runner = fake_runner

    with TestClient(app) as client:
        response = client.post("/v1/runs", json=_request_payload(str(tmp_path / "repo")))
        assert response.status_code == 202
        accepted = response.json()
        assert accepted["schema_version"] == "agent-forge-run-v1"
        assert accepted["status"] == "accepted"
        assert accepted["status_url"].startswith("/v1/runs/run_")
        assert {artifact["kind"] for artifact in accepted["artifacts"]} == {
            "report",
            "run_metadata",
            "logs",
        }
        run_id = accepted["run_id"]

        for _ in range(40):
            status_response = client.get(f"/v1/runs/{run_id}")
            assert status_response.status_code == 200
            if status_response.json()["status"] == "completed":
                break
            asyncio.run(asyncio.sleep(0.01))
        else:
            raise AssertionError("run did not complete")

        completed = status_response.json()
        assert completed["profile"]["id"] == "proof-of-audit-solidity-v1"
        assert completed["artifacts"][0]["available"] is True


def test_service_rejects_unknown_run_id(tmp_path: Path) -> None:
    app = create_app(service_root=tmp_path / "service-root")

    with TestClient(app) as client:
        response = client.get("/v1/runs/run_missing")
        assert response.status_code == 404
        detail = response.json()["detail"]
        assert detail["error"]["code"] == "invalid_request"


def test_service_request_contract_rejects_unknown_fields(tmp_path: Path) -> None:
    app = create_app(service_root=tmp_path / "service-root")
    payload = _request_payload(str(tmp_path / "repo"))
    payload["unexpected"] = "value"

    with TestClient(app) as client:
        response = client.post("/v1/runs", json=payload)
        assert response.status_code == 422


def test_service_can_omit_logs_artifact(tmp_path: Path) -> None:
    app = create_app(service_root=tmp_path / "service-root")
    payload = _request_payload(str(tmp_path / "repo"))
    payload["artifacts"] = {"result_delivery": "pull", "include_logs": False}

    with TestClient(app) as client:
        response = client.post("/v1/runs", json=payload)
        assert response.status_code == 202
        kinds = {artifact["kind"] for artifact in response.json()["artifacts"]}
        assert kinds == {"report", "run_metadata"}
