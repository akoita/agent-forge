"""Unit tests for stateful multi-turn ACP sessions and disk-backed session/load replay."""

from __future__ import annotations

import asyncio
import json
import sys
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from agent_forge.acp import (
    ACP_PROTOCOL_VERSION,
    ACPServer,
    build_replay_updates,
)
from agent_forge.agent.session import DevSession, SessionStore, SessionTurn
from agent_forge.config import ForgeConfig
from agent_forge.llm.base import LLMResponse, Message, Role, TokenUsage, ToolCall
from agent_forge.tools.base import Tool, ToolRegistry, ToolResult


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


class _InspectTool(Tool):
    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return "Read file"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"path": {"type": "string"}}}

    @property
    def side_effecting(self) -> bool:
        return False

    async def execute(self, arguments: dict[str, Any], sandbox: Any) -> ToolResult:
        del sandbox
        return ToolResult(output=f"read:{arguments.get('path')}")


async def _collect_until_id(
    writer: _QueueWriter, req_id: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    updates: list[dict[str, Any]] = []
    while True:
        msg = await asyncio.wait_for(writer.messages.get(), timeout=5.0)
        if msg.get("id") == req_id:
            return updates, msg
        updates.append(msg)


def _has_agent_text(updates: list[dict[str, Any]], expected: str) -> bool:
    for msg in updates:
        upd = msg.get("params", {}).get("update", {})
        if upd.get("sessionUpdate") == "agent_message_chunk":
            content = upd.get("content")
            if isinstance(content, dict) and content.get("text") == expected:
                return True
    return False


class TestACPReplayAndStatefulSessions:
    """build_replay_updates, multi-turn prompt continuity, and disk-backed session/load."""

    def test_build_replay_updates_orders_user_tools_and_assistant(self, tmp_path: Path) -> None:
        dev_session = DevSession(
            repo_path=str(tmp_path),
            id="sess_replay_1",
            messages=[
                Message(role=Role.SYSTEM, content="sys"),
                Message(role=Role.USER, content="Inspect foo.py"),
                Message(
                    role=Role.ASSISTANT,
                    content="Reading foo.py",
                    tool_calls=[
                        ToolCall(id="call-1", name="read_file", arguments={"path": "foo.py"})
                    ],
                ),
                Message(role=Role.TOOL, content="VALUE = 1", tool_call_id="call-1"),
                Message(role=Role.ASSISTANT, content="Done reading foo.py"),
            ],
            turns=[
                SessionTurn(
                    turn_index=0,
                    prompt="Inspect foo.py",
                    response="Done reading foo.py",
                    run_id="r1",
                    message_start_index=0,
                    message_end_index=5,
                )
            ],
        )

        replayed = build_replay_updates(dev_session)
        kinds = [item["update"]["sessionUpdate"] for item in replayed]
        assert kinds == [
            "user_message_chunk",
            "tool_call",
            "tool_call_update",
            "agent_message_chunk",
        ]
        assert replayed[0]["update"]["content"]["text"] == "Inspect foo.py"
        assert replayed[1]["update"]["toolCallId"] == "call-1"
        assert replayed[2]["update"]["status"] == "completed"
        assert replayed[3]["update"]["content"]["text"] == "Done reading foo.py"

    @pytest.mark.asyncio
    async def test_two_turns_and_server_restart_reload_with_replay(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "AGENTS.md").write_text("# ACP Repo Rules\nBe careful.\n", encoding="utf-8")
        (repo / "foo.py").write_text("X = 1\n", encoding="utf-8")
        store_dir = tmp_path / "sessions"

        seen_prompts: list[list[tuple[Role, str]]] = []
        llm_turn1 = AsyncMock()
        llm_turn1.complete.side_effect = [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="tc1", name="read_file", arguments={"path": "foo.py"})],
                usage=TokenUsage(20, 10, 30),
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content="Turn 1 observed X = 1.",
                tool_calls=[],
                usage=TokenUsage(30, 10, 40),
                finish_reason="stop",
            ),
        ]
        llm_turn2 = AsyncMock()

        async def _turn2_complete(messages: list[Message], **_kwargs: Any) -> LLMResponse:
            seen_prompts.append([(m.role, m.content) for m in messages])
            return LLMResponse(
                content="Turn 2 knows Turn 1 saw X = 1.",
                tool_calls=[],
                usage=TokenUsage(40, 10, 50),
                finish_reason="stop",
            )

        llm_turn2.complete.side_effect = _turn2_complete
        llm_turn3 = AsyncMock()

        async def _turn3_complete(messages: list[Message], **_kwargs: Any) -> LLMResponse:
            seen_prompts.append([(m.role, m.content) for m in messages])
            return LLMResponse(
                content="Turn 3 resumed after restart.",
                tool_calls=[],
                usage=TokenUsage(45, 10, 55),
                finish_reason="stop",
            )

        llm_turn3.complete.side_effect = _turn3_complete
        providers = iter([llm_turn1, llm_turn2, llm_turn3])

        def _make_registry() -> ToolRegistry:
            reg = ToolRegistry()
            reg.register(_InspectTool())
            return reg

        def _make_sandbox(_cfg: Any) -> AsyncMock:
            sb = AsyncMock()
            sb.exec.return_value = MagicMock(exit_code=0, stdout="", stderr="")
            return sb

        cfg = ForgeConfig()
        cfg.agent.approval_mode = "auto"

        server1 = ACPServer(
            config=cfg,
            approval_mode="auto",
            session_store=SessionStore(base_dir=store_dir),
            sandbox_factory=_make_sandbox,
            provider_factory=lambda _p, _k: next(providers),
            tools_factory=_make_registry,
        )
        r1 = _QueueReader()
        w1 = _QueueWriter()
        srv1_task = asyncio.create_task(server1.serve(r1, w1))

        await r1.feed({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        await _collect_until_id(w1, 1)

        await r1.feed(
            {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": str(repo)}}
        )
        _, new_resp = await _collect_until_id(w1, 2)
        session_id = new_resp["result"]["sessionId"]

        await r1.feed(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/prompt",
                "params": {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": "Inspect foo.py"}],
                },
            }
        )
        t1_updates, t1_resp = await _collect_until_id(w1, 3)
        assert t1_resp["result"]["stopReason"] == "end_turn"
        assert _has_agent_text(t1_updates, "Turn 1 observed X = 1.")

        # Second turn on the same server shares full conversation and skips redundant discovery
        await r1.feed(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "session/prompt",
                "params": {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": "Summarize"}],
                },
            }
        )
        t2_updates, t2_resp = await _collect_until_id(w1, 4)
        assert t2_resp["result"]["stopReason"] == "end_turn"
        assert seen_prompts[0]
        turn2_contents = [content for _role, content in seen_prompts[0]]
        assert "Inspect foo.py" in turn2_contents
        assert "Turn 1 observed X = 1." in turn2_contents
        assert turn2_contents[-1] == "Summarize"
        assert _has_agent_text(t2_updates, "Turn 2 knows Turn 1 saw X = 1.")

        await r1.close()
        await srv1_task

        # Fresh ACPServer simulates process restart and loads session from disk
        server2 = ACPServer(
            config=cfg,
            approval_mode="auto",
            session_store=SessionStore(base_dir=store_dir),
            sandbox_factory=_make_sandbox,
            provider_factory=lambda _p, _k: next(providers),
            tools_factory=_make_registry,
        )
        r2 = _QueueReader()
        w2 = _QueueWriter()
        srv2_task = asyncio.create_task(server2.serve(r2, w2))

        # Omit cwd to verify fallback to persisted repo_path
        await r2.feed(
            {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "session/load",
                "params": {"sessionId": session_id},
            }
        )
        replay_msgs, load_resp = await _collect_until_id(w2, 10)
        assert "modes" in load_resp["result"]
        assert "configOptions" in load_resp["result"]
        replay_kinds = [m["params"]["update"]["sessionUpdate"] for m in replay_msgs]
        assert replay_kinds == [
            "user_message_chunk",
            "tool_call",
            "tool_call_update",
            "agent_message_chunk",
            "user_message_chunk",
            "agent_message_chunk",
        ]

        with (
            patch("agent_forge.agent.context.discover_repo_instructions") as mock_discover,
            patch("agent_forge.agent.context.build_repo_map") as mock_map,
        ):
            await r2.feed(
                {
                    "jsonrpc": "2.0",
                    "id": 11,
                    "method": "session/prompt",
                    "params": {
                        "sessionId": session_id,
                        "prompt": [{"type": "text", "text": "Continue after reload"}],
                    },
                }
            )
            t3_updates, t3_resp = await _collect_until_id(w2, 11)

        mock_discover.assert_not_called()
        mock_map.assert_not_called()
        assert t3_resp["result"]["stopReason"] == "end_turn"
        assert "Turn 2 knows Turn 1 saw X = 1." in [c for _r, c in seen_prompts[1]]
        assert _has_agent_text(t3_updates, "Turn 3 resumed after restart.")

        # Invalid session ID rejected on session/load
        await r2.feed(
            {
                "jsonrpc": "2.0",
                "id": 12,
                "method": "session/load",
                "params": {"sessionId": "../bad"},
            }
        )
        _, bad_resp = await _collect_until_id(w2, 12)
        assert bad_resp["error"]["code"] == -32602

        await r2.close()
        await srv2_task

    @pytest.mark.asyncio
    async def test_cli_acp_sessions_dir_persists_to_disk(self, tmp_path: Path) -> None:
        sessions_dir = tmp_path / "acp_sessions"
        requests = (
            "\n".join(
                [
                    json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "session/new",
                            "params": {"cwd": str(tmp_path)},
                        }
                    ),
                ]
            )
            + "\n"
        ).encode("utf-8")
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "agent_forge.cli",
            "acp",
            "--sessions-dir",
            str(sessions_dir),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_raw, stderr_raw = await asyncio.wait_for(proc.communicate(requests), timeout=15)
        assert proc.returncode == 0, stderr_raw.decode("utf-8", errors="replace")
        parsed = [
            json.loads(line) for line in stdout_raw.decode("utf-8").splitlines() if line.strip()
        ]
        assert parsed[0]["result"]["protocolVersion"] == ACP_PROTOCOL_VERSION
        sid = parsed[1]["result"]["sessionId"]
        assert SessionStore(base_dir=sessions_dir).exists(sid)
