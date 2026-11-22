"""Unit tests for sandbox hardening, qualification harness, and cancellation (#157)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from docker.errors import APIError

from agent_forge.agent.models import AgentRun, RunState
from agent_forge.config import ForgeConfig, ProviderSettings, ServiceSettings
from agent_forge.sandbox.base import SandboxConfig, sanitize_sandbox_env
from agent_forge.sandbox.bwrap import BwrapSandbox
from agent_forge.sandbox.docker import DockerSandbox
from agent_forge.service import HostedServiceClient, HostedServiceClientError, create_app

if TYPE_CHECKING:
    from pathlib import Path

    from agent_forge.service.app import HostedRunService


def test_sanitize_sandbox_env_strips_credentials() -> None:
    """Sensitive API keys, tokens, passwords, and cloud credentials are stripped."""
    cleaned = sanitize_sandbox_env(
        {
            "SAFE_VAR": "123",
            "PATH": "/usr/bin",
            "GEMINI_API_KEY": "secret",
            "openai_api_key": "secret",
            "AWS_SECRET_ACCESS_KEY": "aws-secret",
            "AWS_ACCESS_KEY_ID": "aws-id",
            "AWS_SESSION_TOKEN": "aws-tok",
            "GOOGLE_APPLICATION_CREDENTIALS": "/tmp/creds.json",
            "SERVICE_TOKEN": "tok",
            "DB_PASSWORD": "pw",
            "APP_SECRET": "shh",
        }
    )
    assert cleaned == {"SAFE_VAR": "123", "PATH": "/usr/bin"}


@pytest.mark.asyncio
async def test_docker_sandbox_drops_caps_sanitizes_env_quotes_paths_and_force_removes() -> None:
    """DockerSandbox hardens container capabilities, env vars, paths, and cleanup."""
    client = MagicMock()
    container = MagicMock()
    container.id = "deadbeef1234"
    container.short_id = "deadbeef"
    container.status = "running"
    container.exec_run.return_value = (0, (b"ok", b""))
    container.stop.side_effect = APIError("stop timed out")
    client.containers.run.return_value = container

    with patch("agent_forge.sandbox.docker.docker") as mock_docker:
        mock_docker.from_env.return_value = client
        sandbox = DockerSandbox()
        cfg = SandboxConfig(
            env_vars={"SAFE_ENV": "yes", "GEMINI_API_KEY": "must-strip", "MY_TOKEN": "nope"}
        )
        await sandbox.start("/tmp/repo", config=cfg)

        run_kwargs = client.containers.run.call_args.kwargs
        assert run_kwargs["init"] is True
        assert run_kwargs["cap_drop"] == ["ALL"]
        assert "no-new-privileges" in run_kwargs["security_opt"]
        assert run_kwargs["environment"] == {"SAFE_ENV": "yes"}
        assert sandbox.container_id == "deadbeef1234"

        await sandbox.write_file("/workspace/dir with spaces/file;name.txt", "hello")
        exec_calls = [call.args[0] for call in container.exec_run.call_args_list]
        assert any("'/workspace/dir with spaces'" in cmd[2] for cmd in exec_calls)
        assert any("'/workspace/dir with spaces/file;name.txt'" in cmd[2] for cmd in exec_calls)

        await sandbox.stop()
        container.remove.assert_called_once_with(force=True)
        assert sandbox.container_id is None


@pytest.mark.asyncio
async def test_bwrap_sandbox_clears_env_and_kills_on_cancel_and_stop(tmp_path: Path) -> None:
    """BwrapSandbox passes --clearenv + env={}, kills active processes on cancel/stop."""
    process = AsyncMock()
    process.kill = MagicMock()
    process.returncode = None

    async def slow_communicate() -> tuple[bytes, bytes]:
        await asyncio.sleep(10)
        return (b"", b"")

    process.communicate = AsyncMock(side_effect=slow_communicate)

    with (
        patch("agent_forge.sandbox.bwrap.platform.system", return_value="Linux"),
        patch("agent_forge.sandbox.bwrap.shutil.which", return_value="/usr/bin/bwrap"),
        patch(
            "agent_forge.sandbox.bwrap.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=process,
        ) as mock_exec,
    ):
        sandbox = BwrapSandbox()
        await sandbox.start(
            str(tmp_path),
            SandboxConfig(
                backend="bwrap",
                env_vars={"SAFE": "ok", "GEMINI_API_KEY": "secret"},
            ),
        )
        task = asyncio.create_task(sandbox.exec("sleep 10", timeout_seconds=30))
        await asyncio.sleep(0.05)

        argv = mock_exec.await_args.args
        kwargs = mock_exec.await_args.kwargs
        assert "--clearenv" in argv
        assert "--remount-ro" in argv
        assert kwargs["env"] == {}
        assert "GEMINI_API_KEY" not in argv
        assert "SAFE" in argv

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        process.kill.assert_called()

        # Also verify stop() reaps tracked active processes
        process.kill.reset_mock()
        process.returncode = None
        sandbox._active_processes.add(process)
        await sandbox.stop()
        process.kill.assert_called_once()
        assert len(sandbox._active_processes) == 0


def _write_cancel_clients_file(path: Path) -> None:
    path.write_text(
        """
