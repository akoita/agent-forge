"""Unit tests for the shared agent runner and optional service/gcs extras (#141)."""

from __future__ import annotations

import importlib
import subprocess
import sys
import tomllib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from agent_forge.agent.models import AgentConfig, AgentRun, RunState
from agent_forge.agent.runner import execute_agent_run, run_agent_task
from agent_forge.cli import main
from agent_forge.config import ForgeConfig, ProviderSettings, ServiceSettings
from agent_forge.orchestration.events import EventBus
from agent_forge.orchestration.queue import Task
from agent_forge.sandbox.base import SandboxConfig
from agent_forge.service import HostedServiceClient, HostedServiceClientError, create_app

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_pyproject_separates_service_and_gcs_extras() -> None:
    """Base dependencies must omit fastapi, uvicorn, and google-cloud-storage."""
    data = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps = [dep.lower() for dep in data["project"]["dependencies"]]
    for forbidden in ("fastapi", "uvicorn", "google-cloud-storage"):
        assert not any(dep.startswith(forbidden) for dep in deps), forbidden

    extras = data["project"]["optional-dependencies"]
    service_deps = [dep.lower() for dep in extras["service"]]
    gcs_deps = [dep.lower() for dep in extras["gcs"]]
    assert any(dep.startswith("fastapi") for dep in service_deps)
    assert any(dep.startswith("uvicorn") for dep in service_deps)
    assert any(dep.startswith("google-cloud-storage") for dep in gcs_deps)


def test_service_package_imports_without_fastapi(monkeypatch: pytest.MonkeyPatch) -> None:
    """Client exports import without fastapi; create_app fails with install guidance."""
    monkeypatch.delitem(sys.modules, "agent_forge.service.app", raising=False)
    monkeypatch.setitem(sys.modules, "fastapi", None)
    monkeypatch.setitem(sys.modules, "fastapi.responses", None)

    assert HostedServiceClient is not None
    assert HostedServiceClientError is not None

    with pytest.raises(RuntimeError, match=r"agent-forge\[service\]"):
        create_app()


def test_service_package_import_in_fresh_subprocess_does_not_load_fastapi() -> None:
    """Importing agent_forge.service does not eagerly load fastapi or app."""
    code = (
        "import sys\n"
        "import agent_forge.service as svc\n"
        "assert svc.HostedServiceClient is not None\n"
        "assert 'fastapi' not in sys.modules\n"
        "assert 'agent_forge.service.app' not in sys.modules\n"
    )
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0


def test_service_app_import_error_mentions_service_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Importing agent_forge.service.app without fastapi explains the service extra."""
    monkeypatch.delitem(sys.modules, "agent_forge.service.app", raising=False)
    monkeypatch.setitem(sys.modules, "fastapi", None)
    monkeypatch.setitem(sys.modules, "fastapi.responses", None)

    with pytest.raises(ImportError, match=r"agent-forge\[service\]"):
        importlib.import_module("agent_forge.service.app")


def test_cli_serve_missing_service_extras_exits_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Running `agent-forge serve` without uvicorn gives actionable install guidance."""
    monkeypatch.setitem(sys.modules, "uvicorn", None)
    runner = CliRunner()
    result = runner.invoke(main, ["serve"])
    assert result.exit_code == 1
    assert "agent-forge[service]" in result.output


