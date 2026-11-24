"""Agent Client Protocol (ACP) JSON-RPC 2.0 message helpers and event mappers."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from agent_forge import __version__
from agent_forge.agent.models import RunState
from agent_forge.config import MCPServerSettings
from agent_forge.llm.base import Role

if TYPE_CHECKING:
    from agent_forge.agent.session import DevSession
    from agent_forge.orchestration.event_stream import StreamEvent

ACP_PROTOCOL_VERSION = 1
AGENT_INFO: dict[str, str] = {
    "name": "agent-forge",
    "title": "Agent Forge",
    "version": __version__,
}

JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603

_TOOL_KINDS: dict[str, str] = {
    "read_file": "read",
    "list_directory": "search",
    "search_codebase": "search",
    "git_diff": "read",
    "write_file": "edit",
    "edit_file": "edit",
    "apply_patch": "edit",
    "run_shell": "execute",
    "shell_start": "execute",
    "shell_poll": "execute",
    "shell_write": "execute",
    "shell_cancel": "execute",
    "git_commit": "execute",
    "git_create_branch": "execute",
    "create_pr": "execute",
    "search_tools": "search",
    "execute_code": "execute",
    "spawn_subagent": "other",
}


def make_response(req_id: int | str | None, result: dict[str, Any]) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 success response object."""
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def make_error(
    req_id: int | str | None,
    code: int,
    message: str,
    data: Any = None,
) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 error response object."""
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def make_notification(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 notification object (no ``id``)."""
    return {"jsonrpc": "2.0", "method": method, "params": params}


