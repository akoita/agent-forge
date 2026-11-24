"""Unit tests for live streaming, EventBus tool progress, prompt history, and approvals in chat."""

from __future__ import annotations

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
    TokenUsage,
    ToolCall,
)
from agent_forge.sandbox.base import ExecResult, Sandbox, SandboxConfig
from agent_forge.tools import create_default_registry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from agent_forge.agent.approvals import ApprovalRequest


class _DummySandbox(Sandbox):
    def __init__(self) -> None:
        self.alive = False
        self.files: dict[str, str] = {}
        self.commands: list[str] = []

    async def start(self, repo_path: str, config: SandboxConfig | None = None) -> None:
        del repo_path, config
        self.alive = True

    async def exec(self, command: str, *, timeout_seconds: int = 30) -> ExecResult:
        self.commands.append(command)
        return ExecResult(exit_code=0, stdout=f"ran:{command}", stderr="")

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


class _StreamingLLM(LLMProvider):
    """Test double emitting configurable streaming chunks and/or complete fallback."""

    def __init__(
        self,
        stream_fn: Callable[[list[Message]], list[LLMResponse]] | None = None,
        complete_fn: Callable[[list[Message]], LLMResponse] | None = None,
    ) -> None:
        self._stream_fn = stream_fn
        self._complete_fn = complete_fn

    async def complete(
        self,
        messages: list[Message],
        tools: Any = None,
        config: LLMConfig | None = None,
    ) -> LLMResponse:
        del tools, config
        if self._complete_fn is None:
            raise AssertionError("Unexpected complete() call")
        return self._complete_fn(messages)

    async def stream(
        self,
        messages: list[Message],
        tools: Any = None,
        config: LLMConfig | None = None,
    ) -> AsyncIterator[LLMResponse]:
        del tools, config
        if self._stream_fn is None:
            raise NotImplementedError
        for chunk in self._stream_fn(messages):
            yield chunk


def _make_runtime(
    tmp_path: Path,
    *,
    llm_factory: Callable[[ForgeConfig, str, str], LLMProvider],
    sandbox: _DummySandbox | None = None,
    approval_mode: str = "auto",
) -> ChatSessionRuntime:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / "app.py").write_text("VALUE = 42\n", encoding="utf-8")
    store = SessionStore(base_dir=tmp_path / "sessions")
    cfg = ForgeConfig()
    cfg.agent.approval_mode = approval_mode  # type: ignore[assignment]
    session = DevSession(
        repo_path=str(repo.resolve()),
        provider=cfg.agent.default_provider,
        model=cfg.agent.default_model,
        approval_mode=approval_mode,
        sandbox_backend="docker",
        sandbox_image=cfg.sandbox.image,
    )
    sb = sandbox or _DummySandbox()
    return ChatSessionRuntime(
        cfg,
        session,
        store,
        approval_mode=approval_mode,
        sandbox_factory=lambda _c: sb,
        provider_factory=llm_factory,
        tools_factory=lambda: create_default_registry(load_plugins=False),
        api_key_resolver=lambda _c, _p: "test-key",
    )


