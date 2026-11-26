"""Unit and security tests for the authenticated ACP WebSocket/HTTP transport and workspace API."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import types
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agent_forge.acp import ACP_PROTOCOL_VERSION, ACPServer
from agent_forge.acp.ws_transport import create_acp_ws_app
from agent_forge.agent.models import AgentConfig, AgentRun, RunState
from agent_forge.agent.session import SessionStore
from agent_forge.cli import main
from agent_forge.config import ForgeConfig
from agent_forge.llm.base import Message, Role
from agent_forge.orchestration.events import Event, EventBus, EventType

if TYPE_CHECKING:
    from pathlib import Path

_TEST_ORIGIN = "http://127.0.0.1:5173"
_TEST_TOKEN = "test-acp-bridge-secret-token"  # noqa: S105 — test fixture token


def _init_git_repo(repo: Path) -> None:
    git_bin = shutil.which("git") or "/usr/bin/git"

    def _run_git(*args: str) -> None:
        subprocess.run([git_bin, *args], cwd=repo, check=True, capture_output=True)  # noqa: S603

    repo.mkdir(parents=True, exist_ok=True)
    _run_git("init")
    _run_git("config", "user.email", "test@example.com")
    _run_git("config", "user.name", "Tester")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    _run_git("add", ".")
    _run_git("commit", "-m", "init")


def _make_app(
    tmp_path: Path,
    *,
    bearer_token: str | None = _TEST_TOKEN,
    rate_limit_requests: int = 120,
    watch_interval_seconds: float = 0.05,
    run_executor: Any = None,
) -> tuple[Any, Path]:
    repo = tmp_path / "repo"
    if not (repo / ".git").exists():
        _init_git_repo(repo)
    sessions_dir = tmp_path / "sessions"
    store = SessionStore(base_dir=sessions_dir)
    cfg = ForgeConfig()
    cfg.agent.approval_mode = "prompt"  # type: ignore[assignment]

    def _server_factory() -> ACPServer:
        return ACPServer(
            config=cfg,
            approval_mode="prompt",
            session_store=store,
            run_executor=run_executor,
        )

    app = create_acp_ws_app(
        workspace_root=repo,
        server_factory=_server_factory,
        bearer_token=bearer_token,
        rate_limit_requests=rate_limit_requests,
        watch_interval_seconds=watch_interval_seconds,
    )
    return app, repo


def _auth_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {_TEST_TOKEN}"}
    if extra:
        headers.update(extra)
    return headers


class TestBaseDependencyIsolation:
    """Ensure importing agent_forge.acp never eagerly loads FastAPI."""

    def test_importing_acp_package_does_not_load_fastapi(self) -> None:
        proc = subprocess.run(  # noqa: S603 — fixed sys.executable test probe
            [
                sys.executable,
                "-c",
                "import sys, agent_forge.acp; assert 'fastapi' not in sys.modules",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr


class TestACPTransportSecurity:
    """Auth, cookie/CSRF enforcement, query token rejection, CORS/Origin, headers, rate limit."""

    def test_rejects_missing_bad_and_query_tokens_and_sets_security_headers(
        self, tmp_path: Path
    ) -> None:
        app, _ = _make_app(tmp_path)
        client = TestClient(app)

        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.headers["x-content-type-options"] == "nosniff"
        assert health.headers["x-frame-options"] == "DENY"
        assert "default-src 'self'" in health.headers["content-security-policy"]
        assert health.headers["cache-control"] == "no-store"

        assert client.get("/workspace/tree").status_code == 401
        assert (
            client.get("/workspace/tree", headers={"Authorization": "Bearer wrong"}).status_code
            == 401
        )
        # Reject query-string tokens even alongside a valid header
        assert (
            client.get(f"/workspace/tree?token={_TEST_TOKEN}", headers=_auth_headers()).status_code
            == 401
        )
        assert client.get(f"/workspace/tree?api_key={_TEST_TOKEN}").status_code == 401

        with (
            pytest.raises(WebSocketDisconnect) as unauth_ws,
            client.websocket_connect("/ws/acp"),
        ):
            pass
        assert unauth_ws.value.code == 4401

        with (
            pytest.raises(WebSocketDisconnect) as query_ws,
            client.websocket_connect(f"/ws/acp?token={_TEST_TOKEN}", headers=_auth_headers()),
        ):
            pass
        assert query_ws.value.code == 4401

        with (
            pytest.raises(WebSocketDisconnect) as bad_origin_ws,
            client.websocket_connect(
                "/ws/acp",
                headers=_auth_headers({"Origin": "https://evil.example.com"}),
            ),
        ):
            pass
        assert bad_origin_ws.value.code == 4403

        bad_cors = client.get(
            "/workspace/tree",
            headers=_auth_headers({"Origin": "https://evil.example.com"}),
        )
        assert bad_cors.status_code == 403

    def test_cookie_session_csrf_and_logout(self, tmp_path: Path) -> None:
        app, repo = _make_app(tmp_path)
        client = TestClient(app)

        # Cannot mint session without Bearer auth
        assert client.post("/auth/session").status_code == 401

        login = client.post(
            "/auth/session",
            headers=_auth_headers({"Origin": _TEST_ORIGIN}),
        )
        assert login.status_code == 200
        assert login.headers.get("access-control-allow-origin") == _TEST_ORIGIN
        csrf_token = login.json()["csrfToken"]
        set_cookie = login.headers.get("set-cookie", "").lower()
        assert "httponly" in set_cookie
        assert "samesite=strict" in set_cookie

        # Read endpoint works using HttpOnly cookie alone
        assert client.get("/workspace/tree").status_code == 200

        # Cookie-authenticated POST requires X-CSRF-Token
        no_csrf = client.post("/workspace/file", json={"path": "notes.txt", "content": "hi"})
        assert no_csrf.status_code == 403

        with_csrf = client.post(
            "/workspace/file",
            json={"path": "notes.txt", "content": "hi\n"},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert with_csrf.status_code == 200
        assert (repo / "notes.txt").read_text(encoding="utf-8") == "hi\n"

        # Logout revokes session
        assert client.post("/auth/logout", headers={"X-CSRF-Token": csrf_token}).status_code == 200
        assert client.get("/workspace/tree").status_code == 401

    def test_rate_limiter_enforces_quota(self, tmp_path: Path) -> None:
        app, _ = _make_app(tmp_path, rate_limit_requests=3)
        client = TestClient(app)

        for _ in range(3):
            assert client.get("/workspace/tree", headers=_auth_headers()).status_code == 200
        blocked = client.get("/workspace/tree", headers=_auth_headers())
        assert blocked.status_code == 429


class TestWorkspaceCompanionAPI:
    """Contained file operations, git inspection, and workspace watcher."""

    def test_tree_read_write_git_and_containment(self, tmp_path: Path) -> None:
        app, repo = _make_app(tmp_path)
        outside = tmp_path / "secret.txt"
        outside.write_text("TOP_SECRET", encoding="utf-8")
        sibling = tmp_path / "repo-evil"
        sibling.mkdir()
        (sibling / "leak.txt").write_text("SIBLING", encoding="utf-8")
        os.symlink(outside, repo / "escaped_link.txt")

        client = TestClient(app)
        headers = _auth_headers()

        tree = client.get("/workspace/tree", headers=headers)
        assert tree.status_code == 200
        paths = [entry["path"] for entry in tree.json()["entries"]]
        assert "src/app.py" in paths
        assert "escaped_link.txt" not in paths
        assert not any(p.startswith(".git") for p in paths)

        read_ok = client.get("/workspace/file", params={"path": "src/app.py"}, headers=headers)
        assert read_ok.status_code == 200
        assert read_ok.json()["content"] == "VALUE = 1\n"

        # Mutate tracked file via API
        write_ok = client.post(
            "/workspace/file",
            json={"path": "src/app.py", "content": "VALUE = 2\n"},
            headers=headers,
        )
        assert write_ok.status_code == 200

        status_resp = client.get("/workspace/git/status", headers=headers)
        assert status_resp.status_code == 200
        assert any(item["path"] == "src/app.py" for item in status_resp.json()["entries"])

        diff_resp = client.get(
            "/workspace/git/diff",
            params={"path": "src/app.py"},
            headers=headers,
        )
        assert diff_resp.status_code == 200
        assert "+VALUE = 2" in diff_resp.json()["diff"]

        # Exhaustive escape/traversal/containment checks
        forbidden_paths = [
            "../secret.txt",
            "src/../../secret.txt",
            "..\\secret.txt",
            "../repo-evil/leak.txt",
            "/etc/passwd",
            "escaped_link.txt",
            ".git/config",
            ".git/hooks/pre-commit",
        ]
        for bad in forbidden_paths:
            assert (
                client.get("/workspace/file", params={"path": bad}, headers=headers).status_code
                == 400
            )
            assert (
                client.post(
                    "/workspace/file",
                    json={"path": bad, "content": "pwn"},
                    headers=headers,
                ).status_code
                == 400
            )
            assert (
                client.get("/workspace/git/diff", params={"path": bad}, headers=headers).status_code
                == 400
            )

        # Oversized payload returns 413
        huge = client.post(
            "/workspace/file",
            json={"path": "big.txt", "content": "x" * 2_000_000},
            headers=headers,
        )
        assert huge.status_code == 413

    def test_workspace_watch_websocket_reports_changes(self, tmp_path: Path) -> None:
        app, repo = _make_app(tmp_path, watch_interval_seconds=0.05)
        client = TestClient(app)

        with client.websocket_connect("/ws/workspace/watch", headers=_auth_headers()) as ws:
            (repo / "watched.py").write_text("CREATED = True\n", encoding="utf-8")
            event = ws.receive_json()
            assert event["type"] == "workspace.changed"
            assert "watched.py" in event["added"]


class TestACPWebSocketLifecycleAndPTY:
    """Full ACP JSON-RPC lifecycle over WebSocket + PTY terminal integration."""

    def test_acp_ws_prompt_permission_cancel_and_replay(self, tmp_path: Path) -> None:
        async def _executor(
            session: Any,
            prompt_text: str,
            _cfg: ForgeConfig,
            agent_cfg: AgentConfig,
            bus: EventBus,
            gate: Any,
            _token: Any,
        ) -> AgentRun:
            await bus.publish(
                Event(
                    type=EventType.ASSISTANT_DELTA,
                    run_id="run-ws-1",
                    data={"delta": "Working... ", "iteration": 1},
                )
            )
            await gate.evaluate("run_shell", {"command": "echo ok"})
            return AgentRun(
                id="run-ws-1",
                task=prompt_text,
                repo_path=session.cwd,
                config=agent_cfg,
                state=RunState.COMPLETED,
                messages=[Message(role=Role.ASSISTANT, content="WS turn completed.")],
            )

        app, repo = _make_app(tmp_path, run_executor=_executor)
        client = TestClient(app)
        headers = _auth_headers({"Origin": _TEST_ORIGIN})

        with client.websocket_connect("/ws/acp", headers=headers) as ws:
            ws.send_text(
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            )
            init_resp = json.loads(ws.receive_text())
            assert init_resp["result"]["protocolVersion"] == ACP_PROTOCOL_VERSION

            ws.send_text(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "session/new",
                        "params": {"cwd": str(repo)},
                    }
                )
            )
            new_resp = json.loads(ws.receive_text())
            session_id = new_resp["result"]["sessionId"]
            cmds_notif = json.loads(ws.receive_text())
            assert cmds_notif["params"]["update"]["sessionUpdate"] == "available_commands_update"

            ws.send_text(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "session/prompt",
                        "params": {
                            "sessionId": session_id,
                            "prompt": [{"type": "text", "text": "Run over WS"}],
                        },
                    }
                )
            )
            seen_updates: list[dict[str, Any]] = []
            prompt_resp: dict[str, Any] | None = None
            while prompt_resp is None:
                frame = json.loads(ws.receive_text())
                if frame.get("method") == "session/request_permission":
                    ws.send_text(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": frame["id"],
                                "result": {
                                    "outcome": {"outcome": "selected", "optionId": "allow_once"}
                                },
                            }
                        )
                    )
                elif frame.get("method") == "session/update":
                    seen_updates.append(frame["params"]["update"])
                elif frame.get("id") == 3:
                    prompt_resp = frame

            assert prompt_resp["result"]["stopReason"] == "end_turn"
            assert any(u["sessionUpdate"] == "agent_message_chunk" for u in seen_updates)

        # Reconnect in a new WebSocket connection and verify session/list + session/load replay
        with client.websocket_connect("/ws/acp", headers=headers) as ws2:
            ws2.send_text(
                json.dumps({"jsonrpc": "2.0", "id": 10, "method": "initialize", "params": {}})
            )
            json.loads(ws2.receive_text())

            ws2.send_text(
                json.dumps({"jsonrpc": "2.0", "id": 11, "method": "session/list", "params": {}})
            )
            list_resp = json.loads(ws2.receive_text())
            assert [item["sessionId"] for item in list_resp["result"]["sessions"]] == [session_id]

            ws2.send_text(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 12,
                        "method": "session/load",
                        "params": {"sessionId": session_id},
                    }
                )
            )
            replayed: list[str] = []
            while True:
                frame = json.loads(ws2.receive_text())
                if frame.get("id") == 12:
                    assert "configOptions" in frame["result"]
                    break
                replayed.append(frame["params"]["update"]["sessionUpdate"])
            assert replayed == ["user_message_chunk", "agent_message_chunk"]

    def test_terminal_websocket_runs_in_workspace_without_secret_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENT_FORGE_ACP_TOKEN", _TEST_TOKEN)
        app, repo = _make_app(tmp_path)
        client = TestClient(app)

        with client.websocket_connect("/ws/terminal", headers=_auth_headers()) as ws:
            ws.send_json({"type": "resize", "cols": 100, "rows": 30})
            ws.send_json(
                {
                    "type": "input",
                    "data": "pwd && printenv AGENT_FORGE_ACP_TOKEN || echo CLEAN_ENV\n",
                }
            )
            ws.send_json({"type": "input", "data": "exit\n"})
            collected = []
            for _ in range(40):
                try:
                    msg = ws.receive_json()
                except WebSocketDisconnect:
                    break
                if msg.get("type") == "output":
                    collected.append(msg["data"])
                elif msg.get("type") == "exit":
                    break
            transcript = "".join(collected)
            assert str(repo.resolve()) in transcript
            assert _TEST_TOKEN not in transcript
            assert "CLEAN_ENV" in transcript

    def test_cli_acp_ws_transport_invokes_uvicorn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        called: dict[str, Any] = {}
        fake_uvicorn = types.ModuleType("uvicorn")

        def _fake_run(app: Any, *, host: str, port: int) -> None:
            called["app"] = app
            called["host"] = host
            called["port"] = port

        fake_uvicorn.run = _fake_run  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
        monkeypatch.setenv("CUSTOM_ACP_TOKEN", _TEST_TOKEN)

        with patch("agent_forge.cli.load_config", return_value=ForgeConfig()):
            runner = CliRunner()
            result = runner.invoke(
                main,
                [
                    "acp",
                    "--transport",
                    "ws",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "9876",
                    "--workspace",
                    str(repo),
                    "--auth-token-env",
                    "CUSTOM_ACP_TOKEN",
                    "--allowed-origin",
                    "http://localhost:5173",
                ],
            )
        assert result.exit_code == 0, result.output
        assert called["host"] == "127.0.0.1"
        assert called["port"] == 9876
        assert called["app"].state.acp_auth.bearer_token == _TEST_TOKEN
