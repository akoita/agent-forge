"""Agent Client Protocol (ACP) JSON-RPC 2.0 message helpers and event mappers."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from agent_forge import __version__
from agent_forge.agent.models import RunState
from agent_forge.config import ForgeConfig, MCPServerSettings
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

ACP_MODES: tuple[dict[str, str], ...] = (
    {
        "id": "code",
        "name": "Code (Prompted)",
        "description": "Standard tool-calling loop with interactive client permission checks.",
    },
    {
        "id": "code_mode",
        "name": "Code Mode",
        "description": "Programmatic sandbox orchestration via search_tools and execute_code.",
    },
    {
        "id": "auto",
        "name": "Autonomous",
        "description": "Run workspace actions automatically without interactive prompts.",
    },
    {
        "id": "ask",
        "name": "Ask / Read-Only",
        "description": "Deny mutating tool calls so edits require an explicit mode change.",
    },
)

ACP_MODE_IDS: frozenset[str] = frozenset(item["id"] for item in ACP_MODES)

ACP_AVAILABLE_COMMANDS: tuple[dict[str, Any], ...] = (
    {"name": "diff", "description": "Inspect workspace Git status and diff."},
    {"name": "undo", "description": "Roll back edits from the previous turn."},
    {
        "name": "memory",
        "description": "List, add, or clear entries in .agent-forge/memory.md.",
        "input": {"hint": "list | add <note> | clear"},
    },
    {
        "name": "apply",
        "description": "Integrate a subagent worker branch into the workspace.",
        "input": {"hint": "<branch>"},
    },
    {"name": "subagents", "description": "Inspect active or completed subagent runs."},
)

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


def build_agent_capabilities() -> dict[str, Any]:
    """Return ACP ``agentCapabilities`` advertised during ``initialize``."""
    return {
        "loadSession": True,
        "promptCapabilities": {
            "embeddedContext": True,
            "image": False,
            "audio": False,
        },
        "mcpCapabilities": {
            "http": True,
            "sse": True,
        },
        "sessionCapabilities": {
            "list": {},
        },
    }


def build_session_modes(current_mode_id: str) -> dict[str, Any]:
    """Build ACP ``SessionModeState`` payload for ``session/new`` and ``session/load``."""
    mode = current_mode_id if current_mode_id in ACP_MODE_IDS else "code"
    return {
        "availableModes": [dict(item) for item in ACP_MODES],
        "currentModeId": mode,
    }


def _model_select_options(cfg: ForgeConfig, current_model: str) -> list[dict[str, str]]:
    seen: set[str] = set()
    options: list[dict[str, str]] = []

    def _add(value: str, label: str, description: str = "") -> None:
        cleaned = value.strip()
        if not cleaned or cleaned in seen:
            return
        seen.add(cleaned)
        opt: dict[str, str] = {"value": cleaned, "name": label}
        if description:
            opt["description"] = description
        options.append(opt)

    _add(current_model, current_model, "Active session model")
    _add(cfg.agent.default_model, cfg.agent.default_model, "Configured default model")
    for prov_name, prov in cfg.providers.items():
        if prov.default_model:
            _add(prov.default_model, prov.default_model, f"Provider: {prov_name}")
    for profile_name, profile in cfg.model_profiles.items():
        if profile.model:
            _add(profile.model, f"{profile.model} ({profile_name})", profile.provider)
    return options


def build_session_config_options(
    *,
    mode_id: str,
    provider: str,
    model: str,
    approval_mode: str,
    execution_mode: str,
    config: ForgeConfig,
) -> list[dict[str, Any]]:
    """Build ACP ``SessionConfigOption`` descriptors reflecting current session settings."""
    provider_options = [{"value": name, "name": name} for name in config.providers]
    if provider and provider not in config.providers:
        provider_options.insert(0, {"value": provider, "name": provider})
    return [
        {
            "id": "mode",
            "name": "Mode",
            "description": "Execution and approval preset for this session.",
            "category": "mode",
            "type": "select",
            "currentValue": mode_id if mode_id in ACP_MODE_IDS else "code",
            "options": [
                {
                    "value": item["id"],
                    "name": item["name"],
                    "description": item["description"],
                }
                for item in ACP_MODES
            ],
        },
        {
            "id": "model",
            "name": "Model",
            "description": "LLM model identifier used for subsequent turns.",
            "category": "model",
            "type": "select",
            "currentValue": model,
            "options": _model_select_options(config, model),
        },
        {
            "id": "provider",
            "name": "Provider",
            "description": "Configured LLM provider adapter.",
            "category": "other",
            "type": "select",
            "currentValue": provider,
            "options": provider_options,
        },
        {
            "id": "approval_mode",
            "name": "Approvals",
            "description": "Policy for gated or side-effecting tool calls.",
            "category": "other",
            "type": "select",
            "currentValue": approval_mode,
            "options": [
                {
                    "value": "prompt",
                    "name": "Prompt",
                    "description": "Ask client before gated tools.",
                },
                {"value": "auto", "name": "Auto", "description": "Approve automatically."},
                {"value": "deny", "name": "Deny", "description": "Deny gated tools."},
            ],
        },
        {
            "id": "execution_mode",
            "name": "Execution",
            "description": "Standard tool-calling loop or sandboxed Code Mode.",
            "category": "other",
            "type": "select",
            "currentValue": execution_mode,
            "options": [
                {"value": "tool_calling", "name": "Tool Calling"},
                {"value": "code_mode", "name": "Code Mode"},
            ],
        },
    ]


def build_available_commands_update(session_id: str) -> dict[str, Any]:
    """Build an ACP ``available_commands_update`` notification payload."""
    commands = [dict(item) for item in ACP_AVAILABLE_COMMANDS]
    return _update_payload(
        session_id,
        {"sessionUpdate": "available_commands_update", "availableCommands": commands},
    )


def build_config_option_update(
    session_id: str,
    config_options: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build an ACP ``config_option_update`` notification payload."""
    return _update_payload(
        session_id,
        {"sessionUpdate": "config_option_update", "configOptions": config_options},
    )