@pytest.mark.asyncio
async def test_live_streaming_tool_progress_and_no_duplicate_output(tmp_path: Path) -> None:
    sb = _DummySandbox()
    sb.files["/workspace/app.py"] = "VALUE = 42\n"
    timeline: list[tuple[str, str]] = []
    call_num = 0

    def _llm_factory(_cfg: ForgeConfig, _p: str, _k: str) -> LLMProvider:
        def _stream(_messages: list[Message]) -> list[LLMResponse]:
            nonlocal call_num
            call_num += 1
            if call_num == 1:
                return [
                    LLMResponse(content="Let me ", finish_reason=""),
                    LLMResponse(
                        content="check that.",
                        tool_calls=[
                            ToolCall(
                                id="call-r1",
                                name="read_file",
                                arguments={"path": "/workspace/app.py"},
                            )
                        ],
                        usage=TokenUsage(15, 10, 25),
                        finish_reason="tool_calls",
                    ),
                ]
            return [
                LLMResponse(content="The value ", finish_reason=""),
                LLMResponse(
                    content="is 42.",
                    usage=TokenUsage(25, 8, 33),
                    finish_reason="stop",
                ),
            ]

        return _StreamingLLM(stream_fn=_stream)

    runtime = _make_runtime(tmp_path, llm_factory=_llm_factory, sandbox=sb)
    outputs: list[str] = []
    inputs = iter(["Check app.py", "/exit"])

    await run_chat_repl(
        runtime,
        input_fn=lambda _p: next(inputs),
        output_fn=outputs.append,
        stream_fn=lambda chunk: timeline.append(("stream", chunk)),
        status_fn=lambda status: timeline.append(("status", status)),
        stream_tokens=True,
    )

    streamed_text = "".join(text for kind, text in timeline if kind == "stream")
    status_lines = [text for kind, text in timeline if kind == "status"]
    assert "Let me check that.\n" in streamed_text
    assert "The value is 42.\n" in streamed_text
    assert any("-> read_file(path=/workspace/app.py)" in line for line in status_lines)
    assert any("[ok] read_file" in line for line in status_lines)
    # Coordinator must flush a newline before printing tool status after partial stream text
    idx_status = next(
        i for i, (kind, val) in enumerate(timeline) if kind == "status" and "read_file" in val
    )
    assert timeline[idx_status - 1] == ("stream", "\n")
    # Final reply must not be printed twice when stream_fn already emitted it
    assert not any("The value is 42." in out for out in outputs)


@pytest.mark.asyncio
async def test_streaming_falls_back_to_complete_when_unimplemented(tmp_path: Path) -> None:
    def _llm_factory(_cfg: ForgeConfig, _p: str, _k: str) -> LLMProvider:
        return _StreamingLLM(
            stream_fn=None,
            complete_fn=lambda _m: LLMResponse(
                content="Fallback response.",
                usage=TokenUsage(10, 5, 15),
                finish_reason="stop",
            ),
        )

    runtime = _make_runtime(tmp_path, llm_factory=_llm_factory)
    streamed: list[str] = []
    outputs: list[str] = []
    inputs = iter(["Hello", "/exit"])

    await run_chat_repl(
        runtime,
        input_fn=lambda _p: next(inputs),
        output_fn=outputs.append,
        stream_fn=streamed.append,
        stream_tokens=True,
    )
    assert "".join(streamed) == "Fallback response.\n"
    assert not any("Fallback response." in line for line in outputs)


@pytest.mark.asyncio
async def test_multiline_input_and_persistent_history_across_restarts(tmp_path: Path) -> None:
    seen_prompts: list[str] = []

    def _llm_factory(_cfg: ForgeConfig, _p: str, _k: str) -> LLMProvider:
        def _complete(messages: list[Message]) -> LLMResponse:
            seen_prompts.append(messages[-1].content)
            return LLMResponse(
                content=f"Got {len(seen_prompts)}",
                usage=TokenUsage(10, 5, 15),
                finish_reason="stop",
            )

        return _StreamingLLM(complete_fn=_complete)

    history_file = tmp_path / "state" / "chat_history"
    rt1 = _make_runtime(tmp_path, llm_factory=_llm_factory)
    prompts1 = iter(
        [
            "line one \\",
            "line two",
            '"""',
            "block line A",
            "block line B",
            '"""',
            "/exit",
        ]
    )
    out1: list[str] = []
    await run_chat_repl(
        rt1,
        input_fn=lambda _p: next(prompts1),
        output_fn=out1.append,
        history_path=history_file,
        stream_tokens=False,
    )

    assert seen_prompts == ["line one \nline two", "block line A\nblock line B"]
    saved_lines = history_file.read_text(encoding="utf-8").splitlines()
    assert saved_lines == [
        "line one  line two",
        "block line A block line B",
        "/exit",
    ]

    # Restart REPL and verify existing history entries are preserved and appended
    rt2 = _make_runtime(tmp_path, llm_factory=_llm_factory)
    prompts2 = iter(["follow-up", "/exit"])
    await run_chat_repl(
        rt2,
        input_fn=lambda _p: next(prompts2),
        output_fn=lambda _s: None,
        history_path=history_file,
        stream_tokens=False,
    )
    reloaded_lines = history_file.read_text(encoding="utf-8").splitlines()
    assert reloaded_lines == [*saved_lines, "follow-up", "/exit"]


