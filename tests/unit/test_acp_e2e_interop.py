"""End-to-end ACP Registry manifest and IDE client-server verification tests."""

from __future__ import annotations

import asyncio
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import agent_forge
from agent_forge.acp import (
    ACP_PROTOCOL_VERSION,
    ACPServer,
    validate_acp_frame,
    validate_registry_manifest,
)
from agent_forge.acp.ws_transport import create_acp_ws_app
from agent_forge.agent.models import AgentConfig, AgentRun, RunState
from agent_forge.agent.session import SessionStore
from agent_forge.config import ForgeConfig
from agent_forge.llm.base import Message, Role
from agent_forge.orchestration.events import Event, EventBus, EventType

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class _PipeReader:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()

    async def feed(self, obj: dict[str, Any]) -> None:
        await self._queue.put((json.dumps(obj) + "\n").encode("utf-8"))

    async def close(self) -> None:
        await self._queue.put(b"")

    async def readline(self) -> bytes:
        return await self._queue.get()


class _PipeWriter:
    def __init__(self) -> None:
        self.messages: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._buffer = bytearray()

    def write(self, data: bytes) -> None:
        self._buffer.extend(data)
        while b"\n" in self._buffer:
            line, _, rest = self._buffer.partition(b"\n")
            self._buffer = bytearray(rest)
            text = line.decode("utf-8").strip()
            if text:
                self.messages.put_nowait(json.loads(text))

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def close(self) -> None:
        pass


async def _send_stdio(
    proc: asyncio.subprocess.Process,
    payload: dict[str, Any],
) -> None:
    assert proc.stdin is not None
    proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
    await proc.stdin.drain()


async def _read_until_id(
    proc: asyncio.subprocess.Process,
    req_id: int,
    *,
    method: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    assert proc.stdout is not None
    notifications: list[dict[str, Any]] = []
    while True:
        raw = await asyncio.wait_for(proc.stdout.readline(), timeout=15)
        if not raw:
            msg = f"ACP subprocess closed stdout before responding to id={req_id}"
            raise AssertionError(msg)
        frame = json.loads(raw.decode("utf-8"))
        if frame.get("id") == req_id and "method" not in frame:
            validate_acp_frame(frame, method=method)
            return notifications, frame
        validate_acp_frame(frame)
        notifications.append(frame)


class TestACPRegistryManifest:
    """Validate root agent.json matches ACP Registry schema and package metadata."""

    def test_registry_manifest_valid_and_matches_package_version(self) -> None:
        manifest_path = _REPO_ROOT / "agent.json"
        pyproject_path = _REPO_ROOT / "pyproject.toml"
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))

        manifest = validate_registry_manifest(data)
        assert manifest.id == "agent-forge"
        assert manifest.version == agent_forge.__version__
        assert manifest.version == pyproject["project"]["version"]
        assert manifest.license == "MIT"
        assert manifest.distribution.uvx is not None
        assert manifest.distribution.uvx.package == f"agent-forge=={manifest.version}"
        assert manifest.distribution.uvx.args[0] == "acp"


