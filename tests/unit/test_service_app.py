from __future__ import annotations

import asyncio
import json
import zipfile
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


def test_service_create_run_and_return_report(tmp_path: Path) -> None:
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
        run_dir = record.run_dir
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run.json").write_text(
            json.dumps(
                {
                    "id": task.id,
                    "task": "audit",
                    "repo_path": str(record.workspace_dir),
                    "state": "completed",
                    "iterations": 1,
                    "total_tokens": {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                    },
                    "config": {
                        "max_iterations": 3,
                        "max_tokens_per_run": 200000,
                        "model": "gemini-3.1-flash-lite-preview",
                        "provider": "gemini",
                        "temperature": 0.0,
                        "system_prompt": None,
                    },
                    "created_at": record.created_at.isoformat(),
                    "completed_at": record.completed_at.isoformat(),
                    "error": None,
                }
            )
            + "\n",
            encoding="utf-8",
        )

    service._worker._task_runner = fake_runner

    with TestClient(app) as client:
        response = client.post("/v1/runs", json=_request_payload(str(repo)))
        assert response.status_code == 202
        run_id = response.json()["run_id"]

        for _ in range(40):
            status_response = client.get(f"/v1/runs/{run_id}")
            assert status_response.status_code == 200
            if status_response.json()["status"] == "completed":
                break
            asyncio.run(asyncio.sleep(0.01))
        else:
            raise AssertionError("run did not complete")

        report_response = client.get(f"/v1/runs/{run_id}/report")
        assert report_response.status_code == 200
        assert report_response.json()["schema_version"] == "proof-of-audit-report-v1"

        logs_response = client.get(f"/v1/runs/{run_id}/logs")
        assert logs_response.status_code == 200
        report_json = logs_response.json()["artifacts"]["report_json"]
        assert report_json.endswith("/.agent-forge/report.json")


def test_service_materializes_zip_sources(tmp_path: Path) -> None:
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

    with TestClient(app) as client:
        response = client.post("/v1/runs", json=payload)
        assert response.status_code == 202
        run_id = response.json()["run_id"]
        record = service._records[run_id]
        assert (record.workspace_dir / "src" / "Vault.sol").exists()


def test_service_rejects_unknown_run_id(tmp_path: Path) -> None:
    app = create_app(service_root=tmp_path / "service-root")

    with TestClient(app) as client:
        response = client.get("/v1/runs/run_missing")
        assert response.status_code == 404
        detail = response.json()["detail"]
        assert detail["error"]["code"] == "invalid_request"


def test_service_rejects_unsupported_profile(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    app = create_app(service_root=tmp_path / "service-root")

    payload = _request_payload(str(repo))
    payload["profile"] = {
        "id": "generic-coding-agent-v1",
        "report_schema": "proof-of-audit-report-v1",
    }

    with TestClient(app) as client:
        response = client.post("/v1/runs", json=payload)
        assert response.status_code == 400