def build_current_mode_update(session_id: str, mode_id: str) -> dict[str, Any]:
    """Build an ACP ``current_mode_update`` notification payload."""
    return _update_payload(
        session_id,
        {"sessionUpdate": "current_mode_update", "currentModeId": mode_id},
    )


def build_plan_update(session_id: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a normalized ACP ``plan`` notification payload."""
    normalized: list[dict[str, str]] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content", "")).strip()
        if not content:
            continue
        priority = str(item.get("priority", "medium")).lower()
        status = str(item.get("status", "pending")).lower()
        normalized.append(
            {
                "content": content,
                "priority": priority if priority in {"high", "medium", "low"} else "medium",
                "status": status
                if status in {"pending", "in_progress", "completed"}
                else "pending",
            }
        )
    return _update_payload(session_id, {"sessionUpdate": "plan", "entries": normalized})


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


def _extract_resource_link(block: dict[str, Any]) -> str:
    uri = str(block.get("uri", "")).strip()
    name = str(block.get("name", "")).strip()
    desc = str(block.get("description", "")).strip()
    header = f"[resource_link: {name or uri}]"
    if uri and name and uri != name:
        header = f"[resource_link: {name} ({uri})]"
    return f"{header}\n{desc}".strip() if desc else header


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
    if btype == "resource_link":
        return _extract_resource_link(block)
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


def _extract_locations(raw_locations: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_locations, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw_locations:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path", "")).strip()
        if not path:
            continue
        loc: dict[str, Any] = {"path": path}
        line = item.get("line")
        if isinstance(line, int) and not isinstance(line, bool):
            loc["line"] = line
        out.append(loc)
    return out


def _locations_from_arguments(arguments: Any) -> list[dict[str, Any]]:
    if not isinstance(arguments, dict):
        return []
    path = str(arguments.get("path", "")).strip()
    if not path:
        return []
    loc: dict[str, Any] = {"path": path}
    line = arguments.get("line")
    if isinstance(line, int) and not isinstance(line, bool):
        loc["line"] = line
    return [loc]


def _diff_block(raw_diff: Any) -> dict[str, Any] | None:
    if not isinstance(raw_diff, dict):
        return None
    path = str(raw_diff.get("path", "")).strip()
    if not path:
        return None
    old_text = raw_diff.get("old_text", raw_diff.get("oldText"))
    new_text = raw_diff.get("new_text", raw_diff.get("newText", ""))
    return {
        "type": "diff",
        "path": path,
        "oldText": str(old_text) if isinstance(old_text, str) else None,
        "newText": str(new_text) if new_text is not None else "",
    }


def _map_tool_completed(data: dict[str, Any], call_id: str) -> dict[str, Any]:
    exit_code = int(data.get("exit_code", 0))
    if exit_code == 0 and data.get("has_error"):
        exit_code = 1
    content: list[dict[str, Any]] = []
    diff = _diff_block(data.get("diff"))
    if diff is not None:
        content.append(diff)
    text = _tool_output_text(data)
    if text:
        content.append({"type": "content", "content": {"type": "text", "text": text}})
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
    if content:
        update["content"] = content
    locations = _extract_locations(data.get("locations"))
    if locations:
        update["locations"] = locations
    return update


def _map_patch_applied(data: dict[str, Any], call_id: str) -> dict[str, Any]:
    changed = data.get("changed_files", [])
    content: list[dict[str, Any]] = []
    raw_diffs = data.get("diffs")
    if isinstance(raw_diffs, list):
        for item in raw_diffs:
            diff = _diff_block(item)
            if diff is not None:
                content.append(diff)
    preview = str(data.get("diff_preview", "") or "")
    summary = preview or f"Modified files: {', '.join(str(p) for p in changed)}"
    if summary:
        content.append({"type": "content", "content": {"type": "text", "text": summary}})
    update: dict[str, Any] = {
        "sessionUpdate": "tool_call_update",
        "toolCallId": call_id,
        "status": "completed",
        "content": content,
    }
    if isinstance(changed, list):
        locs = [{"path": str(p)} for p in changed if str(p).strip()]
        if locs:
            update["locations"] = locs
    return update


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

    if etype == "assistant.delta":
        delta = str(data.get("delta", "") or "")
        if not delta:
            return []
        return [
            _update_payload(
                session_id,
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": delta},
                },
            )
        ]

    if etype == "plan.updated":
        entries = data.get("entries")
        if not isinstance(entries, list) or not entries:
            return []
        payload = build_plan_update(session_id, entries)
        return [payload] if payload["update"]["entries"] else []

    if etype == "tool.called":
        tool_name = str(data.get("tool_name", "tool"))
        title = f"[code-mode] {tool_name}" if data.get("code_mode") else tool_name
        args = data.get("arguments", {})
        update: dict[str, Any] = {
            "sessionUpdate": "tool_call",
            "toolCallId": call_id,
            "title": title,
            "kind": classify_tool_kind(tool_name),
            "status": "in_progress",
            "rawInput": args,
        }
        locations = _locations_from_arguments(args)
        if locations:
            update["locations"] = locations
        return [_update_payload(session_id, update)]

    if etype == "tool.completed":
        return [_update_payload(session_id, _map_tool_completed(data, call_id))]

    if etype == "patch.applied":
        return [_update_payload(session_id, _map_patch_applied(data, call_id))]

    return _map_thought_or_terminal_event(event, session_id)


def _thought_update(session_id: str, text: str) -> list[dict[str, Any]]:
    return [
        _update_payload(
            session_id,
            {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": text}},
        )
    ]


def _map_thought_or_terminal_event(
    event: StreamEvent,
    session_id: str,
) -> list[dict[str, Any]]:
    data = event.data
    etype = event.type
    if etype == "iteration.started":
        it = data.get("iteration", 0)
        max_it = data.get("max_iterations", "?")
        return _thought_update(session_id, f"Iteration {it}/{max_it}")
    if etype == "approval.requested":
        prefix = "[code-mode] " if data.get("code_mode") else ""
        return _thought_update(
            session_id,
            f"{prefix}Approval requested ({data.get('category', 'policy')}) "
            f"for {data.get('tool_name', 'tool')}: {data.get('reason', '')}",
        )
    if etype == "approval.decided":
        prefix = "[code-mode] " if data.get("code_mode") else ""
        verdict = "approved" if data.get("approved") else "denied"
        return _thought_update(
            session_id,
            f"{prefix}Approval {verdict} ({data.get('decided_by', 'policy')}) "
            f"for {data.get('tool_name', 'tool')}",
        )
    if etype == "subagent.started":
        return _thought_update(
            session_id,
            f"Subagent {data.get('subagent_id', 'sub')} started "
            f"({data.get('mode', 'research')}): {data.get('task', '')}",
        )
    if etype == "subagent.completed":
        tokens = int(data.get("total_tokens", 0) or 0)
        branch = data.get("branch")
        branch_note = f", branch={branch}" if branch else ""
        return _thought_update(
            session_id,
            f"Subagent {data.get('subagent_id', 'sub')} {data.get('state', 'completed')} "
            f"({data.get('mode', 'research')}, {tokens:,} tokens{branch_note})",
        )
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
            start_upd: dict[str, Any] = {
                "sessionUpdate": "tool_call",
                "toolCallId": tc.id,
                "title": tc.name,
                "kind": classify_tool_kind(tc.name),
                "status": "in_progress",
                "rawInput": tc.arguments,
            }
            locs = _locations_from_arguments(tc.arguments)
            if locs:
                start_upd["locations"] = locs
            updates.append(_update_payload(dev_session.id, start_upd))
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
