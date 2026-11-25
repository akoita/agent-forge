"""Tests for Issue #224: polyglot tree-sitter repo map & post-edit diagnostics loop."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from agent_forge.agent.core import react_loop
from agent_forge.agent.diagnostics import (
    extract_edited_paths,
    parse_diagnostic_output,
    run_post_edit_diagnostics,
    select_diagnostic_command,
)
from agent_forge.agent.models import AgentConfig, AgentRun, RunState
from agent_forge.agent.repo_map import build_repo_map
from agent_forge.agent.treesitter import (
    extract_treesitter_file_symbols,
    is_treesitter_available,
    supported_treesitter_languages,
)
from agent_forge.cli import _build_agent_config, _build_cli_overrides
from agent_forge.config import load_config
from agent_forge.llm.base import (
    LLMConfig,
    LLMProvider,
    LLMResponse,
    Message,
    TokenUsage,
    ToolCall,
    ToolDefinition,
)
from agent_forge.sandbox.base import ExecResult, Sandbox, SandboxConfig
from agent_forge.tools import ReadFileTool, ToolRegistry, ToolResult, WriteFileTool

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


@dataclass
class _FakeNode:
    type: str
    start_byte: int
    end_byte: int
    start_point: tuple[int, int]
    children: list[_FakeNode]
    fields: dict[str, _FakeNode]

    def child_by_field_name(self, name: str) -> _FakeNode | None:
        return self.fields.get(name)


@dataclass
class _FakeTree:
    root_node: _FakeNode


class _FakeTSParser:
    def __init__(self, tree: _FakeTree) -> None:
        self._tree = tree

    def parse(self, source_bytes: bytes) -> _FakeTree:
        del source_bytes
        return self._tree


class _DiagSubprocessSandbox(Sandbox):
    """Subprocess-backed sandbox executing real shell/python checks in tmp_path."""

    def __init__(self) -> None:
        self.root: Path | None = None
        self.exec_commands: list[str] = []

    def _resolve(self, path: str) -> Path:
        assert self.root is not None
        rel = path.removeprefix("/workspace/").lstrip("/")
        return self.root if rel in {"", "."} else self.root / rel

    async def start(self, repo_path: str, config: SandboxConfig | None = None) -> None:
        from pathlib import Path

        del config
        self.root = Path(repo_path)

    async def exec(self, command: str, *, timeout_seconds: int = 30) -> ExecResult:
        assert self.root is not None
        self.exec_commands.append(command)
        mapped = command.replace("/workspace", str(self.root))
        proc = await asyncio.create_subprocess_exec(
            "/bin/sh",
            "-c",
            mapped,
            cwd=str(self.root),
            env={"PATH": os.environ.get("PATH", os.defpath)},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out_b, err_b = await asyncio.wait_for(
            proc.communicate(),
            timeout=float(timeout_seconds),
        )
        return ExecResult(
            exit_code=int(proc.returncode or 0),
            stdout=out_b.decode("utf-8", errors="replace"),
            stderr=err_b.decode("utf-8", errors="replace"),
        )

    async def read_file(self, path: str) -> str:
        return self._resolve(path).read_text(encoding="utf-8")

    async def write_file(self, path: str, content: str) -> None:
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    async def list_files(self, path: str = "/workspace") -> list[str]:
        del path
        assert self.root is not None
        return [p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file()]

    async def stop(self) -> None:
        return None

    async def is_alive(self) -> bool:
        return self.root is not None


class _DiagLLM(LLMProvider):
    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = responses
        self._idx = 0
        self.seen_tool_outputs: list[str] = []

    @property
    def model_name(self) -> str:
        return "diag-test"

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: LLMConfig | None = None,
    ) -> LLMResponse:
        del tools, config
        for msg in messages:
            if msg.role.value == "tool" and msg.content not in self.seen_tool_outputs:
                self.seen_tool_outputs.append(msg.content)
        resp = self._responses[min(self._idx, len(self._responses) - 1)]
        self._idx += 1
        return resp

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: LLMConfig | None = None,
    ) -> AsyncIterator[LLMResponse]:
        del messages, tools, config
        if False:  # pragma: no cover
            yield LLMResponse(content=None)


def test_treesitter_extractor_handles_fake_ast_and_graceful_fallback() -> None:
    assert "sol" in supported_treesitter_languages()
    assert isinstance(is_treesitter_available(), bool)

    src = (
        'import { Foo } from "./foo";\n'
        "// class CommentDecoy {}\n"
        "class Widget {\n"
        "  render(ctx) { return Foo(ctx); }\n"
        "}\n"
        "function helper(x) { return x; }\n"
    )

    def _span(needle: str) -> tuple[int, int]:
        idx = src.index(needle)
        return idx, idx + len(needle)

    imp_s, imp_e = _span('import { Foo } from "./foo";')
    cmt_s, cmt_e = _span("// class CommentDecoy {}")
    cls_name_s, cls_name_e = _span("Widget")
    meth_name_s, meth_name_e = _span("render")
    foo_s, foo_e = _span("Foo(ctx)")
    cls_s, _cls_e = _span("class Widget")
    fn_name_s, fn_name_e = _span("helper")
    fn_s, _fn_e = _span("function helper")

    cls_ident = _FakeNode("identifier", cls_name_s, cls_name_e, (2, 6), [], {})
    meth_ident = _FakeNode("identifier", meth_name_s, meth_name_e, (3, 2), [], {})
    call_ident = _FakeNode("identifier", foo_s, foo_s + 3, (3, 25), [], {})
    str_node = _FakeNode("string", imp_s, imp_e, (0, 0), [], {})
    cmt_node = _FakeNode("comment", cmt_s, cmt_e, (1, 0), [], {})
    imp_node = _FakeNode("import_statement", imp_s, imp_e, (0, 0), [str_node], {})
    meth_node = _FakeNode(
        "method_definition",
        meth_name_s,
        foo_e + 3,
        (3, 2),
        [call_ident],
        {"name": meth_ident},
    )
    cls_node = _FakeNode(
        "class_declaration",
        cls_s,
        fn_s - 1,
        (2, 0),
        [meth_node],
        {"name": cls_ident},
    )
    fn_ident = _FakeNode("identifier", fn_name_s, fn_name_e, (5, 9), [], {})
    fn_node = _FakeNode(
        "function_declaration",
        fn_s,
        len(src),
        (5, 0),
        [],
        {"name": fn_ident},
    )
    root = _FakeNode(
        "program",
        0,
        len(src),
        (0, 0),
        [imp_node, cmt_node, cls_node, fn_node],
        {},
    )

    syms = extract_treesitter_file_symbols(
        "src/widget.ts",
        "ts",
        src,
        parser_factory=lambda _g: _FakeTSParser(_FakeTree(root)),
    )
    assert syms is not None
    by_name = {d.name: d for d in syms.definitions}
    assert "CommentDecoy" not in by_name
    assert by_name["Widget"].kind == "class"
    assert by_name["render"].kind == "method"
    assert by_name["render"].parent == "Widget"
    assert by_name["helper"].kind == "function"
    assert by_name["helper"].parent is None
    assert "./foo" in syms.imports
    assert "Foo" in syms.references

    assert (
        extract_treesitter_file_symbols("x.txt", "unknown", "hi", parser_factory=lambda _g: None)
        is None
    )


def test_polyglot_repo_map_extracts_new_languages_parents_and_strips_decoys(
    tmp_path: Path,
) -> None:
    (tmp_path / "TokenVault.sol").write_text(
        'import "./Ownable.sol";\n'
        "// contract CommentedOutContract {}\n"
        "contract TokenVault {\n"
        "    function deposit(uint256 amount) public {\n"
        "        // nothing\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "OrderService.cs").write_text(
        "using System.Collections;\n"
        "public class OrderService {\n"
        "    public void Submit(string id) {\n"
        "        TokenVault x;\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "deploy.sh").write_text(
        "#!/bin/sh\n# ignored_fn() {}\nrun_migrations() {\n  echo ok\n}\n",
        encoding="utf-8",
    )

    repo_map = build_repo_map(tmp_path, task="Inspect TokenVault deposit and OrderService")
    assert repo_map.total_files_scanned == 3
    assert "CommentedOutContract" not in repo_map.rendered
    assert "ignored_fn" not in repo_map.rendered
    assert "class TokenVault" in repo_map.rendered
    assert "    function deposit(uint256 amount)" in repo_map.rendered
    assert "class OrderService" in repo_map.rendered
    assert "    function Submit(string id)" in repo_map.rendered
    assert "function run_migrations()" in repo_map.rendered


@pytest.mark.asyncio
async def test_post_edit_diagnostics_catches_python_shell_json_and_skips_missing(
    tmp_path: Path,
) -> None:
    sb = _DiagSubprocessSandbox()
    await sb.start(str(tmp_path), SandboxConfig())

    (tmp_path / "broken.py").write_text("def oops(:\n    pass\n", encoding="utf-8")
    py_rep = await run_post_edit_diagnostics(["/workspace/broken.py"], sb)
    assert py_rep is not None
    assert py_rep.has_errors is True
    formatted = py_rep.format_for_tool_output()
    assert "[POST-EDIT DIAGNOSTICS (py_compile+ruff)]" in formatted
    assert "broken.py:1" in formatted

    (tmp_path / "bad.sh").write_text("if [ -f x ]; then\n  echo hi\n", encoding="utf-8")
    sh_rep = await run_post_edit_diagnostics(["/workspace/bad.sh"], sb)
    assert sh_rep is not None
    assert sh_rep.has_errors is True
    assert sh_rep.checker == "sh-n"

    (tmp_path / "bad.json").write_text('{"a": }\n', encoding="utf-8")
    json_rep = await run_post_edit_diagnostics(["/workspace/bad.json"], sb)
    assert json_rep is not None
    assert json_rep.has_errors is True

    parsed = parse_diagnostic_output(
        "ruff",
        "/workspace/mod.py:4:12: F821 Undefined name `missing_var`\n",
    )
    assert parsed[0].path == "mod.py"
    assert parsed[0].line == 4
    assert parsed[0].column == 12
    assert parsed[0].code == "F821"

    assert select_diagnostic_command(["notes.md"]) is None
    assert (
        extract_edited_paths(
            "apply_patch",
            {},
            ToolResult(output="ok", metadata={"changed_files": ["a.py"], "dry_run": True}),
        )
        == []
    )


@pytest.mark.asyncio
async def test_react_loop_post_edit_diagnostics_feedback_and_opt_out(tmp_path: Path) -> None:
    tools = ToolRegistry()
    tools.register(ReadFileTool())
    tools.register(WriteFileTool())

    sb = _DiagSubprocessSandbox()
    await sb.start(str(tmp_path), SandboxConfig())

    # 1. Opt-out default runs zero diagnostic commands
    llm_off = _DiagLLM(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="off-1",
                        name="write_file",
                        arguments={"path": "/workspace/m.py", "content": "def bad(:\n"},
                    )
                ],
                usage=TokenUsage(10, 5, 15),
                model="diag-test",
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content="Wrote without diagnostics.",
                usage=TokenUsage(10, 5, 15),
                model="diag-test",
                finish_reason="stop",
            ),
        ]
    )
    run_off = AgentRun(
        task="Write without diag",
        repo_path=str(tmp_path),
        config=AgentConfig(post_edit_diagnostics=False, context_enabled=False),
    )
    await react_loop(run_off, llm_off, tools, sb, emit_lifecycle=False)
    assert run_off.state == RunState.COMPLETED
    assert not any("py_compile" in cmd for cmd in sb.exec_commands)

    # 2. Enabled loop flags broken edit immediately, letting turn 2 self-correct
    llm_on = _DiagLLM(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="on-1",
                        name="write_file",
                        arguments={
                            "path": "/workspace/app.py",
                            "content": "def ok(:\n    return 1\n",
                        },
                    )
                ],
                usage=TokenUsage(10, 5, 15),
                model="diag-test",
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="on-2",
                        name="write_file",
                        arguments={
                            "path": "/workspace/app.py",
                            "content": "def ok() -> int:\n    return 1\n",
                        },
                    )
                ],
                usage=TokenUsage(10, 5, 15),
                model="diag-test",
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content="Fixed syntax error after diagnostics.",
                usage=TokenUsage(10, 5, 15),
                model="diag-test",
                finish_reason="stop",
            ),
        ]
    )
    run_on = AgentRun(
        task="Write with diagnostics",
        repo_path=str(tmp_path),
        config=AgentConfig(post_edit_diagnostics=True, context_enabled=False),
    )
    await react_loop(run_on, llm_on, tools, sb, emit_lifecycle=False)
    assert run_on.state == RunState.COMPLETED
    assert any("[POST-EDIT DIAGNOSTICS" in text for text in llm_on.seen_tool_outputs)
    assert run_on.tool_invocations[-1].result.metadata["diagnostics"]["errors"] == 0


def test_diagnostics_config_env_and_cli_flags(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AGENT_FORGE_AGENT_POST_EDIT_DIAGNOSTICS", "true")
    cfg = load_config(project_path=tmp_path)
    assert cfg.agent.post_edit_diagnostics is True

    overrides = _build_cli_overrides(
        model="gemini-3.1-flash-lite-preview",
        provider="gemini",
        max_iterations=10,
        diagnostics=True,
    )
    assert overrides is not None
    assert overrides["agent.post_edit_diagnostics"] is True

    agent_cfg = _build_agent_config(cfg, "gemini")
    assert agent_cfg.post_edit_diagnostics is True
