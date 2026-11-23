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
    """Tests verifying contract-driven single-loop remediation for hosted report emission."""

    @pytest.fixture(autouse=True)
    def _isolate_user_config_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg_dir = tmp_path / "user-cfg"
        monkeypatch.setattr("agent_forge.agent.persistence.USER_CONFIG_DIR", cfg_dir)
        monkeypatch.setattr(
            "agent_forge.agent.core.save_summary",
            lambda *_a: cfg_dir / "summary.json",
        )

    @staticmethod
    def _local_sandbox(repo: Path):
        from agent_forge.sandbox.base import ExecResult, Sandbox, SandboxConfig

        class _LocalRepoSandbox(Sandbox):
            async def start(self, repo_path: str, config: SandboxConfig | None = None) -> None:
                del repo_path, config

            async def stop(self) -> None:
                return None

            async def is_alive(self) -> bool:
                return True

            def _resolve(self, path: str) -> Path:
                rel = path.removeprefix("/workspace").lstrip("/")
                return repo / rel

            async def read_file(self, path: str) -> str:
                return self._resolve(path).read_text(encoding="utf-8")

            async def write_file(self, path: str, content: str) -> None:
                target = self._resolve(path)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")

            async def exec(self, command: str, *, timeout_seconds: int = 30) -> ExecResult:
                del timeout_seconds
                if command.startswith("mkdir -p "):
                    self._resolve(command.removeprefix("mkdir -p ").strip()).mkdir(
                        parents=True, exist_ok=True
                    )
                return ExecResult(exit_code=0, stdout="", stderr="")

        return _LocalRepoSandbox()

    @pytest.mark.asyncio
    async def test_recovery_writes_report_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Single-loop contract remediation writes report.json and completes without 2nd run."""
        from agent_forge.agent.models import AgentConfig
        from agent_forge.llm.base import LLMResponse, TokenUsage, ToolCall
        from agent_forge.orchestration.queue import Task

        service = _make_service(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "Vault.sol").write_text("contract Vault {}\n", encoding="utf-8")

        service._config.agent.default_provider = "gemini"
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")

        task_config = AgentConfig(model="test-model", max_iterations=5)
        task = Task(
            id="run_recovery_ok",
            task_description="Inspect this contract",
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

        llm = AsyncMock()
        llm.complete.side_effect = [
            LLMResponse(
                content="Done inspecting.",
                tool_calls=[],
                usage=TokenUsage(10, 5, 15),
                finish_reason="stop",
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="c-report",
                        name="write_file",
                        arguments={
                            "path": "/workspace/.agent-forge/report.json",
                            "content": json.dumps(_valid_report_payload(run_id=task.id)),
                        },
                    )
                ],
                usage=TokenUsage(10, 10, 20),
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content="Wrote report.",
                tool_calls=[],
                usage=TokenUsage(5, 5, 10),
                finish_reason="stop",
            ),
        ]
        llm.close = AsyncMock()

        with (
            patch("agent_forge.service.app.create_sandbox", return_value=self._local_sandbox(repo)),
            patch("agent_forge.service.app.create_provider", return_value=llm),
        ):
            runner = service._make_task_runner()
            await runner(task)

        assert llm.complete.await_count == 3
        assert record.error is None
        assert record.completed_at is not None

    @pytest.mark.asyncio
    async def test_recovery_fails_still_missing_report(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When remediation stops again without report.json, run fails with report error."""
        from agent_forge.agent.models import AgentConfig
        from agent_forge.llm.base import LLMResponse, TokenUsage
        from agent_forge.orchestration.queue import Task

        service = _make_service(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "Vault.sol").write_text("contract Vault {}\n", encoding="utf-8")

        monkeypatch.setenv("GEMINI_API_KEY", "test-key")

        task_config = AgentConfig(model="test-model", max_iterations=5)
        task = Task(
            id="run_recovery_fail",
            task_description="Inspect this contract",
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

        llm = AsyncMock()
        llm.complete.side_effect = [
            LLMResponse(
                content="Stop 1.", tool_calls=[], usage=TokenUsage(5, 5, 10), finish_reason="stop"
            ),
            LLMResponse(
                content="Stop 2.", tool_calls=[], usage=TokenUsage(5, 5, 10), finish_reason="stop"
            ),
        ]
        llm.close = AsyncMock()

        with (
            patch("agent_forge.service.app.create_provider", return_value=llm),
            patch("agent_forge.service.app.create_sandbox", return_value=self._local_sandbox(repo)),
        ):
            runner = service._make_task_runner()
            with pytest.raises(RuntimeError, match="run completed without writing"):
                await runner(task)

        assert llm.complete.await_count == 2
        assert record.error is not None
        assert record.error.code == "report_generation_failed"

    @pytest.mark.asyncio
    async def test_no_recovery_when_report_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the primary run writes the report on turn 1, no remediation occurs."""
        from agent_forge.agent.models import AgentConfig
        from agent_forge.llm.base import LLMResponse, TokenUsage, ToolCall
        from agent_forge.orchestration.queue import Task

        service = _make_service(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "Vault.sol").write_text("contract Vault {}\n", encoding="utf-8")

        monkeypatch.setenv("GEMINI_API_KEY", "test-key")

        task_config = AgentConfig(model="test-model", max_iterations=5)
        task = Task(
            id="run_no_recovery",
            task_description="Inspect this contract",
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

        llm = AsyncMock()
        llm.complete.side_effect = [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="c-write",
                        name="write_file",
                        arguments={
                            "path": "/workspace/.agent-forge/report.json",
                            "content": json.dumps(_valid_report_payload(run_id=task.id)),
                        },
                    )
                ],
                usage=TokenUsage(10, 10, 20),
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content="Done.", tool_calls=[], usage=TokenUsage(5, 5, 10), finish_reason="stop"
            ),
        ]
        llm.close = AsyncMock()

        with (
            patch("agent_forge.service.app.create_provider", return_value=llm),
            patch("agent_forge.service.app.create_sandbox", return_value=self._local_sandbox(repo)),
        ):
            runner = service._make_task_runner()
            await runner(task)

        assert llm.complete.await_count == 2
        assert record.error is None
        assert record.completed_at is not None