def make_request(req_id: int | str, method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Build an outbound JSON-RPC 2.0 request object."""
    return {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}


_READ_TOKENS = frozenset({"read", "get", "list", "search", "find", "fetch", "inspect", "view"})
_EDIT_TOKENS = frozenset({"write", "edit", "patch", "delete", "create", "update", "remove"})
_EXEC_TOKENS = frozenset({"shell", "exec", "execute", "run"})


def classify_tool_kind(tool_name: str) -> str:
    """Map an Agent Forge or MCP tool name to an ACP tool kind label."""
    if tool_name in _TOOL_KINDS:
        return _TOOL_KINDS[tool_name]
    tokens = {tok for tok in re.split(r"[^a-z0-9]+", tool_name.lower()) if tok}
    if tokens & _READ_TOKENS:
        return "read"
    if tokens & _EDIT_TOKENS:
        return "edit"
    if tokens & _EXEC_TOKENS:
        return "execute"
    return "other"


def _extract_prompt_block(block: Any) -> str:
    if isinstance(block, str):
        return block
    if not isinstance(block, dict):
        return ""
    btype = str(block.get("type", ""))
    if btype == "text":
        return str(block.get("text", ""))
    if btype == "resource":
        res = block.get("resource")
        if isinstance(res, dict):
            uri = str(res.get("uri", ""))
            text = str(res.get("text", ""))
            return f"[{uri}]\n{text}" if uri and text else text
    return ""


def extract_prompt_text(prompt_blocks: Any) -> str:
    """Extract plain prompt text from an ACP ``prompt`` content block array."""
    if isinstance(prompt_blocks, str):
        return prompt_blocks.strip()
    if not isinstance(prompt_blocks, list):
        return ""
    parts = [_extract_prompt_block(block) for block in prompt_blocks]
    return "\n\n".join(part for part in parts if part.strip()).strip()


def _kv_list_to_dict(raw_items: Any) -> dict[str, str]:
    if isinstance(raw_items, dict):
        return {str(k): str(v) for k, v in raw_items.items()}
    if not isinstance(raw_items, list):
        return {}
    out: dict[str, str] = {}
    for entry in raw_items:
        if isinstance(entry, dict) and "name" in entry:
            out[str(entry["name"])] = str(entry.get("value", ""))
    return out


def translate_acp_mcp_servers(raw_servers: Any) -> dict[str, MCPServerSettings]:
    """Convert ACP ``session/new`` ``mcpServers`` entries into MCPServerSettings."""
    if not isinstance(raw_servers, list):
        return {}
    servers: dict[str, MCPServerSettings] = {}
    for index, entry in enumerate(raw_servers):
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or f"acp-mcp-{index}").strip()
        if not name:
            continue
        srv_type = str(entry.get("type", "stdio")).strip().lower()
        if srv_type in ("http", "sse"):
            url = str(entry.get("url", "")).strip()
            if url:
                servers[name] = MCPServerSettings(
                    transport="http",
                    url=url,
                    headers=_kv_list_to_dict(entry.get("headers")),
                )
        else:
            command = str(entry.get("command", "")).strip()
            if command:
                raw_args = entry.get("args", [])
                args = [str(item) for item in raw_args] if isinstance(raw_args, list) else []
                servers[name] = MCPServerSettings(
                    transport="stdio",
                    command=command,
                    args=args,
                    env=_kv_list_to_dict(entry.get("env")),
                )
    return servers


def map_run_state_to_stop_reason(state: RunState) -> str:
    """Translate terminal :class:`RunState` to an ACP ``stopReason`` string."""
    if state == RunState.CANCELLED:
        return "cancelled"
    if state == RunState.TIMEOUT:
        return "max_turn_requests"
    return "end_turn"


def _tool_output_text(data: dict[str, Any]) -> str:
    output = str(data.get("output", "") or "")
    error = str(data.get("error", "") or "")
    if error and output:
        return f"{output}\n[error] {error}"
    return error or output


def _update_payload(session_id: str, update: dict[str, Any]) -> dict[str, Any]:
    return {"sessionId": session_id, "update": update}


def map_stream_event_to_acp_updates(
    event: StreamEvent,
    session_id: str,
    *,
    tool_call_id: str | None = None,
) -> list[dict[str, Any]]:
    """Map one typed :class:`StreamEvent` onto ACP ``session/update`` notifications."""
    data = event.data
    etype = event.type
    call_id = tool_call_id or f"call-{event.seq}"

    if etype == "tool.called":
        tool_name = str(data.get("tool_name", "tool"))
        title = f"[code-mode] {tool_name}" if data.get("code_mode") else tool_name
        return [
            _update_payload(
                session_id,
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": call_id,
                    "title": title,
                    "kind": classify_tool_kind(tool_name),
                    "status": "in_progress",
                    "rawInput": data.get("arguments", {}),
                },
            )
        ]

    if etype == "tool.completed":
        exit_code = int(data.get("exit_code", 0))
        text = _tool_output_text(data)
        update: dict[str, Any] = {
            "sessionUpdate": "tool_call_update",
            "toolCallId": call_id,
            "status": "completed" if exit_code == 0 else "failed",
            "rawOutput": {
                "exit_code": exit_code,
                "output": data.get("output", ""),
                "error": data.get("error"),
            },
        }
        if text:
            update["content"] = [{"type": "content", "content": {"type": "text", "text": text}}]
        return [_update_payload(session_id, update)]

    if etype == "patch.applied":
        changed = data.get("changed_files", [])
        preview = str(data.get("diff_preview", "") or "")
        summary = preview or f"Modified files: {', '.join(str(p) for p in changed)}"
        return [
            _update_payload(
                session_id,
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": call_id,
                    "status": "completed",
                    "content": [{"type": "content", "content": {"type": "text", "text": summary}}],
                },
            )
        ]

    return _map_thought_or_terminal_event(event, session_id)


def _map_thought_or_terminal_event(
    event: StreamEvent,
    session_id: str,
) -> list[dict[str, Any]]:
    data = event.data
    etype = event.type
    if etype == "iteration.started":
        it = data.get("iteration", 0)
        max_it = data.get("max_iterations", "?")
        text = f"Iteration {it}/{max_it}"
        return [
            _update_payload(
                session_id,
                {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": text}},
            )
        ]
    if etype == "approval.requested":
        prefix = "[code-mode] " if data.get("code_mode") else ""
        text = (
            f"{prefix}Approval requested ({data.get('category', 'policy')}) "
            f"for {data.get('tool_name', 'tool')}: {data.get('reason', '')}"
        )
        return [
            _update_payload(
                session_id,
                {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": text}},
            )
        ]
    if etype == "approval.decided":
        prefix = "[code-mode] " if data.get("code_mode") else ""
        verdict = "approved" if data.get("approved") else "denied"
        text = (
            f"{prefix}Approval {verdict} ({data.get('decided_by', 'policy')}) "
            f"for {data.get('tool_name', 'tool')}"
        )
        return [
            _update_payload(
                session_id,
                {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": text}},
            )
        ]
    if etype == "subagent.started":
        sid = str(data.get("subagent_id", "sub"))
        mode = str(data.get("mode", "research"))
        task = str(data.get("task", ""))
        text = f"Subagent {sid} started ({mode}): {task}"
        return [
            _update_payload(
                session_id,
                {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": text}},
            )
        ]
    if etype == "subagent.completed":
        sid = str(data.get("subagent_id", "sub"))
        state = str(data.get("state", "completed"))
        mode = str(data.get("mode", "research"))
        tokens = int(data.get("total_tokens", 0) or 0)
        branch = data.get("branch")
        branch_note = f", branch={branch}" if branch else ""
        text = f"Subagent {sid} {state} ({mode}, {tokens:,} tokens{branch_note})"
        return [
            _update_payload(
                session_id,
                {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": text}},
            )
        ]
    if etype in ("run.failed", "run.cancelled"):
        detail = str(data.get("error") or data.get("reason") or etype)
        return [
            _update_payload(
                session_id,
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": detail},
                },
            )
        ]
    return []


def _replay_turn_tool_updates(
    dev_session: DevSession,
    start_idx: int,
    end_idx: int,
) -> list[dict[str, Any]]:
    updates: list[dict[str, Any]] = []
    turn_messages = dev_session.messages[start_idx:end_idx]
    tool_outputs: dict[str, str] = {
        msg.tool_call_id: msg.content
        for msg in turn_messages
        if msg.role == Role.TOOL and msg.tool_call_id
    }
    for msg in turn_messages:
        if msg.role != Role.ASSISTANT or not msg.tool_calls:
            continue
        for tc in msg.tool_calls:
            updates.append(
                _update_payload(
                    dev_session.id,
                    {
                        "sessionUpdate": "tool_call",
                        "toolCallId": tc.id,
                        "title": tc.name,
                        "kind": classify_tool_kind(tc.name),
                        "status": "in_progress",
                        "rawInput": tc.arguments,
                    },
                )
            )
            if tc.id in tool_outputs:
                text = tool_outputs[tc.id]
                is_err = text.startswith("[ERROR]") or "\n[ERROR] " in text
                tool_upd: dict[str, Any] = {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": tc.id,
                    "status": "failed" if is_err else "completed",
                }
                if text:
                    tool_upd["content"] = [
                        {"type": "content", "content": {"type": "text", "text": text}}
                    ]
                updates.append(_update_payload(dev_session.id, tool_upd))
    return updates


def build_replay_updates(dev_session: DevSession) -> list[dict[str, Any]]:
    """Build ordered ACP ``session/update`` payloads replaying stored turns on ``session/load``."""
    updates: list[dict[str, Any]] = []
    for turn in dev_session.turns:
        updates.append(
            _update_payload(
                dev_session.id,
                {
                    "sessionUpdate": "user_message_chunk",
                    "content": {"type": "text", "text": turn.prompt},
                },
            )
        )
        updates.extend(
            _replay_turn_tool_updates(
                dev_session,
                turn.message_start_index,
                turn.message_end_index,
            )
        )
        if turn.response:
            updates.append(
                _update_payload(
                    dev_session.id,
                    {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": turn.response},
                    },
                )
            )
    return updates
