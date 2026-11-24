"""Unit tests for interactive CLI chat runtime, warm sandbox, slash commands, and session CLI."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from click.testing import CliRunner

from agent_forge.agent.chat import ChatSessionRuntime, run_chat_repl
from agent_forge.agent.session import DevSession, SessionStore
from agent_forge.cli import main
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
from agent_forge.sandbox.base import ExecResult, Sandbox, SandboxConfig
from agent_forge.tools import create_default_registry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path


class _WarmDummySandbox(Sandbox):
    """In-memory sandbox tracking warm lifecycle, files, and persistent PTY state."""

    def __init__(self) -> None:
        self.alive = False
        self.start_calls = 0
        self.stop_calls = 0
        self.close_all_calls = 0
        self.files: dict[str, str] = {}
        self.pty_sessions: list[dict[str, str]] = []

    async def start(self, repo_path: str, config: SandboxConfig | None = None) -> None:
        del repo_path, config
        self.start_calls += 1
        self.alive = True

    async def exec(self, command: str, *, timeout_seconds: int = 30) -> ExecResult:
        del command, timeout_seconds
        return ExecResult(exit_code=0, stdout="", stderr="")

    async def read_file(self, path: str) -> str:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    async def write_file(self, path: str, content: str) -> None:
        self.files[path] = content

    async def stop(self) -> None:
        self.stop_calls += 1
        self.alive = False
        self.pty_sessions.clear()

    async def is_alive(self) -> bool:
        return self.alive

    async def list_sessions(self) -> list[Any]:
        return list(self.pty_sessions)

    async def close_all_sessions(self) -> None:
        self.close_all_calls += 1
        self.pty_sessions.clear()


class _ScriptedLLM(LLMProvider):
    """Minimal async LLM returning scripted responses without network calls."""

    def __init__(self, handler: Callable[[list[Message]], LLMResponse]) -> None:
        self._handler = handler
        self.calls: list[list[Message]] = []
        self.closed = False

    async def complete(
        self,
        messages: list[Message],
        tools: Any = None,
        config: LLMConfig | None = None,
    ) -> LLMResponse:
        del tools, config
        self.calls.append(list(messages))
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

    async def close(self) -> None:
        self.closed = True


def _build_runtime(
    tmp_path: Path,
    *,
    llm_factory: Callable[[ForgeConfig, str, str], LLMProvider],
    sandbox_factory: Callable[[SandboxConfig], _WarmDummySandbox],
    idle_timeout_seconds: float | None = 1800.0,
    clock: Callable[[], float] | None = None,
) -> tuple[ChatSessionRuntime, SessionStore, Path]:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    store = SessionStore(base_dir=tmp_path / "sessions")
    cfg = ForgeConfig()
    session = DevSession(
        repo_path=str(repo.resolve()),
        provider=cfg.agent.default_provider,
        model=cfg.agent.default_model,
        approval_mode="auto",
        sandbox_backend="docker",
        sandbox_image=cfg.sandbox.image,
    )
    runtime = ChatSessionRuntime(
        cfg,
        session,
        store,
        approval_mode="auto",
        idle_timeout_seconds=idle_timeout_seconds,
        clock=clock,
        sandbox_factory=sandbox_factory,
        provider_factory=llm_factory,
        tools_factory=lambda: create_default_registry(load_plugins=False),
        api_key_resolver=lambda _cfg, _prov: "test-key",
    )
    return runtime, store, repo


@pytest.mark.asyncio
async def test_chat_runtime_keeps_warm_sandbox_and_pty_across_turns(tmp_path: Path) -> None:
    sandboxes: list[_WarmDummySandbox] = []

    def _sb_factory(_cfg: SandboxConfig) -> _WarmDummySandbox:
        sb = _WarmDummySandbox()
        sandboxes.append(sb)
        return sb

    turn_num = 0

    def _llm_factory(_cfg: ForgeConfig, _provider: str, _key: str) -> LLMProvider:
        nonlocal turn_num
        turn_num += 1
        current_turn = turn_num
        step = 0

        def _respond(_messages: list[Message]) -> LLMResponse:
            nonlocal step
            step += 1
            if current_turn == 1 and step == 1:
                assert sandboxes
                sandboxes[-1].pty_sessions.append({"session_id": "pty-1", "command": "pytest -w"})
                return LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="call-w1",
                            name="write_file",
                            arguments={"path": "/workspace/note.txt", "content": "warm-state"},
                        )
                    ],
                    usage=TokenUsage(20, 10, 30),
                    finish_reason="tool_calls",
                )
            if current_turn == 2 and step == 1:
                return LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="call-r2",
                            name="read_file",
                            arguments={"path": "/workspace/note.txt"},
                        )
                    ],
                    usage=TokenUsage(25, 10, 35),
                    finish_reason="tool_calls",
                )
            if current_turn == 2:
                return LLMResponse(
                    content="Saw warm-state from turn 1.",
                    tool_calls=[],
                    usage=TokenUsage(20, 10, 30),
                    finish_reason="stop",
                )
            return LLMResponse(
                content="Turn 1 wrote note.",
                tool_calls=[],
                usage=TokenUsage(15, 10, 25),
                finish_reason="stop",
            )

        return _ScriptedLLM(_respond)

    runtime, store, _repo = _build_runtime(
        tmp_path,
        llm_factory=_llm_factory,
        sandbox_factory=_sb_factory,
    )
    run1 = await runtime.run_turn("Create note.txt in sandbox")
    assert run1.state.value == "completed"
    assert len(sandboxes) == 1
    assert sandboxes[0].start_calls == 1
    assert sandboxes[0].stop_calls == 0
    assert sandboxes[0].files["/workspace/note.txt"] == "warm-state"
    assert len(sandboxes[0].pty_sessions) == 1

    run2 = await runtime.run_turn("Read note.txt back")
    assert run2.state.value == "completed"
    assert len(sandboxes) == 1
    assert sandboxes[0].start_calls == 1
    assert sandboxes[0].stop_calls == 0
    assert len(sandboxes[0].pty_sessions) == 1
    assert "warm-state" in runtime.session.messages[-1].content

    ctx_res = await runtime.handle_slash_command("/context")
    assert ctx_res.payload["sandbox_warm"] is True
    assert ctx_res.payload["active_terminal_sessions"] == 1

    await runtime.close()
    assert sandboxes[0].close_all_calls == 1
    assert sandboxes[0].stop_calls == 1
    assert store.load(runtime.session.id).turns[-1].response == "Saw warm-state from turn 1."


@pytest.mark.asyncio
async def test_chat_runtime_idle_timeout_restarts_warm_sandbox(tmp_path: Path) -> None:
    sandboxes: list[_WarmDummySandbox] = []
    now = [100.0]

    def _sb_factory(_cfg: SandboxConfig) -> _WarmDummySandbox:
        sb = _WarmDummySandbox()
        sandboxes.append(sb)
        return sb

    def _llm_factory(_cfg: ForgeConfig, _provider: str, _key: str) -> LLMProvider:
        return _ScriptedLLM(
            lambda _m: LLMResponse(
                content="Done.",
                tool_calls=[],
                usage=TokenUsage(10, 5, 15),
                finish_reason="stop",
            )
        )

    runtime, _store, _repo = _build_runtime(
        tmp_path,
        llm_factory=_llm_factory,
        sandbox_factory=_sb_factory,
        idle_timeout_seconds=60.0,
        clock=lambda: now[0],
    )
    await runtime.run_turn("Turn one")
    assert len(sandboxes) == 1
    assert sandboxes[0].start_calls == 1
    assert sandboxes[0].stop_calls == 0

    now[0] = 200.0
    await runtime.run_turn("Turn two after idle gap")
    assert len(sandboxes) == 2
    assert sandboxes[0].stop_calls == 1
    assert sandboxes[1].start_calls == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_chat_slash_commands_compact_model_clear_help_exit(tmp_path: Path) -> None:
    sandboxes: list[_WarmDummySandbox] = []

    def _sb_factory(_cfg: SandboxConfig) -> _WarmDummySandbox:
        sb = _WarmDummySandbox()
        sandboxes.append(sb)
        return sb

    turn = 0

    def _llm_factory(_cfg: ForgeConfig, _provider: str, _key: str) -> LLMProvider:
        nonlocal turn
        turn += 1
        padding = " ".join(f"token{i}" for i in range(60))
        reply = f"Turn {turn} detailed analysis {padding}."
        return _ScriptedLLM(
            lambda _m: LLMResponse(
                content=reply,
                tool_calls=[],
                usage=TokenUsage(20, 15, 35),
                finish_reason="stop",
            )
        )

    runtime, store, _repo = _build_runtime(
        tmp_path,
        llm_factory=_llm_factory,
        sandbox_factory=_sb_factory,
    )
    await runtime.run_turn("Question 1")
    await runtime.run_turn("Question 2")
    await runtime.run_turn("Question 3")
    await runtime.run_turn("Question 4")

    help_res = await runtime.handle_slash_command("/help")
    assert "/compact" in help_res.output
    assert "/context" in help_res.output

    bad_compact = await runtime.handle_slash_command("/compact abc")
    assert "Usage:" in bad_compact.output
    neg_compact = await runtime.handle_slash_command("/compact -2")
    assert "non-negative" in neg_compact.output

    compact_res = await runtime.handle_slash_command("/compact 1")
    assert compact_res.handled is True
    assert compact_res.payload["epoch"] == 1
    assert compact_res.payload["tokens_saved"] > 0
    assert compact_res.payload["assembled_prompt_tokens"] < compact_res.payload["raw_prompt_tokens"]

    ctx_res = await runtime.handle_slash_command("/context")
    assert ctx_res.payload["compactions"] == 1
    assert ctx_res.payload["assembled_tokens"] < ctx_res.payload["raw_tokens"]

    reloaded = store.load(runtime.session.id)
    assert reloaded.context_state["compaction_epoch"] == 1

    model_curr = await runtime.handle_slash_command("/model")
    assert model_curr.payload["provider"] == runtime.session.provider

    model_switch = await runtime.handle_slash_command("/model openai/gpt-4o")
    assert model_switch.payload == {"provider": "openai", "model": "gpt-4o"}
    assert runtime.session.provider == "openai"
    assert runtime.session.model == "gpt-4o"

    bad_model = await runtime.handle_slash_command("/model unknown-provider/foo")
    assert "failed" in bad_model.output.lower()

    unknown_cmd = await runtime.handle_slash_command("/bogus")
    assert "Unknown command" in unknown_cmd.output

    clear_res = await runtime.handle_slash_command("/clear")
    assert "Cleared" in clear_res.output
    assert runtime.session.turns == []
    assert runtime.session.messages == []

    exit_res = await runtime.handle_slash_command("/exit")
    assert exit_res.should_exit is True
    await runtime.close()


@pytest.mark.asyncio
async def test_run_chat_repl_and_resume_across_restart(tmp_path: Path) -> None:
    sandboxes: list[_WarmDummySandbox] = []

    def _sb_factory(_cfg: SandboxConfig) -> _WarmDummySandbox:
        sb = _WarmDummySandbox()
        sandboxes.append(sb)
        return sb

    prompts_seen: list[list[str]] = []

    def _llm_factory(_cfg: ForgeConfig, _provider: str, _key: str) -> LLMProvider:
        def _respond(messages: list[Message]) -> LLMResponse:
            prompts_seen.append([m.content for m in messages if m.role != Role.SYSTEM])
            return LLMResponse(
                content=f"Ack #{len(prompts_seen)}",
                tool_calls=[],
                usage=TokenUsage(15, 10, 25),
                finish_reason="stop",
            )

        return _ScriptedLLM(_respond)

    runtime1, store, repo = _build_runtime(
        tmp_path,
        llm_factory=_llm_factory,
        sandbox_factory=_sb_factory,
    )
    inputs1 = iter(["First task prompt", "/model openai/gpt-4o", "/context", "/exit"])
    outputs1: list[str] = []
    await run_chat_repl(runtime1, input_fn=lambda _p: next(inputs1), output_fn=outputs1.append)
    assert any("Ack #1" in line for line in outputs1)
    assert any("Switched model to openai/gpt-4o" in line for line in outputs1)
    assert sandboxes[0].stop_calls == 1

    restored = store.load(runtime1.session.id)
    assert restored.provider == "openai"
    assert restored.model == "gpt-4o"

    cfg2 = ForgeConfig()
    runtime2 = ChatSessionRuntime(
        cfg2,
        restored,
        store,
        approval_mode="auto",
        sandbox_factory=_sb_factory,
        provider_factory=_llm_factory,
        tools_factory=lambda: create_default_registry(load_plugins=False),
        api_key_resolver=lambda _cfg, _prov: "test-key",
    )
    assert runtime2.session.repo_path == str(repo.resolve())
    inputs2 = iter(["Followup turn", "/quit"])
    outputs2: list[str] = []
    await run_chat_repl(runtime2, input_fn=lambda _p: next(inputs2), output_fn=outputs2.append)
    assert any("Ack #2" in line for line in outputs2)
    assert any("First task prompt" in item for item in prompts_seen[-1])
    assert sandboxes[1].stop_calls == 1


def test_cli_session_list_show_compact_delete(tmp_path: Path) -> None:
    store_dir = tmp_path / "sessions"
    repo = tmp_path / "repo"
    repo.mkdir()
    store = SessionStore(base_dir=store_dir)
    padding = " ".join(f"word{i}" for i in range(45))
    sess = DevSession(
        id="sess-cli-1",
        repo_path=str(repo.resolve()),
        title="Investigate auth flow",
        provider="gemini",
        model="gemini-3.1-flash-lite-preview",
        messages=[
            Message(role=Role.SYSTEM, content="system"),
            Message(role=Role.USER, content=f"Turn 1 {padding}"),
            Message(role=Role.ASSISTANT, content=f"Reply 1 {padding}"),
            Message(role=Role.USER, content=f"Turn 2 {padding}"),
            Message(role=Role.ASSISTANT, content="Reply 2"),
        ],
    )
    store.save(sess)

    runner = CliRunner()
    list_res = runner.invoke(
        main,
        ["session", "list", "--sessions-dir", str(store_dir), "--output-format", "json"],
    )
    assert list_res.exit_code == 0, list_res.output
    listed = json.loads(list_res.output)
    assert [item["id"] for item in listed] == ["sess-cli-1"]

    show_res = runner.invoke(
        main,
        [
            "session",
            "show",
            "sess-cli-1",
            "--sessions-dir",
            str(store_dir),
            "--output-format",
            "json",
        ],
    )
    assert show_res.exit_code == 0, show_res.output
    shown = json.loads(show_res.output)
    assert shown["id"] == "sess-cli-1"
    assert shown["messages"] == 5

    compact_res = runner.invoke(
        main,
        [
            "session",
            "compact",
            "sess-cli-1",
            "--keep-recent",
            "1",
            "--sessions-dir",
            str(store_dir),
            "--output-format",
            "json",
        ],
    )
    assert compact_res.exit_code == 0, compact_res.output
    compacted = json.loads(compact_res.output)
    assert compacted["compaction_epoch"] == 1
    assert compacted["tokens_saved"] > 0

    del_res = runner.invoke(
        main,
        [
            "session",
            "delete",
            "sess-cli-1",
            "--sessions-dir",
            str(store_dir),
            "--output-format",
            "json",
        ],
    )
    assert del_res.exit_code == 0, del_res.output
    assert json.loads(del_res.output) == {"session_id": "sess-cli-1", "deleted": True}

    missing_res = runner.invoke(
        main,
        [
            "session",
            "delete",
            "sess-cli-1",
            "--sessions-dir",
            str(store_dir),
            "--output-format",
            "json",
        ],
    )
    assert missing_res.exit_code != 0


def test_cli_chat_command_resumes_latest_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_dir = tmp_path / "sessions"
    repo = tmp_path / "repo"
    repo.mkdir()
    store = SessionStore(base_dir=store_dir)
    saved = DevSession(
        id="latest-sess-1",
        repo_path=str(repo.resolve()),
        title="Existing chat",
        provider="gemini",
        model="gemini-3.1-flash-lite-preview",
        messages=[
            Message(role=Role.SYSTEM, content="system"),
            Message(role=Role.USER, content="hi"),
            Message(role=Role.ASSISTANT, content="hello"),
        ],
    )
    store.save(saved)

    captured: dict[str, Any] = {}

    async def _fake_repl(runtime: ChatSessionRuntime, **_kwargs: Any) -> DevSession:
        captured["session_id"] = runtime.session.id
        captured["idle_timeout"] = runtime.idle_timeout_seconds
        await runtime.close()
        return runtime.session

    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setattr("agent_forge.agent.chat.run_chat_repl", _fake_repl)

    runner = CliRunner()
    res = runner.invoke(
        main,
        [
            "chat",
            "--repo",
            str(repo),
            "--resume",
            "latest",
            "--sessions-dir",
            str(store_dir),
            "--idle-timeout",
            "300",
        ],
    )
    assert res.exit_code == 0, res.output
    assert captured == {"session_id": "latest-sess-1", "idle_timeout": 300.0}
