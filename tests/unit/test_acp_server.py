"""Unit tests for the Agent Client Protocol (ACP) server and protocol mappings."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from agent_forge.acp import (
    ACP_PROTOCOL_VERSION,
    ACPServer,
    ACPSession,
    classify_tool_kind,
    extract_prompt_text,
    map_run_state_to_stop_reason,
    map_stream_event_to_acp_updates,
    translate_acp_mcp_servers,
)
from agent_forge.agent.models import AgentConfig, AgentRun, RunState
from agent_forge.llm.base import Message, Role
from agent_forge.orchestration.event_stream import StreamEvent
from agent_forge.orchestration.events import Event, EventBus, EventType

if TYPE_CHECKING:
    from pathlib import Path

    from agent_forge.agent.approvals import ApprovalGate
    from agent_forge.agent.budget import CancellationToken
    from agent_forge.config import ForgeConfig


class _QueueReader:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()

    async def feed(self, obj: dict[str, Any] | bytes) -> None:
        raw = obj if isinstance(obj, bytes) else (json.dumps(obj) + "\n").encode("utf-8")
        await self._queue.put(raw)

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


class TestACPProtocolMapping:
    """Protocol utility functions, MCP server translation, and StreamEvent mappers."""

    def test_helpers_and_stream_event_mappings(self) -> None:
        assert classify_tool_kind("read_file") == "read"
        assert classify_tool_kind("apply_patch") == "edit"
        assert classify_tool_kind("run_shell") == "execute"
        assert classify_tool_kind("mcp__srv__delete_item") == "edit"
        assert classify_tool_kind("custom_widget") == "other"

        text = extract_prompt_text(
            [
                {"type": "text", "text": "Fix bug"},
                {"type": "resource", "resource": {"uri": "file:///a.py", "text": "x = 1"}},
            ]
        )
        assert "Fix bug" in text
        assert "[file:///a.py]" in text

        servers = translate_acp_mcp_servers(
            [
                {
                    "name": "fs",
                    "command": "npx",
                    "args": ["server"],
                    "env": [{"name": "ROOT", "value": "/repo"}],
                },
                {
                    "type": "http",
                    "name": "docs",
                    "url": "https://mcp.example.com/mcp",
                    "headers": [{"name": "X-Org", "value": "af"}],
                },
            ]
        )
        assert servers["fs"].transport == "stdio"
        assert servers["fs"].env == {"ROOT": "/repo"}
        assert servers["docs"].transport == "http"
        assert servers["docs"].headers == {"X-Org": "af"}

        assert map_run_state_to_stop_reason(RunState.COMPLETED) == "end_turn"
        assert map_run_state_to_stop_reason(RunState.CANCELLED) == "cancelled"
        assert map_run_state_to_stop_reason(RunState.TIMEOUT) == "max_turn_requests"

        ev_called = StreamEvent(
            seq=0,
            type="tool.called",
            run_id="r1",
            timestamp="2026-01-01T00:00:00Z",
            data={"tool_name": "edit_file", "arguments": {"path": "app.py"}},
        )
        updates = map_stream_event_to_acp_updates(ev_called, "s1", tool_call_id="c-1")
        assert updates[0]["update"]["sessionUpdate"] == "tool_call"
        assert updates[0]["update"]["kind"] == "edit"
        assert updates[0]["update"]["toolCallId"] == "c-1"

        ev_done = StreamEvent(
            seq=1,
            type="tool.completed",
            run_id="r1",
            timestamp="2026-01-01T00:00:01Z",
            data={"tool_name": "edit_file", "exit_code": 0, "output": "ok"},
        )
        upd_done = map_stream_event_to_acp_updates(ev_done, "s1", tool_call_id="c-1")
        assert upd_done[0]["update"]["status"] == "completed"
        assert upd_done[0]["update"]["toolCallId"] == "c-1"

        ev_req = StreamEvent(
            seq=2,
            type="approval.requested",
            run_id="r1",
            timestamp="2026-01-01T00:00:02Z",
            data={"tool_name": "run_shell", "category": "file_deletion", "reason": "rm"},
        )
        assert map_stream_event_to_acp_updates(ev_req, "s1")[0]["update"]["sessionUpdate"] == (
            "agent_thought_chunk"
        )


class TestACPServerBidirectionalSession:
    """Interactive ACP initialize, prompt streaming, permissions, and cancellation."""

    @pytest.mark.asyncio
    async def test_session_streams_updates_and_handles_permissions(self, tmp_path: Path) -> None:
        reader = _QueueReader()
        writer = _QueueWriter()
        outcomes: list[bool] = []

        async def _fake_executor(
            session: ACPSession,
            prompt_text: str,
            cfg: ForgeConfig,
            agent_cfg: AgentConfig,
            bus: EventBus,
            gate: ApprovalGate,
            token: CancellationToken,
        ) -> AgentRun:
            del cfg, agent_cfg, token
            await bus.publish(
                Event(
                    type=EventType.ITERATION_STARTED,
                    run_id="run-acp-1",
                    timestamp=datetime.now(UTC),
                    data={"iteration": 1, "max_iterations": 5},
                )
            )
            await bus.publish(
                Event(
                    type=EventType.TOOL_CALLED,
                    run_id="run-acp-1",
                    timestamp=datetime.now(UTC),
                    data={"tool_name": "run_shell", "arguments": {"command": "rm -f tmp.txt"}},
                )
            )
            _, d1 = await gate.evaluate("run_shell", {"command": "rm -f tmp.txt"})
            _, d2 = await gate.evaluate("run_shell", {"command": "rm -f tmp.txt"})
            _, d3 = await gate.evaluate("run_shell", {"command": "rm -f tmp.txt"})
            outcomes.extend(
                [
                    bool(d1 and d1.approved),
                    bool(d2 and d2.approved),
                    bool(d3 and d3.approved),
                ]
            )
            await bus.publish(
                Event(
                    type=EventType.TOOL_COMPLETED,
                    run_id="run-acp-1",
                    timestamp=datetime.now(UTC),
                    data={"tool_name": "run_shell", "exit_code": 0, "output": "done"},
                )
            )
            return AgentRun(
                id="run-acp-1",
                task=prompt_text,
                repo_path=session.cwd,
                config=AgentConfig(),
                state=RunState.COMPLETED,
                messages=[Message(role=Role.ASSISTANT, content="Applied fix safely.")],
            )

        server = ACPServer(approval_mode="prompt", run_executor=_fake_executor)
        srv_task = asyncio.create_task(server.serve(reader, writer))

        await reader.feed({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        init_resp = await writer.messages.get()
        assert init_resp["result"]["protocolVersion"] == ACP_PROTOCOL_VERSION

        await reader.feed({"jsonrpc": "2.0", "id": 99, "method": "no/suchMethod"})
        unknown_resp = await writer.messages.get()
        assert unknown_resp["error"]["code"] == -32601

        await reader.feed(
            {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": str(tmp_path)}}
        )
        sess_resp = await writer.messages.get()
        session_id = sess_resp["result"]["sessionId"]

        await reader.feed(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/prompt",
                "params": {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": "Clean tmp.txt"}],
                },
            }
        )

        seen_updates: list[dict[str, Any]] = []
        perm_requests = 0
        prompt_resp: dict[str, Any] | None = None
        while prompt_resp is None:
            msg = await asyncio.wait_for(writer.messages.get(), timeout=3.0)
            if msg.get("method") == "session/update":
                seen_updates.append(msg["params"]["update"])
            elif msg.get("method") == "session/request_permission":
                perm_requests += 1
                option_id = "reject_once" if perm_requests == 1 else "allow_always"
                await reader.feed(
                    {
                        "jsonrpc": "2.0",
                        "id": msg["id"],
                        "result": {"outcome": {"outcome": "selected", "optionId": option_id}},
                    }
                )
            elif msg.get("id") == 3:
                prompt_resp = msg

        await reader.close()
        await srv_task

        assert perm_requests == 2
        assert outcomes == [False, True, True]
        assert prompt_resp["result"]["stopReason"] == "end_turn"
        assert prompt_resp["result"]["_meta"]["runId"] == "run-acp-1"
        update_kinds = [u["sessionUpdate"] for u in seen_updates]
        assert "agent_thought_chunk" in update_kinds
        assert "tool_call" in update_kinds
        assert "tool_call_update" in update_kinds
        assert "agent_message_chunk" in update_kinds

    @pytest.mark.asyncio
    async def test_session_cancel_interrupts_pending_permission(self, tmp_path: Path) -> None:
        reader = _QueueReader()
        writer = _QueueWriter()
        cancelled_seen: list[bool] = []

        async def _slow_executor(
            session: ACPSession,
            prompt_text: str,
            cfg: ForgeConfig,
            agent_cfg: AgentConfig,
            bus: EventBus,
            gate: ApprovalGate,
            token: CancellationToken,
        ) -> AgentRun:
            del cfg, agent_cfg, bus
            _, decision = await gate.evaluate("run_shell", {"command": "rm -f danger.txt"})
            cancelled_seen.append(token.is_cancelled)
            assert decision is not None and not decision.approved
            return AgentRun(
                id="run-cancel-1",
                task=prompt_text,
                repo_path=session.cwd,
                config=AgentConfig(),
                state=RunState.CANCELLED if token.is_cancelled else RunState.COMPLETED,
            )

        server = ACPServer(approval_mode="prompt", run_executor=_slow_executor)
        srv_task = asyncio.create_task(server.serve(reader, writer))

        await reader.feed(
            {"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {"cwd": str(tmp_path)}}
        )
        session_id = (await writer.messages.get())["result"]["sessionId"]

        await reader.feed(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/prompt",
                "params": {"sessionId": session_id, "prompt": [{"type": "text", "text": "Go"}]},
            }
        )
        perm_msg = await asyncio.wait_for(writer.messages.get(), timeout=3.0)
        assert perm_msg["method"] == "session/request_permission"

        await reader.feed(
            {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": session_id}}
        )
        final_resp = await asyncio.wait_for(writer.messages.get(), timeout=3.0)
        await reader.close()
        await srv_task

        assert cancelled_seen == [True]
        assert final_resp["id"] == 2
        assert final_resp["result"]["stopReason"] == "cancelled"

    @pytest.mark.asyncio
    async def test_cli_acp_subprocess_emits_pure_jsonrpc_stdout(self, tmp_path: Path) -> None:
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
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_raw, stderr_raw = await asyncio.wait_for(proc.communicate(requests), timeout=15)
        assert proc.returncode == 0, stderr_raw.decode("utf-8", errors="replace")
        lines = [line for line in stdout_raw.decode("utf-8").splitlines() if line.strip()]
        assert len(lines) == 3
        parsed = [json.loads(line) for line in lines]
        assert parsed[0]["result"]["protocolVersion"] == ACP_PROTOCOL_VERSION
        assert parsed[1]["result"]["sessionId"].startswith("sess_")
        assert parsed[1]["result"]["modes"]["currentModeId"] == "code"
        assert parsed[2]["method"] == "session/update"
        assert parsed[2]["params"]["update"]["sessionUpdate"] == "available_commands_update"