[clients.client-a]
api_key_env = "CLIENT_A_KEY"
allowed_profiles = ["gemini"]
allowed_report_schemas = ["agent-forge-report-v1"]
allowed_source_kinds = ["local_path"]
max_active_runs = 2
max_runs_per_day = 10
allow_local_path = true

[clients.client-b]
api_key_env = "CLIENT_B_KEY"
allowed_profiles = ["gemini"]
allowed_report_schemas = ["agent-forge-report-v1"]
allowed_source_kinds = ["local_path"]
max_active_runs = 2
max_runs_per_day = 10
allow_local_path = true
""".lstrip(),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_hosted_service_cancel_queued_and_active_runs_and_cleans_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /v1/runs/{run_id}/cancel stops tasks, enforces auth, and cleans workspace."""
    repo = tmp_path / "source-repo"
    repo.mkdir()
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    clients_path = tmp_path / "clients.toml"
    _write_cancel_clients_file(clients_path)
    monkeypatch.setenv("CLIENT_A_KEY", "key-a")
    monkeypatch.setenv("CLIENT_B_KEY", "key-b")
    monkeypatch.setenv("GEMINI_API_KEY", "gem-key")

    cfg = ForgeConfig(
        providers={"gemini": ProviderSettings(api_key_env="GEMINI_API_KEY", default_model="m")},
        service=ServiceSettings(
            root_dir=str(tmp_path / "service-root"),
            auth_enabled=True,
            clients_path=str(clients_path),
            allow_local_path_sources=True,
        ),
    )

    entered = asyncio.Event()
    release = asyncio.Event()
    stopped_sandboxes: list[bool] = []

    fake_sandbox = MagicMock()

    async def stop_sandbox() -> None:
        stopped_sandboxes.append(True)

    fake_sandbox.stop = AsyncMock(side_effect=stop_sandbox)

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
        kwargs["on_sandbox_started"](fake_sandbox)
        entered.set()
        await release.wait()
        run = AgentRun(task=task, repo_path=repo_path, config=agent_config, id=run_id)
        run.state = RunState.CANCELLED
        return run

    app = create_app(config=cfg)
    service: HostedRunService = app.state.service

    with patch("agent_forge.service.app.run_agent_task", side_effect=fake_run_agent_task):
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as http_client,
        ):
            client_a = HostedServiceClient(
                base_url="http://testserver",
                api_key="key-a",
                http_client=http_client,
            )
            client_b = HostedServiceClient(
                base_url="http://testserver",
                api_key="key-b",
                http_client=http_client,
            )

            # Submit active run and wait until sandbox has started
            resp1 = await http_client.post(
                "/v1/runs",
                json={
                    "schema_version": "agent-forge-run-request-v1",
                    "client": {"name": "a", "request_id": "r1", "service_id": "client-a"},
                    "profile": {"id": "gemini", "report_schema": "agent-forge-report-v1"},
                    "source": {"kind": "local_path", "uri": str(repo)},
                },
                headers={"X-Agent-Forge-API-Key": "key-a"},
            )
            assert resp1.status_code == 202
            run1_id = resp1.json()["run_id"]
            await entered.wait()

            workspace1 = service._records[run1_id].workspace_dir
            assert workspace1.exists()

            # Cross-client cancel must be denied with 403
            with pytest.raises(HostedServiceClientError) as exc_info:
                await client_b.cancel_run(run1_id)
            assert exc_info.value.status_code == 403

            # Owner cancels active run -> sandbox stopped, workspace cleaned, source repo untouched
            cancelled = await client_a.cancel_run(run1_id)
            assert cancelled.status == "cancelled"
            assert stopped_sandboxes == [True]
            assert not workspace1.exists()
            assert (repo / "app.py").exists()

            # Even once blocked worker resumes, state remains cancelled
            release.set()
            await asyncio.sleep(0.05)
            status_after = await client_a.get_status(run1_id)
            assert status_after.status == "cancelled"

            # Unknown run returns 404
            with pytest.raises(HostedServiceClientError) as missing_exc:
                await client_a.cancel_run("run_does_not_exist")
            assert missing_exc.value.status_code == 404
