"""Unit tests for Issue #25: deterministic persistent repo memory (.agent-forge/memory.md)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from agent_forge.agent.chat import ChatSessionRuntime
from agent_forge.agent.context import ContextEngine, ContextPolicy
from agent_forge.agent.memory import DEFAULT_MEMORY_PATH, RepoMemoryStore
from agent_forge.agent.runner import render_task_system_prompt
from agent_forge.agent.session import DevSession, SessionStore
from agent_forge.cli import main
from agent_forge.config import ForgeConfig, load_config
from agent_forge.sandbox.base import SandboxConfig
from agent_forge.tools import create_default_registry

if TYPE_CHECKING:
    from pathlib import Path


def test_repo_memory_store_crud_dedup_bullets_and_path_confinement(tmp_path: Path) -> None:
    store = RepoMemoryStore(tmp_path)
    assert store.path == (tmp_path / DEFAULT_MEMORY_PATH).resolve()
    assert store.load_entries() == []

    # Add and deduplicate identically normalized learnings
    e1, added1 = store.add(
        "Always run pytest -q before committing\n", category="Testing", created_at="2026-11-25"
    )
    assert added1 is True
    assert e1.category == "testing"
    _, added_dup = store.add("Always run   pytest -q before committing", category="testing")
    assert added_dup is False

    # Plain human-authored markdown bullet is also parsed cleanly
    with store.path.open("a", encoding="utf-8") as fh:
        fh.write("- Avoid mutating fixture globals in conftest.py\n")

    entries = store.load_entries()
    assert len(entries) == 2
    assert store.search("pytest")[0].id == e1.id

    # Render honors token budget
    rendered_small = store.render_for_prompt(max_tokens=20, max_entries=10)
    assert rendered_small.truncated is True
    assert "## Repository Memory" in rendered_small.rendered

    # Remove by unambiguous prefix and clear remaining
    assert store.remove(e1.id[:7]) is True
    assert len(store.load_entries()) == 1
    assert store.clear() == 1
    assert store.load_entries() == []

    # Reject escaping repository boundary
    with pytest.raises(ValueError, match="escapes repository root"):
        RepoMemoryStore(tmp_path, relative_path="../outside.md")
    with pytest.raises(ValueError, match="repository-relative"):
        RepoMemoryStore(tmp_path, relative_path="/etc/memory.md")


def test_context_engine_injects_memory_into_static_prompt_prefix(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("# Guide\nFollow conventions.\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("def greet() -> str:\n    return 'hi'\n", encoding="utf-8")
    RepoMemoryStore(tmp_path).add(
        "Use port 8081 when testing the local health service",
        category="workflow",
        created_at="2026-11-25",
    )

    tools = create_default_registry(load_plugins=False)
    sb_cfg = SandboxConfig(backend="docker")

    p1 = render_task_system_prompt(
        "Fix bug A",
        tools,
        sb_cfg,
        repo_path=tmp_path,
        context_engine=ContextEngine(tmp_path, task="Fix bug A"),
        discover_extensions=False,
    )
    p2 = render_task_system_prompt(
        "Fix bug B",
        tools,
        sb_cfg,
        repo_path=tmp_path,
        context_engine=ContextEngine(tmp_path, task="Fix bug B"),
        discover_extensions=False,
    )
    assert "Use port 8081 when testing the local health service" in p1
    prefix1 = p1.split("## Task", 1)[0]
    prefix2 = p2.split("## Task", 1)[0]
    assert prefix1 == prefix2

    # Opt-out omits memory cleanly
    p_disabled = render_task_system_prompt(
        "Fix bug C",
        tools,
        sb_cfg,
        repo_path=tmp_path,
        context_engine=ContextEngine(
            tmp_path, task="Fix bug C", policy=ContextPolicy(memory_enabled=False)
        ),
        discover_extensions=False,
    )
    assert "Use port 8081" not in p_disabled


def test_memory_config_env_cli_and_chat_slash_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AGENT_FORGE_CONTEXT_MEMORY_ENABLED", "false")
    monkeypatch.setenv("AGENT_FORGE_CONTEXT_MAX_MEMORY_TOKENS", "512")
    cfg = load_config(project_path=tmp_path)
    assert cfg.context.memory_enabled is False
    assert cfg.context.max_memory_tokens == 512

    runner = CliRunner()
    res_add = runner.invoke(
        main,
        [
            "memory",
            "add",
            "Prefer targeted pytest -q runs",
            "--repo",
            str(tmp_path),
            "--output-format",
            "json",
        ],
    )
    assert res_add.exit_code == 0
    added_payload = json.loads(res_add.output)
    assert added_payload["added"] is True
    entry_id = added_payload["id"]

    res_list = runner.invoke(
        main,
        ["memory", "list", "--repo", str(tmp_path), "--output-format", "json"],
    )
    assert res_list.exit_code == 0
    listed = json.loads(res_list.output)
    assert len(listed) == 1
    assert listed[0]["id"] == entry_id

    res_rem = runner.invoke(main, ["memory", "remove", entry_id, "--repo", str(tmp_path)])
    assert res_rem.exit_code == 0
    assert RepoMemoryStore(tmp_path).load_entries() == []


@pytest.mark.asyncio
async def test_chat_slash_memory_updates_store_and_context_engine(tmp_path: Path) -> None:
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = DevSession(repo_path=str(tmp_path), id="chat-mem-25")
    rt = ChatSessionRuntime(ForgeConfig(), session, store)
    rt.context_engine = ContextEngine(tmp_path, task="Initial chat")
    rt.context_engine.prepare_initial_context()

    r_add = await rt.handle_slash_command("/memory add Keep migrations reversible")
    assert r_add.handled is True
    assert r_add.payload["added"] is True
    assert "Keep migrations reversible" in rt.context_engine.repo_memory_rendered

    r_list = await rt.handle_slash_command("/memory")
    assert r_list.handled is True
    assert "Keep migrations reversible" in r_list.output

    entry_id = str(r_add.payload["id"])
    r_rm = await rt.handle_slash_command(f"/memory remove {entry_id}")
    assert r_rm.handled is True
    assert r_rm.payload["removed"] is True
    assert rt.context_engine.repo_memory_rendered == ""

    r_clear = await rt.handle_slash_command("/memory clear")
    assert r_clear.handled is True
    assert r_clear.payload["cleared"] == 0