@pytest.mark.asyncio
async def test_interactive_approval_prompt_flushes_stream_and_honors_decision(
    tmp_path: Path,
) -> None:
    sb = _DummySandbox()
    approval_requests: list[ApprovalRequest] = []
    timeline: list[tuple[str, str]] = []
    step = 0

    def _llm_factory(_cfg: ForgeConfig, _p: str, _k: str) -> LLMProvider:
        def _stream(_messages: list[Message]) -> list[LLMResponse]:
            nonlocal step
            step += 1
            if step == 1:
                return [
                    LLMResponse(content="Trying cleanup", finish_reason=""),
                    LLMResponse(
                        content=None,
                        tool_calls=[
                            ToolCall(
                                id="call-rm-1",
                                name="run_shell",
                                arguments={"command": "rm -rf /workspace/old"},
                            )
                        ],
                        usage=TokenUsage(10, 5, 15),
                        finish_reason="tool_calls",
                    ),
                ]
            if step == 2:
                return [
                    LLMResponse(
                        content="Denied, running again safely",
                        tool_calls=[
                            ToolCall(
                                id="call-rm-2",
                                name="run_shell",
                                arguments={"command": "rm -rf /workspace/tmp"},
                            )
                        ],
                        usage=TokenUsage(12, 5, 17),
                        finish_reason="tool_calls",
                    ),
                ]
            return [
                LLMResponse(
                    content="Approved cleanup done.",
                    usage=TokenUsage(14, 5, 19),
                    finish_reason="stop",
                )
            ]

        return _StreamingLLM(stream_fn=_stream)

    def _confirm(req: ApprovalRequest) -> bool:
        approval_requests.append(req)
        timeline.append(("confirm", req.reason))
        # Deny first call, approve second call
        return len(approval_requests) > 1

    runtime = _make_runtime(tmp_path, llm_factory=_llm_factory, sandbox=sb, approval_mode="prompt")
    inputs = iter(["Clean old artifacts", "/exit"])

    await run_chat_repl(
        runtime,
        input_fn=lambda _p: next(inputs),
        output_fn=lambda _s: None,
        stream_fn=lambda c: timeline.append(("stream", c)),
        status_fn=lambda s: timeline.append(("status", s)),
        confirm_fn=_confirm,
        stream_tokens=True,
    )

    assert len(approval_requests) == 2
    assert sb.commands == ["rm -rf /workspace/tmp"]
    assert any("denied" in val for kind, val in timeline if kind == "status")
    assert any("approved" in val for kind, val in timeline if kind == "status")
    confirm_idx = next(i for i, (kind, _) in enumerate(timeline) if kind == "confirm")
    assert timeline[confirm_idx - 1][0] != "stream" or timeline[confirm_idx - 1][1] == "\n"


def test_cli_chat_wires_stream_and_history_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_dir = tmp_path / "sessions"
    history_file = tmp_path / "my_history"
    repo = tmp_path / "repo"
    repo.mkdir()
    captured: dict[str, Any] = {}

    async def _fake_repl(runtime: ChatSessionRuntime, **kwargs: Any) -> DevSession:
        captured["stream_tokens"] = kwargs.get("stream_tokens")
        captured["stream_fn"] = kwargs.get("stream_fn")
        captured["history_path"] = kwargs.get("history_path")
        captured["approval_mode"] = runtime.approval_mode
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
            "--sessions-dir",
            str(store_dir),
            "--no-stream",
            "--history-file",
            str(history_file),
            "--approval-mode",
            "deny",
        ],
    )
    assert res.exit_code == 0, res.output
    assert captured["stream_tokens"] is False
    assert captured["stream_fn"] is None
    assert captured["history_path"] == history_file
    assert captured["approval_mode"] == "deny"