class TestACPIDEInteropEndToEnd:
    """Real subprocess + WebSocket ACP client-server E2E verification."""

    @pytest.mark.asyncio
    async def test_stdio_subprocess_e2e_manifest_args_persistence_and_reload(
        self, tmp_path: Path
    ) -> None:
        manifest = validate_registry_manifest(
            json.loads((_REPO_ROOT / "agent.json").read_text(encoding="utf-8"))
        )
        assert manifest.distribution.uvx is not None
        repo = tmp_path / "workspace"
        repo.mkdir()
        sessions_dir = tmp_path / "sessions"
        launch_args = [*manifest.distribution.uvx.args, "--sessions-dir", str(sessions_dir)]

        proc1 = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "agent_forge.cli",
            *launch_args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await _send_stdio(
                proc1,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": ACP_PROTOCOL_VERSION},
                },
            )
            _, init_resp = await _read_until_id(proc1, 1, method="initialize")
            assert init_resp["result"]["agentCapabilities"]["loadSession"] is True

            await _send_stdio(
                proc1,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/new",
                    "params": {"cwd": str(repo)},
                },
            )
            _, new_resp = await _read_until_id(proc1, 2, method="session/new")
            session_id = new_resp["result"]["sessionId"]
            assert proc1.stdout is not None
            cmds_frame = json.loads(
                (await asyncio.wait_for(proc1.stdout.readline(), timeout=15)).decode("utf-8")
            )
            validate_acp_frame(cmds_frame)
            assert cmds_frame["params"]["update"]["sessionUpdate"] == "available_commands_update"

            await _send_stdio(
                proc1,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "session/set_mode",
                    "params": {"sessionId": session_id, "modeId": "code_mode"},
                },
            )
            await _read_until_id(proc1, 3, method="session/set_mode")

            await _send_stdio(
                proc1,
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "session/set_config_option",
                    "params": {"sessionId": session_id, "configId": "model", "value": "gpt-4o"},
                },
            )
            await _read_until_id(proc1, 4, method="session/set_config_option")

            assert proc1.stdin is not None
            proc1.stdin.close()
            _, stderr1 = await asyncio.wait_for(proc1.communicate(), timeout=15)
            assert proc1.returncode == 0, stderr1.decode("utf-8", errors="replace")
        finally:
            if proc1.returncode is None:
                proc1.kill()
                await proc1.wait()

        # Second OS process reconnects and loads persisted session state
        proc2 = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "agent_forge.cli",
            *launch_args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await _send_stdio(
                proc2,
                {"jsonrpc": "2.0", "id": 10, "method": "initialize", "params": {}},
            )
            await _read_until_id(proc2, 10, method="initialize")

            await _send_stdio(
                proc2,
                {"jsonrpc": "2.0", "id": 11, "method": "session/list", "params": {}},
            )
            _, list_resp = await _read_until_id(proc2, 11, method="session/list")
            assert [item["sessionId"] for item in list_resp["result"]["sessions"]] == [session_id]

            await _send_stdio(
                proc2,
                {
                    "jsonrpc": "2.0",
                    "id": 12,
                    "method": "session/load",
                    "params": {"sessionId": session_id},
                },
            )
            _, load_resp = await _read_until_id(proc2, 12, method="session/load")
            assert load_resp["result"]["modes"]["currentModeId"] == "code_mode"
            loaded_model = next(
                opt for opt in load_resp["result"]["configOptions"] if opt["id"] == "model"
            )["currentValue"]
            assert loaded_model == "gpt-4o"

            assert proc2.stdin is not None
            proc2.stdin.close()
            _, stderr2 = await asyncio.wait_for(proc2.communicate(), timeout=15)
            assert proc2.returncode == 0, stderr2.decode("utf-8", errors="replace")
        finally:
            if proc2.returncode is None:
                proc2.kill()
                await proc2.wait()

    @pytest.mark.asyncio
    async def test_e2e_prompt_permissions_cancellation_and_replay_stdio_and_ws(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        store = SessionStore(base_dir=tmp_path / "sessions")
        cfg = ForgeConfig()
        cfg.agent.approval_mode = "prompt"  # type: ignore[assignment]
        turn_num = 0

        async def _executor(
            session: Any,
            prompt_text: str,
            _cfg: ForgeConfig,
            agent_cfg: AgentConfig,
            bus: EventBus,
            gate: Any,
            token: Any,
        ) -> AgentRun:
            nonlocal turn_num
            turn_num += 1
            if turn_num == 1:
                await bus.publish(
                    Event(
                        type=EventType.ASSISTANT_DELTA,
                        run_id="run-e2e-1",
                        data={"delta": "Applying fix...", "iteration": 1},
                    )
                )
                _, dec = await gate.evaluate("run_shell", {"command": "rm -f .tmp-artifact"})
                assert dec is not None and dec.approved
                return AgentRun(
                    id="run-e2e-1",
                    task=prompt_text,
                    repo_path=session.cwd,
                    config=agent_cfg,
                    state=RunState.COMPLETED,
                    messages=[Message(role=Role.ASSISTANT, content="E2E fix verified.")],
                )
            await gate.evaluate("run_shell", {"command": "rm -rf build"})
            return AgentRun(
                id="run-e2e-2",
                task=prompt_text,
                repo_path=session.cwd,
                config=agent_cfg,
                state=RunState.CANCELLED if token.is_cancelled else RunState.COMPLETED,
            )

        # 1. Verify full prompt -> permission -> cancellation over stdio-framed ACPServer
        reader = _PipeReader()
        writer = _PipeWriter()
        server = ACPServer(
            config=cfg,
            approval_mode="prompt",
            session_store=store,
            run_executor=_executor,
        )
        srv_task = asyncio.create_task(server.serve(reader, writer))

        await reader.feed({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        validate_acp_frame(await writer.messages.get(), method="initialize")
        await reader.feed(
            {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": str(repo)}}
        )
        new_resp = await writer.messages.get()
        validate_acp_frame(new_resp, method="session/new")
        sid = new_resp["result"]["sessionId"]
        validate_acp_frame(await writer.messages.get())  # available_commands_update

        await reader.feed(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "Fix bug"}]},
            }
        )
        while True:
            msg = await asyncio.wait_for(writer.messages.get(), timeout=5.0)
            validate_acp_frame(msg, method="session/prompt" if msg.get("id") == 3 else None)
            if msg.get("method") == "session/request_permission":
                await reader.feed(
                    {
                        "jsonrpc": "2.0",
                        "id": msg["id"],
                        "result": {"outcome": {"outcome": "selected", "optionId": "allow_once"}},
                    }
                )
            elif msg.get("id") == 3:
                assert msg["result"]["stopReason"] == "end_turn"
                break

        await reader.feed(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "Clean build"}]},
            }
        )
        perm2 = await asyncio.wait_for(writer.messages.get(), timeout=5.0)
        validate_acp_frame(perm2)
        await reader.feed(
            {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": sid}}
        )
        while True:
            msg = await asyncio.wait_for(writer.messages.get(), timeout=5.0)
            validate_acp_frame(msg, method="session/prompt" if msg.get("id") == 4 else None)
            if msg.get("id") == 4:
                assert msg["result"]["stopReason"] == "cancelled"
                break

        await reader.close()
        await srv_task

        # 2. Verify another client reloads that same persisted session over authenticated WebSocket
        _assert_ws_session_replay(repo, cfg, store, sid)


def _assert_ws_session_replay(
    repo: Path,
    cfg: ForgeConfig,
    store: SessionStore,
    sid: str,
) -> None:
    bridge_auth = f"interop-secret-{sid}"
    app = create_acp_ws_app(
        workspace_root=repo,
        server_factory=lambda: ACPServer(config=cfg, approval_mode="prompt", session_store=store),
        bearer_token=bridge_auth,
    )
    client = TestClient(app)
    with client.websocket_connect(
        "/ws/acp",
        headers={"Authorization": f"Bearer {bridge_auth}", "Origin": "http://127.0.0.1:5173"},
    ) as ws:
        ws.send_text(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}))
        validate_acp_frame(json.loads(ws.receive_text()), method="initialize")
        ws.send_text(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/load",
                    "params": {"sessionId": sid},
                }
            )
        )
        replayed_prompts: list[str] = []
        while True:
            frame = json.loads(ws.receive_text())
            if frame.get("id") == 2:
                validate_acp_frame(frame, method="session/load")
                break
            validate_acp_frame(frame)
            upd = frame["params"]["update"]
            if upd["sessionUpdate"] == "user_message_chunk":
                replayed_prompts.append(upd["content"]["text"])
        assert replayed_prompts == ["Fix bug", "Clean build"]
