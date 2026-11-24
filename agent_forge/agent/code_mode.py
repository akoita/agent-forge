"""Code Mode runtime: progressive tool lookup and credential-free host RPC bridge."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import shlex
import shutil
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_forge.agent.approvals import ApprovalGate
from agent_forge.agent.journal import compute_tool_idempotency_key, is_side_effecting_tool
from agent_forge.agent.models import ToolInvocation
from agent_forge.orchestration.events import Event, EventType
from agent_forge.tools.base import ToolRegistry, ToolResult

if TYPE_CHECKING:
    from agent_forge.agent.journal import RunJournal
    from agent_forge.agent.models import AgentRun
    from agent_forge.orchestration.events import EventBus
    from agent_forge.sandbox.base import Sandbox
    from agent_forge.tools.base import Tool

_BLOCKED_BRIDGED_TOOLS = frozenset({"execute_code", "search_tools"})
_REQ_FILENAME_RE = re.compile(r"^req_(\d+)\.json$")

_RUNNER_SCRIPT = '''"""Sandbox-side Code Mode runner forwarding call_tool() over local JSON RPC."""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path

_RPC_DIR = Path(__file__).resolve().parent
_REQ_SEQ = 0


def list_tools() -> list[str]:
    """Return available host-bridged tool names for this run."""
    catalog_path = _RPC_DIR / "tools.json"
    if not catalog_path.exists():
        return []
    return list(json.loads(catalog_path.read_text(encoding="utf-8")))


def call_tool(tool_name: str, arguments: dict | None = None) -> dict:
    """Invoke a host-bridged tool synchronously and return its structured response dict."""
    global _REQ_SEQ
    _REQ_SEQ += 1
    seq = _REQ_SEQ
    req_tmp = _RPC_DIR / f"req_{seq}.json.tmp"
    req_file = _RPC_DIR / f"req_{seq}.json"
    resp_file = _RPC_DIR / f"resp_{seq}.json"
    payload = {
        "seq": seq,
        "tool": str(tool_name),
        "arguments": dict(arguments or {}),
    }
    req_tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(req_tmp, req_file)
    while not resp_file.exists():
        time.sleep(0.005)
    raw = resp_file.read_text(encoding="utf-8")
    return dict(json.loads(raw))


def _main() -> int:
    user_code = (_RPC_DIR / "user_script.py").read_text(encoding="utf-8")
    namespace = {
        "__name__": "__main__",
        "call_tool": call_tool,
        "list_tools": list_tools,
    }
    try:
        exec(compile(user_code, "code_mode_script.py", "exec"), namespace)
        return 0
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else (0 if code is None else 1)
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
'''


@dataclass(frozen=True)
class CodeModeLimits:
    """Resource and safety bounds for sandboxed Code Mode script runs."""

    timeout_seconds: int = 30
    max_timeout_seconds: int = 120
    max_bridged_calls: int = 50
    max_output_chars: int = 16_000
    poll_interval_seconds: float = 0.005


def _collect_param_text(td: Any) -> str:
    props = td.parameters.get("properties", {}) if isinstance(td.parameters, dict) else {}
    if not isinstance(props, dict):
        return ""
    parts: list[str] = []
    for pname, pspec in props.items():
        parts.append(str(pname).lower())
        if isinstance(pspec, dict) and pspec.get("description"):
            parts.append(str(pspec["description"]).lower())
    return " ".join(parts)


def _score_tool_match(query_tokens: list[str], raw_query: str, td: Any) -> int:
    if not raw_query:
        return 1
    name_lower = td.name.lower()
    desc_lower = td.description.lower()
    param_text = _collect_param_text(td)

    score = 20 if raw_query in name_lower else (10 if raw_query in desc_lower else 0)
    for token in query_tokens:
        if token in name_lower:
            score += 8
        if token in desc_lower:
            score += 3
        if token in param_text:
            score += 2
    return score


def search_tool_definitions(
    registry: ToolRegistry,
    query: str,
    *,
    limit: int = 10,
    include_schema: bool = True,
) -> list[dict[str, Any]]:
    """Rank and return matching tool definitions from *registry* for progressive discovery."""
    bounded_limit = max(1, min(limit, 50))
    raw_query = query.strip().lower()
    tokens = [tok for tok in re.split(r"[\s_\-:./]+", raw_query) if tok]
    scored: list[tuple[int, str, dict[str, Any]]] = []
    for td in registry.list_definitions():
        if td.name in _BLOCKED_BRIDGED_TOOLS:
            continue
        score = _score_tool_match(tokens, raw_query, td)
        if score <= 0:
            continue
        entry: dict[str, Any] = {
            "name": td.name,
            "description": td.description,
        }
        if include_schema:
            entry["parameters"] = td.parameters
        scored.append((score, td.name, entry))

    scored.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in scored[:bounded_limit]]


def _truncate_output(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    suffix = "\n... [code output truncated]"
    return text[: max(0, max_chars - len(suffix))].rstrip() + suffix


def _format_exec_combined(stdout: str, stderr: str) -> str:
    out = stdout.rstrip("\n")
    err = stderr.rstrip("\n")
    if out and err:
        return f"{out}\n[stderr]\n{err}"
    return out or err


def _extract_journal_result(result: ToolResult) -> dict[str, Any]:
    return {
        "output": result.output,
        "error": result.error,
        "exit_code": result.exit_code,
        "execution_time_ms": result.execution_time_ms,
        "metadata": result.metadata,
    }


def _result_to_rpc_dict(result: ToolResult) -> dict[str, Any]:
    return {
        "output": result.output,
        "error": result.error,
        "exit_code": result.exit_code,
        "metadata": result.metadata,
    }


class HostToolBridge:
    """Credential-free host RPC bridge routing scripted tool calls through gates & journal."""

    def __init__(
        self,
        underlying_tools: ToolRegistry,
        *,
        run: AgentRun,
        sandbox: Sandbox,
        journal: RunJournal,
        completed_results_by_key: dict[str, ToolResult] | None = None,
        approval_gate: ApprovalGate | None = None,
        event_bus: EventBus | None = None,
        limits: CodeModeLimits | None = None,
    ) -> None:
        self.underlying_tools = underlying_tools
        self.run = run
        self.sandbox = sandbox
        self.journal = journal
        self.completed_results_by_key = (
            completed_results_by_key if completed_results_by_key is not None else {}
        )
        self.approval_gate = approval_gate
        self.event_bus = event_bus
        self.limits = limits or CodeModeLimits()
        self._bridged_calls_total = 0

    def set_completed_results(self, completed: dict[str, ToolResult]) -> None:
        """Bind reconciled idempotent results from ``RunJournal`` after resume preparation."""
        self.completed_results_by_key = completed

    async def _emit(self, event_type: EventType, data: dict[str, Any]) -> None:
        if self.event_bus is not None:
            await self.event_bus.publish(Event(type=event_type, run_id=self.run.id, data=data))

    async def _evaluate_approval(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        tool_obj: object,
    ) -> ToolResult | None:
        active_gate = self.approval_gate
        if active_gate is None:
            tool_mode = getattr(tool_obj, "approval_mode", "inherit")
            if isinstance(tool_mode, str) and tool_mode.strip().lower() in ("prompt", "deny"):
                active_gate = ApprovalGate(mode=tool_mode)
            else:
                return None

        request, decision = await active_gate.evaluate(tool_name, arguments, tool=tool_obj)
        if request is None or decision is None:
            return None

        await self._emit(
            EventType.APPROVAL_REQUESTED,
            {
                "tool_name": request.tool_name,
                "category": request.category,
                "reason": request.reason,
                "iteration": self.run.iterations,
                "code_mode": True,
            },
        )
        await self._emit(
            EventType.APPROVAL_DECIDED,
            {
                "tool_name": request.tool_name,
                "category": request.category,
                "approved": decision.approved,
                "decided_by": decision.decided_by,
                "reason": decision.reason,
                "iteration": self.run.iterations,
                "code_mode": True,
            },
        )
        if decision.approved:
            return None

        denial_msg = f"Approval denied ({request.category}): {request.reason}" + (
            f" — {decision.reason}" if decision.reason else ""
        )
        return ToolResult(
            output="",
            error=denial_msg,
            exit_code=1,
            metadata={
                "approval_denied": True,
                "category": request.category,
                "decided_by": decision.decided_by,
            },
        )

    def _record_bridged_outcome(
        self,
        *,
        bridge_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        idempotency_key: str,
        side_effecting: bool,
        reused: bool,
        timestamp: datetime,
        result: ToolResult,
    ) -> None:
        duration_ms = 0 if reused else result.execution_time_ms
        self.run.tool_invocations.append(
            ToolInvocation(
                tool_name=tool_name,
                arguments=arguments,
                result=result,
                iteration=self.run.iterations,
                timestamp=timestamp,
                duration_ms=duration_ms,
            )
        )
        self.journal.append(
            "action_result",
            {
                "tool_call_id": bridge_call_id,
                "idempotency_key": idempotency_key,
                "tool_name": tool_name,
                "arguments": arguments,
                "side_effecting": side_effecting,
                "reused": reused,
                "duration_ms": duration_ms,
                "result": _extract_journal_result(result),
            },
            iteration=self.run.iterations,
        )

    async def invoke(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        parent_call_id: str,
        seq: int,
    ) -> ToolResult:
        """Execute a single bridged host tool call through gates, journal, and events."""
        if tool_name in _BLOCKED_BRIDGED_TOOLS:
            return ToolResult(
                output="",
                error=f"Tool '{tool_name}' cannot be invoked from inside execute_code.",
                exit_code=1,
            )
        if self._bridged_calls_total >= self.limits.max_bridged_calls:
            return ToolResult(
                output="",
                error=(f"Code Mode bridged call limit ({self.limits.max_bridged_calls}) exceeded."),
                exit_code=1,
            )

        self._bridged_calls_total += 1
        bridge_call_id = f"{parent_call_id}:bridge:{seq}"
        idem_key = compute_tool_idempotency_key(
            self.run.id,
            self.run.iterations,
            bridge_call_id,
            tool_name,
            arguments,
            call_index=seq,
        )
        start_time = datetime.now(UTC)

        try:
            tool_obj: Tool = self.underlying_tools.get(tool_name)
        except KeyError:
            return ToolResult(output="", error=f"Unknown tool: '{tool_name}'", exit_code=1)

        side_effecting = is_side_effecting_tool(tool_name, tool_obj)
        await self._emit(
            EventType.TOOL_CALLED,
            {
                "tool_name": tool_name,
                "arguments": dict(arguments),
                "iteration": self.run.iterations,
                "code_mode": True,
            },
        )
        self.journal.append(
            "action_requested",
            {
                "tool_call_id": bridge_call_id,
                "idempotency_key": idem_key,
                "tool_name": tool_name,
                "arguments": dict(arguments),
                "side_effecting": side_effecting,
            },
            iteration=self.run.iterations,
        )

        if idem_key in self.completed_results_by_key:
            cached = self.completed_results_by_key[idem_key]
            self._record_bridged_outcome(
                bridge_call_id=bridge_call_id,
                tool_name=tool_name,
                arguments=dict(arguments),
                idempotency_key=idem_key,
                side_effecting=side_effecting,
                reused=True,
                timestamp=start_time,
                result=cached,
            )
            await self._emit(
                EventType.TOOL_COMPLETED,
                {
                    "tool_name": tool_name,
                    "duration_ms": 0,
                    "has_error": cached.error is not None,
                    "reused": True,
                    "code_mode": True,
                },
            )
            return cached

        denied = await self._evaluate_approval(tool_name, dict(arguments), tool_obj)
        if denied is not None:
            self._record_bridged_outcome(
                bridge_call_id=bridge_call_id,
                tool_name=tool_name,
                arguments=dict(arguments),
                idempotency_key=idem_key,
                side_effecting=side_effecting,
                reused=False,
                timestamp=start_time,
                result=denied,
            )
            await self._emit(
                EventType.TOOL_COMPLETED,
                {
                    "tool_name": tool_name,
                    "duration_ms": denied.execution_time_ms,
                    "has_error": True,
                    "reused": False,
                    "code_mode": True,
                },
            )
            return denied

        try:
            result = await tool_obj.execute(dict(arguments), self.sandbox)
        except (OSError, RuntimeError, ValueError, TypeError) as exc:
            result = ToolResult(output="", error=f"{type(exc).__name__}: {exc}", exit_code=1)

        self._record_bridged_outcome(
            bridge_call_id=bridge_call_id,
            tool_name=tool_name,
            arguments=dict(arguments),
            idempotency_key=idem_key,
            side_effecting=side_effecting,
            reused=False,
            timestamp=start_time,
            result=result,
        )
        self.completed_results_by_key[idem_key] = result
        await self._emit(
            EventType.TOOL_COMPLETED,
            {
                "tool_name": tool_name,
                "duration_ms": result.execution_time_ms,
                "has_error": result.error is not None,
                "reused": False,
                "code_mode": True,
            },
        )
        return result

    async def _watch_rpc_requests(
        self,
        rpc_dir: Path,
        *,
        parent_call_id: str,
        stop_event: asyncio.Event,
        bridged_tools: list[str],
    ) -> None:
        handled: set[int] = set()
        while not stop_event.is_set():
            pending_reqs = sorted(rpc_dir.glob("req_*.json"))
            for req_path in pending_reqs:
                match = _REQ_FILENAME_RE.match(req_path.name)
                if match is None:
                    continue
                seq = int(match.group(1))
                if seq in handled:
                    continue
                handled.add(seq)
                try:
                    payload = json.loads(req_path.read_text(encoding="utf-8"))
                    tool_name = str(payload.get("tool", ""))
                    raw_args = payload.get("arguments")
                    args = dict(raw_args) if isinstance(raw_args, dict) else {}
                    bridged_tools.append(tool_name)
                    res = await self.invoke(tool_name, args, parent_call_id=parent_call_id, seq=seq)
                except (json.JSONDecodeError, OSError, RuntimeError, ValueError, TypeError) as exc:
                    res = ToolResult(output="", error=f"RPC error: {exc}", exit_code=1)
                resp_tmp = rpc_dir / f"resp_{seq}.json.tmp"
                resp_file = rpc_dir / f"resp_{seq}.json"
                resp_tmp.write_text(json.dumps(_result_to_rpc_dict(res)), encoding="utf-8")
                resp_tmp.replace(resp_file)
            await asyncio.sleep(self.limits.poll_interval_seconds)

    async def execute_script(
        self,
        code: str,
        *,
        parent_call_id: str,
        timeout_seconds: int | None = None,
    ) -> ToolResult:
        """Run *code* in the sandbox while serving bridged tool RPCs on the host."""
        started = time.monotonic()
        if not code.strip():
            return ToolResult(output="", error="Missing required argument: code", exit_code=1)

        raw_timeout = timeout_seconds or self.limits.timeout_seconds
        effective_timeout = max(1, min(int(raw_timeout), self.limits.max_timeout_seconds))
        rpc_dir_name = f".agent_forge_rpc_{uuid.uuid4().hex[:8]}"
        rpc_dir = Path(self.run.repo_path) / rpc_dir_name
        rpc_dir.mkdir(parents=True, exist_ok=True)
        stop_event = asyncio.Event()
        bridged_tools: list[str] = []
        watcher: asyncio.Task[None] | None = None

        try:
            catalog = sorted(
                td.name
                for td in self.underlying_tools.list_definitions()
                if td.name not in _BLOCKED_BRIDGED_TOOLS
            )
            (rpc_dir / "tools.json").write_text(json.dumps(catalog), encoding="utf-8")
            (rpc_dir / "runner.py").write_text(_RUNNER_SCRIPT, encoding="utf-8")
            (rpc_dir / "user_script.py").write_text(code, encoding="utf-8")

            watcher = asyncio.create_task(
                self._watch_rpc_requests(
                    rpc_dir,
                    parent_call_id=parent_call_id,
                    stop_event=stop_event,
                    bridged_tools=bridged_tools,
                )
            )
            rel_runner = shlex.quote(f"{rpc_dir_name}/runner.py")
            ws_runner = shlex.quote(f"/workspace/{rpc_dir_name}/runner.py")
            cmd = f"if [ -f {ws_runner} ]; then python3 {ws_runner}; else python3 {rel_runner}; fi"
            exec_res = await self.sandbox.exec(cmd, timeout_seconds=effective_timeout)
        finally:
            stop_event.set()
            if watcher is not None:
                watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watcher
            shutil.rmtree(rpc_dir, ignore_errors=True)

        combined = _truncate_output(
            _format_exec_combined(exec_res.stdout, exec_res.stderr),
            self.limits.max_output_chars,
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        error_msg = (
            f"Script exited with code {exec_res.exit_code}: {exec_res.stderr.strip()}"
            if exec_res.exit_code != 0 and exec_res.stderr.strip()
            else (
                f"Script exited with code {exec_res.exit_code}" if exec_res.exit_code != 0 else None
            )
        )
        return ToolResult(
            output=combined,
            error=error_msg,
            exit_code=exec_res.exit_code,
            execution_time_ms=elapsed_ms,
            metadata={
                "code_mode": True,
                "bridged_calls": len(bridged_tools),
                "bridged_tools": bridged_tools,
                "exit_code": exec_res.exit_code,
            },
        )


def build_code_mode_registry(
    underlying_tools: ToolRegistry,
    *,
    run: AgentRun,
    sandbox: Sandbox,
    journal: RunJournal,
    completed_results_by_key: dict[str, ToolResult] | None = None,
    approval_gate: ApprovalGate | None = None,
    event_bus: EventBus | None = None,
    limits: CodeModeLimits | None = None,
) -> tuple[ToolRegistry, HostToolBridge]:
    """Build the minimal 2-tool registry (``search_tools``, ``execute_code``) + host bridge."""
    from agent_forge.tools.code_mode import ExecuteCodeTool, SearchToolsTool

    bridge = HostToolBridge(
        underlying_tools,
        run=run,
        sandbox=sandbox,
        journal=journal,
        completed_results_by_key=completed_results_by_key,
        approval_gate=approval_gate,
        event_bus=event_bus,
        limits=limits,
    )
    registry = ToolRegistry()
    registry.register(SearchToolsTool(underlying_tools))
    registry.register(ExecuteCodeTool(bridge))
    return registry, bridge
