"""Unit tests for /diff, /undo, /sessions, and /resume chat workspace controls."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from agent_forge.agent.chat import ChatSessionRuntime, run_chat_repl
from agent_forge.agent.session import DevSession, SessionStore
from agent_forge.config import ForgeConfig
from agent_forge.llm.base import LLMConfig, LLMProvider, LLMResponse, Message, TokenUsage
from agent_forge.sandbox.base import ExecResult, Sandbox, SandboxConfig
from agent_forge.tools import create_default_registry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable


def _git(repo: Path, *args: str) -> None:
    git_exe = shutil.which("git")
    assert git_exe is not None
    res = subprocess.run(  # noqa: S603
        [git_exe, *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr


def _init_git_repo(repo: Path) -> Path:
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-m", "init")
    return repo


class _WorkspaceSandbox(Sandbox):
    """Sandbox that mutates files on the host workspace directory directly for turn tests."""

    def __init__(self, on_turn_mutate: Callable[[Path], None] | None = None) -> None:
        self.alive = False
        self.start_calls = 0
        self.stop_calls = 0
        self.repo_path: Path | None = None
        self._on_turn_mutate = on_turn_mutate

    async def start(self, repo_path: str, config: SandboxConfig | None = None) -> None:
        del config
        self.start_calls += 1
        self.alive = True
        self.repo_path = Path(repo_path)

    async def exec(self, command: str, *, timeout_seconds: int = 30) -> ExecResult:
        del timeout_seconds
        if self._on_turn_mutate is not None and self.repo_path is not None:
            self._on_turn_mutate(self.repo_path)
        return ExecResult(exit_code=0, stdout=f"ok:{command}", stderr="")

    async def read_file(self, path: str) -> str:
        assert self.repo_path is not None
        rel = path.removeprefix("/workspace/").lstrip("/")
        return (self.repo_path / rel).read_text(encoding="utf-8")

    async def write_file(self, path: str, content: str) -> None:
        assert self.repo_path is not None
        rel = path.removeprefix("/workspace/").lstrip("/")
        target = self.repo_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    async def stop(self) -> None:
        self.stop_calls += 1
        self.alive = False

    async def is_alive(self) -> bool:
        return self.alive


class _TurnMutatingLLM(LLMProvider):
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


def _build_runtime(
    repo: Path,
    store: SessionStore,
    *,
    sandbox_factory: Callable[[SandboxConfig], _WorkspaceSandbox],
    llm_factory: Callable[[ForgeConfig, str, str], LLMProvider],
    session_id: str | None = None,
) -> ChatSessionRuntime:
    cfg = ForgeConfig()
    kwargs: dict[str, Any] = {
        "repo_path": str(repo.resolve()),
        "provider": cfg.agent.default_provider,
        "model": cfg.agent.default_model,
    }
    if session_id is not None:
        kwargs["id"] = session_id
    sess = DevSession(**kwargs)
    return ChatSessionRuntime(
        cfg,
        sess,
        store,
        sandbox_factory=sandbox_factory,
        provider_factory=llm_factory,
        tools_factory=lambda: create_default_registry(load_plugins=False),
        api_key_resolver=lambda _c, _p: "test-key",
    )


@pytest.mark.asyncio
async def test_slash_diff_and_non_git_workspace(tmp_path: Path) -> None:
    repo = _init_git_repo(tmp_path / "git_repo")
    store = SessionStore(base_dir=tmp_path / "sessions")
    sb = _WorkspaceSandbox()
    rt = _build_runtime(
        repo,
        store,
        sandbox_factory=lambda _c: sb,
        llm_factory=lambda _c, _p, _k: _TurnMutatingLLM(
            lambda _m: LLMResponse(content="ok", usage=TokenUsage(1, 1, 2), finish_reason="stop")
        ),
    )

    clean = await rt.handle_slash_command("/diff")
    assert clean.payload["clean"] is True
    assert "clean" in clean.output.lower()

    (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    (repo / "extra.txt").write_text("new\n", encoding="utf-8")

    dirty = await rt.handle_slash_command("/diff")
    assert dirty.payload["clean"] is False
    assert "VALUE = 2" in dirty.output
    assert "extra.txt" in dirty.output

    stat = await rt.handle_slash_command("/diff --stat")
    assert "app.py" in stat.payload["diff_text"]
    assert "VALUE = 2" not in stat.payload["diff_text"]
    await rt.close()

    plain_dir = tmp_path / "plain_repo"
    plain_dir.mkdir()
    rt_plain = _build_runtime(
        plain_dir,
        store,
        sandbox_factory=lambda _c: sb,
        llm_factory=lambda _c, _p, _k: _TurnMutatingLLM(
            lambda _m: LLMResponse(content="ok", usage=TokenUsage(1, 1, 2), finish_reason="stop")
        ),
    )
    non_git = await rt_plain.handle_slash_command("/diff")
    assert non_git.payload["is_git_repo"] is False
    assert "not a Git repository" in non_git.output
    await rt_plain.close()


@pytest.mark.asyncio
async def test_slash_undo_preserves_prior_local_edits_and_guards_conflicts(tmp_path: Path) -> None:
    repo = _init_git_repo(tmp_path / "repo")
    # Developer already had an uncommitted local edit BEFORE the agent turn ran
    (repo / "app.py").write_text("VALUE = 1\nLOCAL_EDIT = True\n", encoding="utf-8")

    store = SessionStore(base_dir=tmp_path / "sessions")
    sb = _WorkspaceSandbox()

    turn_num = 0

    def _llm_factory(_c: ForgeConfig, _p: str, _k: str) -> LLMProvider:
        nonlocal turn_num
        turn_num += 1
        cur = turn_num

        def _respond(_m: list[Message]) -> LLMResponse:
            if cur == 1:
                (repo / "app.py").write_text("VALUE = 99\nLOCAL_EDIT = True\n", encoding="utf-8")
                (repo / "created_by_turn.py").write_text("CREATED = 1\n", encoding="utf-8")
                return LLMResponse(
                    content="Edited app.py and created file.",
                    usage=TokenUsage(10, 5, 15),
                    finish_reason="stop",
                )
            (repo / "second_turn.py").write_text("SECOND = 2\n", encoding="utf-8")
            return LLMResponse(
                content="Created second_turn.py.",
                usage=TokenUsage(10, 5, 15),
                finish_reason="stop",
            )

        return _TurnMutatingLLM(_respond)

    rt = _build_runtime(repo, store, sandbox_factory=lambda _c: sb, llm_factory=_llm_factory)

    empty_undo = await rt.handle_slash_command("/undo")
    assert empty_undo.payload["reverted"] is False

    await rt.run_turn("Modify app.py and add created_by_turn.py")
    await rt.run_turn("Add second_turn.py")

    # External edit to second_turn.py should block /undo cleanly
    (repo / "second_turn.py").write_text("SECOND = 999 # edited outside REPL\n", encoding="utf-8")
    blocked = await rt.handle_slash_command("/undo")
    assert blocked.payload["reverted"] is False
    assert blocked.payload["reason"] == "conflict"
    assert "second_turn.py" in blocked.payload["conflicts"]

    # /undo --force overrides external edit conflict for turn 2
    forced = await rt.handle_slash_command("/undo --force")
    assert forced.payload["reverted"] is True
    assert not (repo / "second_turn.py").exists()

    # Next /undo reverts turn 1: removes created_by_turn.py AND restores prior LOCAL_EDIT!
    undone_turn1 = await rt.handle_slash_command("/undo")
    assert undone_turn1.payload["reverted"] is True
    assert not (repo / "created_by_turn.py").exists()
    assert (repo / "app.py").read_text(encoding="utf-8") == "VALUE = 1\nLOCAL_EDIT = True\n"
    await rt.close()


@pytest.mark.asyncio
async def test_slash_sessions_and_in_place_resume(tmp_path: Path) -> None:
    repo_a = _init_git_repo(tmp_path / "repo_a")
    repo_b = _init_git_repo(tmp_path / "repo_b")
    store = SessionStore(base_dir=tmp_path / "sessions")
    sandboxes: list[_WorkspaceSandbox] = []

    def _sb_factory(_c: SandboxConfig) -> _WorkspaceSandbox:
        sb = _WorkspaceSandbox()
        sandboxes.append(sb)
        return sb

    def _llm_factory(_c: ForgeConfig, _p: str, _k: str) -> LLMProvider:
        return _TurnMutatingLLM(
            lambda _m: LLMResponse(content="Ack", usage=TokenUsage(5, 5, 10), finish_reason="stop")
        )

    # Seed an older session in repo_a and another session in repo_b
    rt_other = _build_runtime(
        repo_b,
        store,
        sandbox_factory=_sb_factory,
        llm_factory=_llm_factory,
        session_id="sess-repo-b",
    )
    await rt_other.run_turn("Repo B work")
    await rt_other.close()

    rt_old_a = _build_runtime(
        repo_a,
        store,
        sandbox_factory=_sb_factory,
        llm_factory=_llm_factory,
        session_id="sess-repo-a-1",
    )
    await rt_old_a.run_turn("Repo A earlier task")
    await rt_old_a.close()

    # Active runtime in repo_a starts its own warm sandbox
    rt_active = _build_runtime(
        repo_a,
        store,
        sandbox_factory=_sb_factory,
        llm_factory=_llm_factory,
        session_id="sess-repo-a-active",
    )
    inputs = iter(
        ["Active repo A task", "/sessions", "/sessions --all", "/resume sess-repo-a-1", "/exit"]
    )
    outputs: list[str] = []
    await run_chat_repl(rt_active, input_fn=lambda _p: next(inputs), output_fn=outputs.append)

    sessions_repo_a = next(
        line for line in outputs if "Stored sessions:" in line and "sess-repo-b" not in line
    )
    assert "sess-repo-a-1" in sessions_repo_a
    assert "sess-repo-a-active" in sessions_repo_a

    sessions_all = next(line for line in outputs if "sess-repo-b" in line)
    assert "sess-repo-a-1" in sessions_all
    assert any("Resumed session sess-repo-a-1" in line for line in outputs)

    # Same-repo switch kept the warm sandbox alive across /resume and only closed once on /exit
    assert sandboxes[-1].start_calls == 1
    assert sandboxes[-1].stop_calls == 1

    # Switching across DIFFERENT repos closes the old repo's warm sandbox immediately
    rt_cross = _build_runtime(
        repo_a,
        store,
        sandbox_factory=_sb_factory,
        llm_factory=_llm_factory,
        session_id="sess-cross-start",
    )
    await rt_cross.run_turn("Warm up repo A sandbox")
    sb_cross = rt_cross.sandbox
    assert sb_cross is not None
    assert await sb_cross.is_alive() is True
    cross_res = await rt_cross.handle_slash_command("/resume sess-repo-b")
    assert cross_res.payload["session_id"] == "sess-repo-b"
    assert rt_cross.sandbox is None
    assert sb_cross.stop_calls == 1
    await rt_cross.close()
