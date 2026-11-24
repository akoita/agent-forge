"""Tests for opt-in Code Mode programmatic tool execution & host RPC bridge."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest

from agent_forge.agent.approvals import ApprovalGate, ApprovalRequest
from agent_forge.agent.code_mode import (
    CodeModeLimits,
    build_code_mode_registry,
    search_tool_definitions,
)
from agent_forge.agent.core import react_loop
from agent_forge.agent.journal import RunJournal, replay_journal
from agent_forge.agent.models import AgentConfig, AgentRun, RunState
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
from agent_forge.orchestration.events import Event, EventBus, EventType
from agent_forge.sandbox.base import ExecResult, Sandbox, SandboxConfig, sanitize_sandbox_env
from agent_forge.tools import Tool, ToolRegistry, ToolResult, create_default_registry
from agent_forge.tools.code_mode import SearchToolsTool

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


@dataclass
class _TurnSpec:
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tokens: tuple[int, int] = (20, 10)
    finish_reason: str = "stop"


class _CapturingLLM(LLMProvider):
    def __init__(self, turns: list[_TurnSpec]) -> None:
        self._turns = turns
        self.calls = 0
        self.seen_tool_names: list[list[str]] = []

    @property
    def model_name(self) -> str:
        return "code-mode-test"

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: LLMConfig | None = None,
    ) -> LLMResponse:
        del messages, config
        self.seen_tool_names.append([td.name for td in (tools or [])])
        idx = min(self.calls, len(self._turns) - 1)
        self.calls += 1
        spec = self._turns[idx]
        pt, ct = spec.tokens
        return LLMResponse(
            content=spec.content,
            tool_calls=list(spec.tool_calls or []),
            usage=TokenUsage(prompt_tokens=pt, completion_tokens=ct, total_tokens=pt + ct),
            model=self.model_name,
            finish_reason=spec.finish_reason,
        )

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: LLMConfig | None = None,
    ) -> AsyncIterator[LLMResponse]:
        del messages, tools, config
        if False:  # pragma: no cover
            yield LLMResponse(content=None)


class _SubprocessCodeSandbox(Sandbox):
    """Local subprocess sandbox with sanitized env (zero host secrets) for Python runs."""

    def __init__(self) -> None:
        self.root: Path | None = None
        self.stopped = False

    def _resolve(self, path: str) -> Path:
        assert self.root is not None
        rel = path.removeprefix("/workspace/").lstrip("/")
        return self.root if rel in {"", "."} else self.root / rel

    async def start(self, repo_path: str, config: SandboxConfig) -> None:
        from pathlib import Path

        del config
        self.root = Path(repo_path)

    async def exec(self, command: str, *, timeout_seconds: int = 30) -> ExecResult:
        assert self.root is not None
        clean_env = sanitize_sandbox_env(dict(os.environ))
        mapped_cmd = command.replace("/workspace", str(self.root))
        proc = await asyncio.create_subprocess_exec(
            "/bin/sh",
            "-c",
            mapped_cmd,
            cwd=str(self.root),
            env=clean_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out_b, err_b = await asyncio.wait_for(
                proc.communicate(), timeout=float(timeout_seconds)
            )
            return ExecResult(
                exit_code=int(proc.returncode or 0),
                stdout=out_b.decode("utf-8", errors="replace"),
                stderr=err_b.decode("utf-8", errors="replace"),
            )
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            return ExecResult(
                exit_code=124,
                stdout="",
                stderr=f"Command timed out after {timeout_seconds}s",
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
        self.stopped = True

    async def is_alive(self) -> bool:
        return self.root is not None and not self.stopped


class _SyntheticTool(Tool):
    """Configurable catalog tool simulating native or MCP-backed tools."""

    def __init__(
        self,
        tool_name: str,
        tool_desc: str,
        *,
        is_mcp: bool = False,
        approval_mode: str = "inherit",
        side_effect: bool = False,
    ) -> None:
        self._name = tool_name
        self._desc = tool_desc
        self.is_mcp_tool = is_mcp
        self.approval_mode = approval_mode
        self._side_effect = side_effect
        self.executions = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._desc

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Target resource identifier"}
            },
            "required": ["target"],
        }

    @property
    def side_effecting(self) -> bool:
        return self._side_effect

    async def execute(self, arguments: dict[str, Any], sandbox: Sandbox) -> ToolResult:
        del sandbox
        self.executions += 1
        return ToolResult(output=f"{self._name}:{arguments.get('target', '')}", exit_code=0)


def _build_large_registry() -> tuple[ToolRegistry, list[_SyntheticTool]]:
    reg = create_default_registry(load_plugins=False)
    synthetic: list[_SyntheticTool] = []
    for idx in range(12):
        mcp_tool = _SyntheticTool(
            f"mcp__service{idx}__fetch_item",
            f"Fetch metadata item {idx} from external catalog",
            is_mcp=True,
            approval_mode="prompt" if idx == 0 else "inherit",
            side_effect=(idx == 0),
        )
        reg.register(mcp_tool)
        synthetic.append(mcp_tool)
    return reg, synthetic


def test_search_tool_definitions_ranks_and_excludes_wrapper_tools() -> None:
    reg, _ = _build_large_registry()
    assert len(reg) >= 28

    hits = search_tool_definitions(reg, "service0 fetch", limit=5)
    assert hits
    assert hits[0]["name"] == "mcp__service0__fetch_item"
    assert "parameters" in hits[0]

    names_only = search_tool_definitions(reg, "", limit=15, include_schema=False)
    assert len(names_only) == 15
    assert all("parameters" not in item for item in names_only)
    assert all(item["name"] not in {"search_tools", "execute_code"} for item in names_only)


@pytest.mark.asyncio
async def test_execute_code_multi_step_loop_strip_secrets_and_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "super-secret-host-key")
    monkeypatch.setenv("MCP_SERVICE_TOKEN", "secret-mcp-token")
    for idx in range(3):
        (tmp_path / f"data_{idx}.txt").write_text(
            f"line1\nMATCH: item-{idx}\nline3\n",
            encoding="utf-8",
        )

    reg, _ = _build_large_registry()
    sb = _SubprocessCodeSandbox()
    await sb.start(str(tmp_path), SandboxConfig())

    script = (
        "import os\n"
        "assert os.environ.get('GEMINI_API_KEY') is None, 'key leaked'\n"
        "assert os.environ.get('MCP_SERVICE_TOKEN') is None, 'token leaked'\n"
        "assert 'read_file' in list_tools()\n"
        "matches = []\n"
        "for i in range(3):\n"
        "    res = call_tool('read_file', {'path': f'/workspace/data_{i}.txt'})\n"
        "    for line in res['output'].splitlines():\n"
        "        if 'MATCH:' in line:\n"
        "            matches.append(line.strip())\n"
        "write_res = call_tool('write_file', {\n"
        "    'path': '/workspace/summary.txt',\n"
        "    'content': '\\n'.join(matches) + '\\n',\n"
        "})\n"
        "assert write_res['exit_code'] == 0\n"
        "print('FILTERED:', ','.join(matches))\n"
    )

    llm = _CapturingLLM(
        [
            _TurnSpec(
                tool_calls=[
                    ToolCall(
                        id="c-search",
                        name="search_tools",
                        arguments={"query": "read_file"},
                    )
                ],
                finish_reason="tool_calls",
            ),
            _TurnSpec(
                tool_calls=[
                    ToolCall(
                        id="c-exec",
                        name="execute_code",
                        arguments={"code": script},
                    )
                ],
                finish_reason="tool_calls",
            ),
            _TurnSpec(content="Processed all 3 files cleanly.", finish_reason="stop"),
        ]
    )

    journal_path = tmp_path / "journal.jsonl"
    run = AgentRun(
        task="Summarize matching lines",
        repo_path=str(tmp_path),
        config=AgentConfig(execution_mode="code_mode", context_enabled=False),
    )
    await react_loop(
        run,
        llm,
        reg,
        sb,
        emit_lifecycle=False,
        journal=RunJournal(run.id, path=journal_path, fsync=False),
    )

    assert run.state == RunState.COMPLETED
    assert run.tool_invocations[-1].tool_name == "execute_code"
    assert run.tool_invocations[-1].result.exit_code == 0, run.tool_invocations[-1].result.error
    assert llm.seen_tool_names[0] == ["search_tools", "execute_code"]
    assert (tmp_path / "summary.txt").read_text(encoding="utf-8") == (
        "MATCH: item-0\nMATCH: item-1\nMATCH: item-2\n"
    )
    assert "FILTERED: MATCH: item-0,MATCH: item-1,MATCH: item-2" in (
        run.tool_invocations[-1].result.output
    )
    # Zero leftover RPC scratch directories
    assert list(tmp_path.glob(".agent_forge_rpc_*")) == []
    # Full journal validates cleanly with zero pending/duplicate side effects
    replay = replay_journal(journal_path)
    assert replay.valid is True
    assert replay.duplicate_side_effects == []


@pytest.mark.asyncio
async def test_code_mode_approval_gate_deny_prompt_and_mcp_parity(tmp_path: Path) -> None:
    reg, synthetic = _build_large_registry()
    sb = _SubprocessCodeSandbox()
    await sb.start(str(tmp_path), SandboxConfig())

    bus = EventBus()
    approval_events: list[Event] = []

    async def _collect(ev: Event) -> None:
        approval_events.append(ev)

    await bus.subscribe(EventType.APPROVAL_REQUESTED, _collect)
    await bus.subscribe(EventType.APPROVAL_DECIDED, _collect)

    # 1. Deny mode rejects destructive rm inside script
    deny_run = AgentRun(
        task="Block destructive shell",
        repo_path=str(tmp_path),
        config=AgentConfig(execution_mode="code_mode", approval_mode="deny", context_enabled=False),
    )
    _, deny_bridge = build_code_mode_registry(
        reg,
        run=deny_run,
        sandbox=sb,
        journal=RunJournal(deny_run.id, path=tmp_path / "deny.jsonl", fsync=False),
        approval_gate=ApprovalGate(mode="deny"),
        event_bus=bus,
    )
    deny_script = (
        "import json\n"
        "res = call_tool('run_shell', {'command': 'rm -rf /tmp/nonexistent'})\n"
        "print(json.dumps(res))\n"
    )
    deny_res = await deny_bridge.execute_script(deny_script, parent_call_id="p-deny")
    assert deny_res.exit_code == 0
    payload = json.loads(deny_res.output)
    assert payload["exit_code"] == 1
    assert payload["metadata"]["approval_denied"] is True

    # 2. Interactive prompt callback runs mid-script for both shell & MCP tool
    prompted_tools: list[str] = []

    async def _callback(req: ApprovalRequest) -> bool:
        prompted_tools.append(req.tool_name)
        return req.tool_name == "mcp__service0__fetch_item"

    prompt_run = AgentRun(
        task="Prompt mid script",
        repo_path=str(tmp_path),
        config=AgentConfig(
            execution_mode="code_mode",
            approval_mode="prompt",
            context_enabled=False,
        ),
    )
    _, prompt_bridge = build_code_mode_registry(
        reg,
        run=prompt_run,
        sandbox=sb,
        journal=RunJournal(prompt_run.id, path=tmp_path / "prompt.jsonl", fsync=False),
        approval_gate=ApprovalGate(mode="prompt", callback=_callback),
        event_bus=bus,
    )
    prompt_script = (
        "import json\n"
        "r_mcp = call_tool('mcp__service0__fetch_item', {'target': 'doc-1'})\n"
        "r_rm = call_tool('run_shell', {'command': 'rm -rf /tmp/nope'})\n"
        "print(json.dumps({'mcp': r_mcp, 'rm': r_rm}))\n"
    )
    prompt_res = await prompt_bridge.execute_script(prompt_script, parent_call_id="p-prompt")
    assert prompt_res.exit_code == 0
    outcomes = json.loads(prompt_res.output)
    assert outcomes["mcp"]["exit_code"] == 0
    assert synthetic[0].executions == 1
    assert outcomes["rm"]["exit_code"] == 1
    assert outcomes["rm"]["metadata"]["approval_denied"] is True
    assert prompted_tools == ["mcp__service0__fetch_item", "run_shell"]
    assert len(approval_events) >= 4


@pytest.mark.asyncio
async def test_code_mode_idempotency_guards_and_crash_cleanup(tmp_path: Path) -> None:
    reg, synthetic = _build_large_registry()
    sb = _SubprocessCodeSandbox()
    await sb.start(str(tmp_path), SandboxConfig())

    journal_path = tmp_path / "idem.jsonl"
    run = AgentRun(
        task="Idempotent bridge test",
        repo_path=str(tmp_path),
        config=AgentConfig(execution_mode="code_mode", context_enabled=False),
    )
    _, bridge = build_code_mode_registry(
        reg,
        run=run,
        sandbox=sb,
        journal=RunJournal(run.id, path=journal_path, fsync=False),
        limits=CodeModeLimits(timeout_seconds=1, max_bridged_calls=2),
    )

    first = await bridge.invoke(
        "mcp__service1__fetch_item",
        {"target": "x"},
        parent_call_id="p1",
        seq=1,
    )
    reused = await bridge.invoke(
        "mcp__service1__fetch_item",
        {"target": "x"},
        parent_call_id="p1",
        seq=1,
    )
    assert first.output == "mcp__service1__fetch_item:x"
    assert reused.output == first.output
    assert synthetic[1].executions == 1

    # Third call exceeds max_bridged_calls=2
    over_limit = await bridge.invoke(
        "read_file", {"path": "/workspace/a"}, parent_call_id="p1", seq=3
    )
    assert over_limit.exit_code == 1
    assert "limit (2) exceeded" in (over_limit.error or "")

    # Recursive invocation blocked
    recursive = await bridge.invoke(
        "execute_code", {"code": "print(1)"}, parent_call_id="p1", seq=4
    )
    assert recursive.exit_code == 1
    assert "cannot be invoked from inside execute_code" in (recursive.error or "")

    # Timeout & script exception clean up RPC scratch directories completely
    timed_out = await bridge.execute_script("import time\ntime.sleep(5)", parent_call_id="p-to")
    assert timed_out.exit_code == 124
    assert list(tmp_path.glob(".agent_forge_rpc_*")) == []

    search_tool = SearchToolsTool(reg)
    search_res = await search_tool.execute({"query": "fetch_item", "limit": 3}, sb)
    assert search_res.exit_code == 0
    assert json.loads(search_res.output)["count"] == 3


def test_code_mode_config_env_and_cli_flags(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AGENT_FORGE_AGENT_EXECUTION_MODE", "code_mode")
    cfg = load_config(project_path=tmp_path)
    assert cfg.agent.execution_mode == "code_mode"

    overrides = _build_cli_overrides(
        model="gemini-3.1-flash-lite-preview",
        provider="gemini",
        max_iterations=10,
        execution_mode="CODE_MODE",
    )
    assert overrides is not None
    assert overrides["agent.execution_mode"] == "code_mode"

    agent_cfg = _build_agent_config(cfg, "gemini")
    assert agent_cfg.execution_mode == "code_mode"