@pytest.mark.asyncio
async def test_execute_agent_run_runs_hook_and_cleans_up_on_error() -> None:
    """execute_agent_run calls post_run_hook before teardown and cleans up on error."""
    order: list[str] = []

    sandbox = MagicMock()

    async def start(**_kwargs: object) -> None:
        order.append("sandbox.start")

    async def stop() -> None:
        order.append("sandbox.stop")

    sandbox.start = AsyncMock(side_effect=start)
    sandbox.stop = AsyncMock(side_effect=stop)

    llm = MagicMock()

    async def close() -> None:
        order.append("llm.close")

    llm.close = AsyncMock(side_effect=close)

    tools = MagicMock()
    agent_run = AgentRun(task="fix bug", repo_path="/tmp/repo", config=AgentConfig(model="m"))
    sandbox_cfg = SandboxConfig()

    async def fake_loop(run: AgentRun, *_args: object, **_kwargs: object) -> AgentRun:
        order.append("react_loop")
        run.state = RunState.COMPLETED
        return run

    async def hook(*_args: object) -> None:
        order.append("post_run_hook")
        assert sandbox.stop.await_count == 0
        assert llm.close.await_count == 0

    result = await execute_agent_run(
        agent_run,
        sandbox_cfg,
        llm,
        tools,
        sandbox,
        post_run_hook=hook,
        react_loop_fn=fake_loop,
    )
    assert result.state == RunState.COMPLETED
    assert order == [
        "sandbox.start",
        "react_loop",
        "post_run_hook",
        "sandbox.stop",
        "llm.close",
    ]

    # Cleanup still runs both stop() and close() if react_loop fails
    sandbox.stop = AsyncMock(side_effect=RuntimeError("stop boom"))
    llm.close = AsyncMock()

    async def failing_loop(*_args: object, **_kwargs: object) -> AgentRun:
        raise ValueError("loop boom")

    with pytest.raises(RuntimeError, match="stop boom"):
        await execute_agent_run(
            agent_run,
            sandbox_cfg,
            llm,
            tools,
            sandbox,
            react_loop_fn=failing_loop,
        )
    sandbox.stop.assert_awaited_once()
    llm.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_cli_and_service_share_run_agent_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both CLI queued mode and HostedRunService delegate execution to run_agent_task."""
    from agent_forge.cli import _make_task_runner
    from agent_forge.service.app import HostedRunRecord, HostedRunService
    from agent_forge.service.models import ClientRef, ProfileRef, RunRequest, SourceRef

    cfg = ForgeConfig(
        providers={"gemini": ProviderSettings(api_key_env="TEST_GEMINI_KEY", default_model="m")},
        service=ServiceSettings(root_dir=str(tmp_path / "svc"), auth_enabled=False),
    )
    cfg.agent.default_provider = "gemini"
    monkeypatch.setenv("TEST_GEMINI_KEY", "test-key")

    cli_task = Task(
        id="cli-task-1",
        task_description="cli queued task",
        repo_path=str(tmp_path),
        config=AgentConfig(model="m"),
    )
    cli_bus = EventBus()
    with patch("agent_forge.agent.runner.run_agent_task", new_callable=AsyncMock) as mock_run:
        cli_runner = _make_task_runner(cfg, "gemini", "test-key", cli_bus)
        await cli_runner(cli_task)

    mock_run.assert_awaited_once()
    assert mock_run.call_args.kwargs["discover_extensions"] is False
    assert mock_run.call_args.kwargs["emit_lifecycle"] is False
    assert mock_run.call_args.kwargs["event_bus"] is cli_bus

    # Now verify HostedRunService uses the same shared runner helper
    from datetime import UTC, datetime

    service = HostedRunService(service_root=tmp_path / "svc", config=cfg)
    repo = tmp_path / "repo"
    repo.mkdir()
    report_path = repo / ".agent-forge" / "report.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(
        '{"schema_version": "agent-forge-report-v1", "run_id": "svc-run-1", "summary": "ok"}',
        encoding="utf-8",
    )
    record = HostedRunRecord(
        run_id="svc-run-1",
        request=RunRequest(
            schema_version="agent-forge-run-request-v1",
            client=ClientRef(name="test", request_id="r1"),
            profile=ProfileRef(id="gemini", report_schema="agent-forge-report-v1"),
            source=SourceRef(kind="local_path", uri=str(repo)),
        ),
        workspace_dir=repo,
        created_at=datetime.now(UTC),
    )
    service._records["svc-run-1"] = record
    svc_task = Task(
        id="svc-run-1",
        task_description="hosted task",
        repo_path=str(repo),
        config=AgentConfig(model="m"),
    )
    svc_runner = service._make_task_runner()

    with patch("agent_forge.service.app.run_agent_task", new_callable=AsyncMock) as mock_svc_run:
        await svc_runner(svc_task)

    mock_svc_run.assert_awaited_once()
    assert mock_svc_run.call_args.kwargs["report_schema"] == "agent-forge-report-v1"
    assert mock_svc_run.call_args.kwargs["post_run_hook"] is not None
    assert callable(run_agent_task)
