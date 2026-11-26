"""Full ACP protocol schema conformance tests covering all methods and streaming updates."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from agent_forge.acp import (
    ACP_PROTOCOL_VERSION,
    ACPServer,
    build_plan_update,
    extract_prompt_text,
    map_stream_event_to_acp_updates,
    validate_acp_frame,
)
from agent_forge.agent.session import SessionStore
from agent_forge.config import ForgeConfig
from agent_forge.llm.base import (
    LLMConfig,
    LLMProvider,
    LLMResponse,
    Message,
    TokenUsage,
    ToolCall,
)
from agent_forge.orchestration.event_stream import StreamEvent
from agent_forge.orchestration.events import EventBus, EventType
from agent_forge.tools import EditFileTool, ReadFileTool, ToolRegistry, WriteFileTool

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from agent_forge.agent.budget import CancellationToken
    from agent_forge.agent.models import AgentConfig


class _QueueReader:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()

    async def feed(self, obj: dict[str, Any]) -> None:
        await self._queue.put((json.dumps(obj) + "\n").encode("utf-8"))

    async def close(self) -> None:
        await self._queue.put(b"")

    async def readline(self) -> bytes:
        return await self._queue.get()


class _QueueWriter:
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


class _DictSandbox:
    def __init__(self, initial_files: dict[str, str] | None = None) -> None:
        self.alive = False
        self.files: dict[str, str] = dict(initial_files or {})

    async def start(self, repo_path: str, config: Any = None) -> None:
        del repo_path, config
        self.alive = True

    async def exec(self, command: str, *, timeout_seconds: int = 30) -> Any:
        del timeout_seconds
        if command.startswith("mkdir -p "):
            return MagicMock(exit_code=0, stdout="", stderr="")
        if command.startswith("cat "):
            path = command[4:].strip()
            if path in self.files:
                return MagicMock(exit_code=0, stdout=self.files[path], stderr="")
            return MagicMock(exit_code=1, stdout="", stderr=f"Not found: {path}")
        return MagicMock(exit_code=0, stdout="", stderr="")

    async def read_file(self, path: str) -> str:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    async def write_file(self, path: str, content: str) -> None:
        self.files[path] = content

    async def stop(self) -> None:
        self.alive = False

    async def is_alive(self) -> bool:
        return self.alive


class _StreamingScriptedLLM(LLMProvider):
    def __init__(self, script_fn: Callable[[int], list[LLMResponse]]) -> None:
        self._script_fn = script_fn
        self._turn = 0

    async def complete(
        self,
        messages: list[Message],
        tools: Any = None,
        config: LLMConfig | None = None,
    ) -> LLMResponse:
        del messages, tools, config
        raise AssertionError("Expected streaming path to be used")

    async def stream(
        self,
        messages: list[Message],
        tools: Any = None,
        config: LLMConfig | None = None,
    ) -> AsyncIterator[LLMResponse]:
        del messages, tools, config
        self._turn += 1
        for chunk in self._script_fn(self._turn):
            yield chunk


async def _drain_until_id(
    writer: _QueueWriter,
    req_id: int,
    *,
    method: str | None = None,
    extra_notifications: int = 0,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    updates: list[dict[str, Any]] = []
    target_resp: dict[str, Any] | None = None
    while target_resp is None:
        msg = await asyncio.wait_for(writer.messages.get(), timeout=5.0)
        if msg.get("id") == req_id and "method" not in msg:
            validate_acp_frame(msg, method=method)
            target_resp = msg
        else:
            validate_acp_frame(msg)
            updates.append(msg)
    for _ in range(extra_notifications):
        msg = await asyncio.wait_for(writer.messages.get(), timeout=5.0)
        validate_acp_frame(msg)
        updates.append(msg)
    return updates, target_resp


class TestACPFullConformance:
    """End-to-end ACP schema validation across handshake, sessions, streaming, diffs, and fs."""

    @pytest.mark.asyncio
    async def test_initialize_auth_list_config_and_mode_switching(self, tmp_path: Path) -> None:
        repo_a = tmp_path / "repo_a"
        repo_b = tmp_path / "repo_b"
        repo_a.mkdir()
        repo_b.mkdir()
        store_dir = tmp_path / "sessions"
        store = SessionStore(base_dir=store_dir)

        cfg = ForgeConfig()
        cfg.agent.approval_mode = "prompt"  # type: ignore[assignment]
        seen_configs: list[AgentConfig] = []

        async def _executor(
            session: Any,
            prompt_text: str,
            _cfg: ForgeConfig,
            agent_cfg: AgentConfig,
            _bus: EventBus,
            _gate: Any,
            _token: CancellationToken,
        ) -> Any:
            from agent_forge.agent.models import AgentRun, RunState

            seen_configs.append(agent_cfg)
            return AgentRun(
                id="run-cfg-1",
                task=prompt_text,
                repo_path=session.cwd,
                config=agent_cfg,
                state=RunState.COMPLETED,
                messages=[Message(role="assistant", content="Configured turn complete.")],  # type: ignore[arg-type]
            )

        server = ACPServer(
            config=cfg,
            approval_mode="prompt",
            session_store=store,
            auth_methods=[{"id": "api_key_env", "name": "Environment API Key"}],
            run_executor=_executor,
        )
        reader = _QueueReader()
        writer = _QueueWriter()
        srv_task = asyncio.create_task(server.serve(reader, writer))

        # 1. Initialize handshake & capabilities
        await reader.feed(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": 99,
                    "clientCapabilities": {"fs": {"readTextFile": True}},
                    "clientInfo": {"name": "StudioTest", "version": "0.1.0"},
                },
            }
        )
        _, init_resp = await _drain_until_id(writer, 1, method="initialize")
        assert init_resp["result"]["protocolVersion"] == ACP_PROTOCOL_VERSION
        caps = init_resp["result"]["agentCapabilities"]
        assert caps["loadSession"] is True
        assert caps["promptCapabilities"]["embeddedContext"] is True
        assert caps["mcpCapabilities"] == {"http": True, "sse": True}
        assert caps["sessionCapabilities"] == {"list": {}}
        assert init_resp["result"]["authMethods"][0]["id"] == "api_key_env"

        # 2. Authenticate valid + invalid
        await reader.feed(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "authenticate",
                "params": {"methodId": "unknown"},
            }
        )
        _, bad_auth = await _drain_until_id(writer, 2, method="authenticate")
        assert bad_auth["error"]["code"] == -32602

        await reader.feed(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "authenticate",
                "params": {"methodId": "api_key_env"},
            }
        )
        await _drain_until_id(writer, 3, method="authenticate")

        # 3. session/new emits modes, configOptions, and available_commands_update
        await reader.feed(
            {"jsonrpc": "2.0", "id": 4, "method": "session/new", "params": {"cwd": str(repo_a)}}
        )
        post_new_updates, new_resp = await _drain_until_id(
            writer, 4, method="session/new", extra_notifications=1
        )
        sess_a = new_resp["result"]["sessionId"]
        assert new_resp["result"]["modes"]["currentModeId"] == "code"
        option_ids = {opt["id"]: opt["currentValue"] for opt in new_resp["result"]["configOptions"]}
        assert option_ids["mode"] == "code"
        assert post_new_updates[-1]["params"]["update"]["sessionUpdate"] == (
            "available_commands_update"
        )

        # Create a second session in repo_b
        await reader.feed(
            {"jsonrpc": "2.0", "id": 5, "method": "session/new", "params": {"cwd": str(repo_b)}}
        )
        _, new_b_resp = await _drain_until_id(
            writer, 5, method="session/new", extra_notifications=1
        )
        sess_b = new_b_resp["result"]["sessionId"]

        # 4. session/list filtering & bad cursor handling
        await reader.feed(
            {"jsonrpc": "2.0", "id": 6, "method": "session/list", "params": {"cwd": str(repo_a)}}
        )
        _, list_a = await _drain_until_id(writer, 6, method="session/list")
        assert [s["sessionId"] for s in list_a["result"]["sessions"]] == [sess_a]

        await reader.feed({"jsonrpc": "2.0", "id": 7, "method": "session/list", "params": {}})
        _, list_all = await _drain_until_id(writer, 7, method="session/list")
        assert {s["sessionId"] for s in list_all["result"]["sessions"]} == {sess_a, sess_b}

        await reader.feed(
            {"jsonrpc": "2.0", "id": 8, "method": "session/list", "params": {"cursor": "-1"}}
        )
        _, bad_cursor = await _drain_until_id(writer, 8, method="session/list")
        assert bad_cursor["error"]["code"] == -32602

        # 5. session/set_config_option & session/set_mode
        await reader.feed(
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "session/set_config_option",
                "params": {"sessionId": sess_a, "configId": "model", "value": "gpt-4o"},
            }
        )
        opt_notifs, opt_resp = await _drain_until_id(
            writer, 9, method="session/set_config_option", extra_notifications=1
        )
        assert (
            next(o for o in opt_resp["result"]["configOptions"] if o["id"] == "model")[
                "currentValue"
            ]
            == "gpt-4o"
        )
        assert opt_notifs[-1]["params"]["update"]["sessionUpdate"] == "config_option_update"

        await reader.feed(
            {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "session/set_mode",
                "params": {"sessionId": sess_a, "modeId": "code_mode"},
            }
        )
        mode_notifs, _ = await _drain_until_id(
            writer, 10, method="session/set_mode", extra_notifications=2
        )
        assert [n["params"]["update"]["sessionUpdate"] for n in mode_notifs[-2:]] == [
            "current_mode_update",
            "config_option_update",
        ]

        # Run turn and confirm live config took effect
        await reader.feed(
            {
                "jsonrpc": "2.0",
                "id": 11,
                "method": "session/prompt",
                "params": {
                    "sessionId": sess_a,
                    "prompt": [{"type": "text", "text": "Check config"}],
                },
            }
        )
        await _drain_until_id(writer, 11, method="session/prompt")
        assert seen_configs[-1].model == "gpt-4o"
        assert seen_configs[-1].execution_mode == "code_mode"

        await reader.close()
        await srv_task

        # Restart and confirm persisted config survived via session/load
        server2 = ACPServer(config=cfg, session_store=store, run_executor=_executor)
        r2 = _QueueReader()
        w2 = _QueueWriter()
        srv2_task = asyncio.create_task(server2.serve(r2, w2))
        await r2.feed({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        await _drain_until_id(w2, 1, method="initialize")
        await r2.feed(
            {"jsonrpc": "2.0", "id": 2, "method": "session/load", "params": {"sessionId": sess_a}}
        )
        load_notifs, load_resp = await _drain_until_id(
            w2, 2, method="session/load", extra_notifications=1
        )
        assert load_resp["result"]["modes"]["currentModeId"] == "code_mode"
        loaded_model = next(o for o in load_resp["result"]["configOptions"] if o["id"] == "model")[
            "currentValue"
        ]
        assert loaded_model == "gpt-4o"
        assert load_notifs[-1]["params"]["update"]["sessionUpdate"] == "available_commands_update"
        await r2.close()
        await srv2_task

    @pytest.mark.asyncio
    async def test_live_token_streaming_structured_diffs_and_client_fs_read(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        sandbox = _DictSandbox({"/workspace/app.py": "VALUE = 1\n"})

        def _script(turn: int) -> list[LLMResponse]:
            if turn == 1:
                return [
                    LLMResponse(
                        content="Reading editor buffer.",
                        tool_calls=[
                            ToolCall(id="tc-read", name="read_file", arguments={"path": "app.py"})
                        ],
                        usage=TokenUsage(10, 5, 15),
                        finish_reason="tool_calls",
                    )
                ]
            if turn == 2:
                return [
                    LLMResponse(
                        content="Updating app.py and adding helper.py.",
                        tool_calls=[
                            ToolCall(
                                id="tc-edit",
                                name="edit_file",
                                arguments={
                                    "path": "app.py",
                                    "old_text": "VALUE = 1",
                                    "new_text": "VALUE = 2",
                                },
                            ),
                            ToolCall(
                                id="tc-write",
                                name="write_file",
                                arguments={"path": "helper.py", "content": "OK = True\n"},
                            ),
                        ],
                        usage=TokenUsage(20, 10, 30),
                        finish_reason="tool_calls",
                    )
                ]
            return [
                LLMResponse(content="All ", finish_reason=""),
                LLMResponse(content="done.", usage=TokenUsage(10, 5, 15), finish_reason="stop"),
            ]

        def _tools_factory() -> ToolRegistry:
            reg = ToolRegistry()
            reg.register(ReadFileTool())
            reg.register(EditFileTool())
            reg.register(WriteFileTool())
            return reg

        cfg = ForgeConfig()
        cfg.agent.approval_mode = "auto"  # type: ignore[assignment]
        server = ACPServer(
            config=cfg,
            approval_mode="auto",
            session_store=SessionStore(base_dir=tmp_path / "sessions"),
            sandbox_factory=lambda _c: sandbox,
            provider_factory=lambda _p, _k: _StreamingScriptedLLM(_script),
            tools_factory=_tools_factory,
        )
        reader = _QueueReader()
        writer = _QueueWriter()
        srv_task = asyncio.create_task(server.serve(reader, writer))

        await reader.feed(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": 1,
                    "clientCapabilities": {"fs": {"readTextFile": True}},
                },
            }
        )
        await _drain_until_id(writer, 1, method="initialize")

        await reader.feed(
            {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": str(repo)}}
        )
        _, sess_resp = await _drain_until_id(writer, 2, method="session/new", extra_notifications=1)
        session_id = sess_resp["result"]["sessionId"]

        await reader.feed(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/prompt",
                "params": {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": "Update VALUE"}],
                },
            }
        )

        client_fs_requests: list[str] = []
        updates: list[dict[str, Any]] = []
        prompt_resp: dict[str, Any] | None = None
        while prompt_resp is None:
            msg = await asyncio.wait_for(writer.messages.get(), timeout=5.0)
            if msg.get("method") == "fs/read_text_file":
                validate_acp_frame(msg)
                client_fs_requests.append(msg["params"]["path"])
                await reader.feed(
                    {
                        "jsonrpc": "2.0",
                        "id": msg["id"],
                        "result": {"content": "VALUE = 1 # unsaved editor buffer\n"},
                    }
                )
            elif msg.get("method") == "session/update":
                validate_acp_frame(msg)
                updates.append(msg["params"]["update"])
            elif msg.get("id") == 3:
                validate_acp_frame(msg, method="session/prompt")
                prompt_resp = msg

        assert prompt_resp["result"]["stopReason"] == "end_turn"
        assert client_fs_requests == [str((repo / "app.py").resolve())]

        # Token deltas arrived incrementally without duplicating full combined text
        message_chunks = [
            u["content"]["text"] for u in updates if u["sessionUpdate"] == "agent_message_chunk"
        ]
        assert "All " in message_chunks
        assert "done." in message_chunks
        assert "All done." not in message_chunks

        # Structured diff blocks and workspace locations emitted on tool_call_update
        tool_updates = [u for u in updates if u["sessionUpdate"] == "tool_call_update"]
        diffs = [
            block
            for upd in tool_updates
            for block in upd.get("content", [])
            if block.get("type") == "diff"
        ]
        assert len(diffs) == 2
        edit_diff = next(d for d in diffs if d["path"] == "/workspace/app.py")
        assert edit_diff["oldText"] == "VALUE = 1"
        assert edit_diff["newText"] == "VALUE = 2"
        write_diff = next(d for d in diffs if d["path"] == "/workspace/helper.py")
        assert write_diff["oldText"] is None
        assert write_diff["newText"] == "OK = True\n"
        assert any(
            loc["path"] == "/workspace/app.py" and loc.get("line") == 1
            for upd in tool_updates
            for loc in upd.get("locations", [])
        )

        # Workspace traversal blocked before sending fs/read_text_file to client
        session = server._sessions[session_id]
        escaped = await server.read_client_text_file(session, "../../etc/passwd")
        assert escaped is None

        await reader.close()
        await srv_task

    def test_plan_updates_and_resource_link_prompt_blocks(self, tmp_path: Path) -> None:
        del tmp_path
        plan_payload = build_plan_update(
            "sess_plan",
            [
                {"content": "Inspect codebase", "priority": "high", "status": "completed"},
                {"content": "Apply fix", "priority": "invalid", "status": "in_progress"},
            ],
        )
        validate_acp_frame({"jsonrpc": "2.0", "method": "session/update", "params": plan_payload})
        assert plan_payload["update"]["entries"][1]["priority"] == "medium"

        ev = StreamEvent(
            seq=0,
            type=EventType.PLAN_UPDATED.value,
            run_id="r-plan",
            timestamp=datetime.now(UTC).isoformat(),
            data={"entries": [{"content": "Run tests", "priority": "high", "status": "pending"}]},
        )
        mapped = map_stream_event_to_acp_updates(ev, "sess_plan")
        assert mapped[0]["update"]["sessionUpdate"] == "plan"

        extracted = extract_prompt_text(
            [
                {"type": "text", "text": "Review"},
                {
                    "type": "resource_link",
                    "uri": "file:///repo/app.py",
                    "name": "app.py",
                    "description": "Entry module",
                },
            ]
        )
        assert "Review" in extracted
        assert "[resource_link: app.py (file:///repo/app.py)]" in extracted
        assert "Entry module" in extracted
