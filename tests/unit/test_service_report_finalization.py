"""Unit tests for hosted report finalization — validation and recovery logic."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from agent_forge.config import ForgeConfig, ServiceSettings
from agent_forge.service.app import _REPORT_REQUIRED_FIELDS, HostedRunRecord, HostedRunService
from agent_forge.service.models import (
    ClientRef,
    ProfileRef,
    RunRequest,
    SourceRef,
)

if TYPE_CHECKING:
    from pathlib import Path


def _minimal_run_request(source_uri: str) -> RunRequest:
    return RunRequest(
        schema_version="agent-forge-run-request-v1",
        client=ClientRef(
            name="test-client",
            request_id="req-001",
            service_id="test-service",
        ),
        profile=ProfileRef(
            id="proof-of-audit-solidity-v1",
            report_schema="proof-of-audit-report-v1",
        ),
        source=SourceRef(
            kind="local_path",
            uri=source_uri,
            entry_contract="Vault",
        ),
    )


def _make_record(tmp_path: Path) -> HostedRunRecord:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return HostedRunRecord(
        run_id="run_test001",
        request=_minimal_run_request(str(tmp_path / "repo")),
        workspace_dir=workspace,
        created_at=datetime.now(UTC),
    )


def _valid_report_payload(run_id: str = "run_test001") -> dict:
    return {
        "schema_version": "proof-of-audit-report-v1",
        "run_id": run_id,
        "summary": "No issues found.",
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


def _make_service(tmp_path: Path) -> HostedRunService:
    config = ForgeConfig(
        service=ServiceSettings(
            root_dir=str(tmp_path / "service-root"),
            auth_enabled=False,
        )
    )
    return HostedRunService(service_root=tmp_path / "service-root", config=config)


class TestValidateReportArtifact:
    """Tests for HostedRunService._validate_report_artifact()."""

    def test_missing_report_raises_report_generation_failed(self, tmp_path: Path) -> None:
        service = _make_service(tmp_path)
        record = _make_record(tmp_path)
        # report_path does not exist

        with pytest.raises(RuntimeError, match="run completed without writing"):
            service._validate_report_artifact(record)

        assert record.error is not None
        assert record.error.code == "report_generation_failed"

    def test_invalid_json_raises_report_invalid_json(self, tmp_path: Path) -> None:
        service = _make_service(tmp_path)
        record = _make_record(tmp_path)
        record.report_path.parent.mkdir(parents=True, exist_ok=True)
        record.report_path.write_text("this is not json {{{", encoding="utf-8")

        with pytest.raises(RuntimeError, match="not valid JSON"):
            service._validate_report_artifact(record)

        assert record.error is not None
        assert record.error.code == "report_invalid_json"

    def test_missing_required_fields_raises_schema_invalid(self, tmp_path: Path) -> None:
        service = _make_service(tmp_path)
        record = _make_record(tmp_path)
        record.report_path.parent.mkdir(parents=True, exist_ok=True)
        # Valid JSON but missing required fields
        record.report_path.write_text(
            json.dumps({"schema_version": "proof-of-audit-report-v1"}),
            encoding="utf-8",
        )

        with pytest.raises(RuntimeError, match="missing required fields"):
            service._validate_report_artifact(record)

        assert record.error is not None
        assert record.error.code == "report_schema_invalid"

    def test_valid_report_passes_validation(self, tmp_path: Path) -> None:
        service = _make_service(tmp_path)
        record = _make_record(tmp_path)
        record.report_path.parent.mkdir(parents=True, exist_ok=True)
        record.report_path.write_text(
            json.dumps(_valid_report_payload()),
            encoding="utf-8",
        )

        # Should not raise
        service._validate_report_artifact(record)
        assert record.error is None

    def test_all_required_fields_are_checked(self, tmp_path: Path) -> None:
        """Verify each required field individually causes a validation failure."""
        service = _make_service(tmp_path)
        for field_name in _REPORT_REQUIRED_FIELDS:
            record = _make_record(tmp_path)
            record.report_path.parent.mkdir(parents=True, exist_ok=True)
            payload = _valid_report_payload()
            del payload[field_name]
            record.report_path.write_text(json.dumps(payload), encoding="utf-8")

            with pytest.raises(RuntimeError, match="missing required fields"):
                service._validate_report_artifact(record)

            assert record.error is not None
            assert record.error.code == "report_schema_invalid"
            assert field_name in record.error.message


class TestReportRecoveryIntegration:
    """Tests verifying the recovery pass is triggered when report is missing."""

    @pytest.mark.asyncio
    async def test_recovery_writes_report_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When recovery writes the report file, the run should succeed."""
        from agent_forge.agent.models import AgentConfig
        from agent_forge.orchestration.queue import Task

        service = _make_service(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "Vault.sol").write_text("contract Vault {}\n", encoding="utf-8")

        monkeypatch.setenv("GEMINI_API_KEY", "test-key")

        task_config = AgentConfig(model="test-model", max_iterations=5)
        task = Task(
            id="run_recovery_ok",
            task_description="Audit this contract",
            repo_path=str(repo),
            config=task_config,
        )

        record = HostedRunRecord(
            run_id=task.id,
            request=_minimal_run_request(str(repo)),
            workspace_dir=repo,
            created_at=datetime.now(UTC),
        )
        service._records[task.id] = record

        call_count = 0

        async def fake_react_loop(run, llm, tools, sandbox, *, event_bus=None):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                # Recovery pass writes the report
                record.report_path.parent.mkdir(parents=True, exist_ok=True)
                record.report_path.write_text(
                    json.dumps(_valid_report_payload(run_id=task.id)),
                    encoding="utf-8",
                )
            return run

        mock_sandbox = AsyncMock()
        mock_sandbox.start = AsyncMock()
        mock_sandbox.stop = AsyncMock()
        mock_sandbox.is_alive = AsyncMock(return_value=True)

        mock_llm = AsyncMock()
        mock_llm.close = AsyncMock()

        with (
            patch("agent_forge.service.app.react_loop", side_effect=fake_react_loop),
            patch("agent_forge.service.app._create_llm", return_value=mock_llm),
            patch("agent_forge.service.app.create_sandbox", return_value=mock_sandbox),
            patch("agent_forge.service.app.create_default_registry"),
        ):
            runner = service._make_task_runner()
            await runner(task)

        assert call_count == 2, "recovery pass should have been triggered"
        assert record.error is None
        assert record.completed_at is not None

    @pytest.mark.asyncio
    async def test_recovery_fails_still_missing_report(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When recovery also fails to write the report, error is raised."""
        from agent_forge.agent.models import AgentConfig
        from agent_forge.orchestration.queue import Task

        service = _make_service(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "Vault.sol").write_text("contract Vault {}\n", encoding="utf-8")

        monkeypatch.setenv("GEMINI_API_KEY", "test-key")

        task_config = AgentConfig(model="test-model", max_iterations=5)
        task = Task(
            id="run_recovery_fail",
            task_description="Audit this contract",
            repo_path=str(repo),
            config=task_config,
        )

        record = HostedRunRecord(
            run_id=task.id,
            request=_minimal_run_request(str(repo)),
            workspace_dir=repo,
            created_at=datetime.now(UTC),
        )
        service._records[task.id] = record

        async def fake_react_loop(run, llm, tools, sandbox, *, event_bus=None):
            # Never writes the report
            return run

        mock_sandbox = AsyncMock()
        mock_sandbox.start = AsyncMock()
        mock_sandbox.stop = AsyncMock()
        mock_sandbox.is_alive = AsyncMock(return_value=True)

        mock_llm = AsyncMock()
        mock_llm.close = AsyncMock()

        with (
            patch("agent_forge.service.app.react_loop", side_effect=fake_react_loop),
            patch("agent_forge.service.app._create_llm", return_value=mock_llm),
            patch("agent_forge.service.app.create_sandbox", return_value=mock_sandbox),
            patch("agent_forge.service.app.create_default_registry"),
        ):
            runner = service._make_task_runner()
            with pytest.raises(RuntimeError, match="run completed without writing"):
                await runner(task)

        assert record.error is not None
        assert record.error.code == "report_generation_failed"

    @pytest.mark.asyncio
    async def test_no_recovery_when_report_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the primary run writes the report, no recovery is attempted."""
        from agent_forge.agent.models import AgentConfig
        from agent_forge.orchestration.queue import Task

        service = _make_service(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "Vault.sol").write_text("contract Vault {}\n", encoding="utf-8")

        monkeypatch.setenv("GEMINI_API_KEY", "test-key")

        task_config = AgentConfig(model="test-model", max_iterations=5)
        task = Task(
            id="run_no_recovery",
            task_description="Audit this contract",
            repo_path=str(repo),
            config=task_config,
        )

        record = HostedRunRecord(
            run_id=task.id,
            request=_minimal_run_request(str(repo)),
            workspace_dir=repo,
            created_at=datetime.now(UTC),
        )
        service._records[task.id] = record

        call_count = 0

        async def fake_react_loop(run, llm, tools, sandbox, *, event_bus=None):
            nonlocal call_count
            call_count += 1
            # Primary run writes the report
            record.report_path.parent.mkdir(parents=True, exist_ok=True)
            record.report_path.write_text(
                json.dumps(_valid_report_payload(run_id=task.id)),
                encoding="utf-8",
            )
            return run

        mock_sandbox = AsyncMock()
        mock_sandbox.start = AsyncMock()
        mock_sandbox.stop = AsyncMock()
        mock_sandbox.is_alive = AsyncMock(return_value=True)

        mock_llm = AsyncMock()
        mock_llm.close = AsyncMock()

        with (
            patch("agent_forge.service.app.react_loop", side_effect=fake_react_loop),
            patch("agent_forge.service.app._create_llm", return_value=mock_llm),
            patch("agent_forge.service.app.create_sandbox", return_value=mock_sandbox),
            patch("agent_forge.service.app.create_default_registry"),
        ):
            runner = service._make_task_runner()
            await runner(task)

        assert call_count == 1, "recovery should NOT have been triggered"
        assert record.error is None
        assert record.completed_at is not None
