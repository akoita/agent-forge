"""Integration test verifying warm DockerSandbox persistence across ChatSessionRuntime turns."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import docker
import pytest
from docker.errors import DockerException

from agent_forge.agent.chat import ChatSessionRuntime
from agent_forge.agent.session import DevSession, SessionStore
from agent_forge.config import ForgeConfig
from agent_forge.llm.base import (
    LLMConfig,
    LLMProvider,
    LLMResponse,
    Message,
    Role,
    TokenUsage,
    ToolCall,
)
from agent_forge.sandbox.docker import DockerSandbox
from agent_forge.tools import create_default_registry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

pytestmark = pytest.mark.integration

_SESSION_RE = re.compile(r"\[session=([^\s\]]+)")


class _IntegrationScriptedLLM(LLMProvider):
    """Minimal scripted LLM driving real Docker tools without external network API calls."""

    def __init__(self, handler: Callable[[list[Message]], LLMResponse]) -> None:
        self._handler = handler

    async def complete(
        self,
        messages: list[Message],
        tools: Any = None,
        config: LLMConfig | None = None,
    ) -> LLMResponse:
        del tools, config
        return self._handler(messages)

    async def stream(
        self,
        messages: list[Message],
        tools: Any = None,
        config: LLMConfig | None = None,
    ) -> AsyncIterator[Any]:
        del messages, tools, config
        if False:
            yield None


def _extract_pty_session_id(messages: list[Message]) -> str:
    for msg in reversed(messages):
        if msg.role == Role.TOOL:
            match = _SESSION_RE.search(msg.content)
            if match:
                return match.group(1)
    raise AssertionError("Expected shell_start tool result containing [session=...]")


@pytest.mark.asyncio
async def test_warm_docker_sandbox_preserves_files_pty_and_restarts_on_idle(
    tmp_path: Path,
) -> None:
    """Verify real Docker container & Python PTY state stay warm across turns until idle timeout."""
    try:
        client = docker.from_env()
        client.ping()
        client.images.get("agent-forge-sandbox:latest")
    except DockerException:
        pytest.skip("Docker daemon or agent-forge-sandbox:latest image unavailable")

    repo = tmp_path / "warm_repo"
    repo.mkdir()
    (repo / "README.md").write_text("# Warm Test\n", encoding="utf-8")
    store = SessionStore(base_dir=tmp_path / "sessions")

    cfg = ForgeConfig()
    cfg.sandbox.backend = "docker"
    cfg.sandbox.image = "agent-forge-sandbox:latest"
    cfg.sandbox.timeout_seconds = 60

    session = DevSession(
        repo_path=str(repo.resolve()),
        provider=cfg.agent.default_provider,
        model=cfg.agent.default_model,
        sandbox_backend="docker",
        sandbox_image="agent-forge-sandbox:latest",
    )

    now = [100.0]
    turn_counter = 0
    captured_pty_id: list[str] = []
    turn2_tool_outputs: list[str] = []

    def _llm_factory(_cfg: ForgeConfig, _provider: str, _key: str) -> LLMProvider:
        nonlocal turn_counter
        turn_counter += 1
        cur_turn = turn_counter
        step = 0

        def _respond(messages: list[Message]) -> LLMResponse:
            nonlocal step
            step += 1
            if cur_turn == 1:
                if step == 1:
                    return LLMResponse(
                        content="",
                        tool_calls=[
                            ToolCall(
                                id="it-write-1",
                                name="write_file",
                                arguments={
                                    "path": "/workspace/warm_note.txt",
                                    "content": "warm-persist-ok",
                                },
                            )
                        ],
                        usage=TokenUsage(20, 10, 30),
                        finish_reason="tool_calls",
                    )
                if step == 2:
                    return LLMResponse(
                        content="",
                        tool_calls=[
                            ToolCall(
                                id="it-pty-start",
                                name="shell_start",
                                arguments={
                                    "command": "python3 -u -i",
                                    "pty": True,
                                    "wait_seconds": 1.0,
                                },
                            )
                        ],
                        usage=TokenUsage(20, 10, 30),
                        finish_reason="tool_calls",
                    )
                if step == 3:
                    sid = _extract_pty_session_id(messages)
                    captured_pty_id.append(sid)
                    return LLMResponse(
                        content="",
                        tool_calls=[
                            ToolCall(
                                id="it-pty-write-1",
                                name="shell_write",
                                arguments={
                                    "session_id": sid,
                                    "input": "WARM_SECRET = 1337\nprint('WARM_SET')\n",
                                    "wait_seconds": 1.0,
                                },
                            )
                        ],
                        usage=TokenUsage(20, 10, 30),
                        finish_reason="tool_calls",
                    )
                return LLMResponse(
                    content="Turn 1 initialized file and PTY state.",
                    usage=TokenUsage(15, 5, 20),
                    finish_reason="stop",
                )

            if cur_turn == 2:
                if step == 1:
                    return LLMResponse(
                        content="",
                        tool_calls=[
                            ToolCall(
                                id="it-read-2",
                                name="read_file",
                                arguments={"path": "/workspace/warm_note.txt"},
                            ),
                            ToolCall(
                                id="it-pty-write-2",
                                name="shell_write",
                                arguments={
                                    "session_id": captured_pty_id[0],
                                    "input": "print('GOT_WARM=', WARM_SECRET)\n",
                                    "wait_seconds": 1.0,
                                },
                            ),
                        ],
                        usage=TokenUsage(25, 10, 35),
                        finish_reason="tool_calls",
                    )
                turn2_tool_outputs.extend(m.content for m in messages if m.role == Role.TOOL)
                return LLMResponse(
                    content="Turn 2 confirmed warm file and live PTY.",
                    usage=TokenUsage(15, 5, 20),
                    finish_reason="stop",
                )

            return LLMResponse(
                content="Turn 3 restarted container after idle timeout.",
                usage=TokenUsage(10, 5, 15),
                finish_reason="stop",
            )

        return _IntegrationScriptedLLM(_respond)

    runtime = ChatSessionRuntime(
        cfg,
        session,
        store,
        idle_timeout_seconds=60.0,
        clock=lambda: now[0],
        sandbox_factory=lambda _c: DockerSandbox(),
        provider_factory=_llm_factory,
        tools_factory=lambda: create_default_registry(load_plugins=False),
        api_key_resolver=lambda _c, _p: "test-key",
    )

    try:
        r1 = await runtime.run_turn("Create warm_note.txt and set WARM_SECRET in Python PTY")
        assert r1.state.value == "completed"
        sb1 = runtime.sandbox
        assert isinstance(sb1, DockerSandbox)
        cid1 = sb1.container_id
        assert cid1
        assert await sb1.is_alive() is True
        assert len(await sb1.list_sessions()) == 1

        # Second turn within idle window reuses the exact same Docker container and Python PTY
        now[0] = 120.0
        r2 = await runtime.run_turn("Read warm_note.txt and inspect WARM_SECRET from PTY")
        assert r2.state.value == "completed"
        assert runtime.sandbox is sb1
        assert sb1.container_id == cid1
        assert any("warm-persist-ok" in out for out in turn2_tool_outputs)
        assert any("GOT_WARM= 1337" in out for out in turn2_tool_outputs)

        # Exceeding idle_timeout_seconds tears down the old container and starts a fresh one
        now[0] = 250.0
        r3 = await runtime.run_turn("Confirm container restarted after idle expiry")
        assert r3.state.value == "completed"
        sb2 = runtime.sandbox
        assert isinstance(sb2, DockerSandbox)
        cid2 = sb2.container_id
        assert cid2 and cid2 != cid1
        assert len(await sb2.list_sessions()) == 0
    finally:
        await runtime.close()

    assert runtime.sandbox is None
