"""Strict Pydantic v2 schemas and JSON-RPC frame validators for ACP."""
# ruff: noqa: N815

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ACPBaseModel(BaseModel):
    """Base model permitting upstream ACP ``_meta`` and forward extensions."""

    model_config = ConfigDict(extra="allow")


class ImplementationInfo(ACPBaseModel):
    """Client or agent implementation metadata exchanged during ``initialize``."""

    name: str
    version: str
    title: str | None = None


class FileSystemCapabilities(ACPBaseModel):
    """Filesystem capabilities advertised by an ACP client."""

    readTextFile: bool = False
    writeTextFile: bool = False


class ClientCapabilities(ACPBaseModel):
    """Client capabilities sent in ``initialize``."""

    fs: FileSystemCapabilities = Field(default_factory=FileSystemCapabilities)
    terminal: bool = False


class PromptCapabilities(ACPBaseModel):
    """Prompt block types accepted by the agent."""

    image: bool = False
    audio: bool = False
    embeddedContext: bool = False


class MCPCapabilities(ACPBaseModel):
    """MCP server transports accepted by the agent in ``session/new`` and ``session/load``."""

    http: bool = False
    sse: bool = False


class SessionListCapabilities(ACPBaseModel):
    """Capabilities for ``session/list``."""


class SessionCapabilities(ACPBaseModel):
    """Session-level capabilities supported by the agent."""

    list: SessionListCapabilities | None = None


class AgentCapabilities(ACPBaseModel):
    """Capabilities returned by the agent in ``initialize``."""

    loadSession: bool = False
    promptCapabilities: PromptCapabilities = Field(default_factory=PromptCapabilities)
    mcpCapabilities: MCPCapabilities = Field(default_factory=MCPCapabilities)
    sessionCapabilities: SessionCapabilities = Field(default_factory=SessionCapabilities)


class AuthMethod(ACPBaseModel):
    """Authentication method descriptor returned in ``initialize``."""

    id: str
    name: str
    description: str | None = None


class InitializeResult(ACPBaseModel):
    """Response payload for ``initialize``."""

    protocolVersion: int
    agentCapabilities: AgentCapabilities
    agentInfo: ImplementationInfo | None = None
    authMethods: list[AuthMethod] = Field(default_factory=list)


class AuthenticateResult(ACPBaseModel):
    """Response payload for ``authenticate``."""


class SessionMode(ACPBaseModel):
    """Mode descriptor in ``SessionModeState``."""

    id: str
    name: str
    description: str | None = None


class SessionModeState(ACPBaseModel):
    """Available modes and currently active mode ID."""

    availableModes: list[SessionMode]
    currentModeId: str


class SessionConfigSelectOption(ACPBaseModel):
    """Selectable option value within a ``SessionConfigOption``."""

    value: str
    name: str
    description: str | None = None


class SessionConfigOption(ACPBaseModel):
    """Runtime-mutable session configuration option."""

    id: str
    name: str
    description: str | None = None
    category: str | None = None
    type: Literal["select"] = "select"
    currentValue: str
    options: list[SessionConfigSelectOption]


class SessionNewResult(ACPBaseModel):
    """Response payload for ``session/new``."""

    sessionId: str
    modes: SessionModeState | None = None
    configOptions: list[SessionConfigOption] = Field(default_factory=list)


class SessionLoadResult(ACPBaseModel):
    """Response payload for ``session/load``."""

    modes: SessionModeState | None = None
    configOptions: list[SessionConfigOption] = Field(default_factory=list)


class SessionInfo(ACPBaseModel):
    """Summary metadata for a stored session returned by ``session/list``."""

    sessionId: str
    cwd: str
    title: str | None = None
    updatedAt: str | None = None


class SessionListResult(ACPBaseModel):
    """Response payload for ``session/list``."""

    sessions: list[SessionInfo]
    nextCursor: str | None = None


class SessionSetConfigOptionResult(ACPBaseModel):
    """Response payload for ``session/set_config_option``."""

    configOptions: list[SessionConfigOption]


class SessionSetModeResult(ACPBaseModel):
    """Response payload for ``session/set_mode``."""


class SessionPromptResult(ACPBaseModel):
    """Response payload for ``session/prompt``."""

    stopReason: Literal[
        "end_turn",
        "max_tokens",
        "max_turn_requests",
        "refusal",
        "cancelled",
    ]


class TextContent(ACPBaseModel):
    """Plain text or markdown content block."""

    type: Literal["text"] = "text"
    text: str


class ContentToolCallContent(ACPBaseModel):
    """Wrapped content block inside a tool call update."""

    type: Literal["content"] = "content"
    content: TextContent


class DiffToolCallContent(ACPBaseModel):
    """Structured file diff block inside a tool call update."""

    type: Literal["diff"] = "diff"
    path: str
    oldText: str | None = None
    newText: str


ToolCallContentBlock = Annotated[
    ContentToolCallContent | DiffToolCallContent,
    Field(discriminator="type"),
]


class ToolCallLocation(ACPBaseModel):
    """Workspace file location touched or inspected by a tool call."""

    path: str
    line: int | None = None


class PlanEntry(ACPBaseModel):
    """Single item in an ACP ``plan`` update."""

    content: str
    priority: Literal["high", "medium", "low"] = "medium"
    status: Literal["pending", "in_progress", "completed"] = "pending"


class AvailableCommandInput(ACPBaseModel):
    """Optional argument hint metadata for an advertised slash command."""

    hint: str


class AvailableCommand(ACPBaseModel):
    """Slash command descriptor advertised via ``available_commands_update``."""

    name: str
    description: str
    input: AvailableCommandInput | None = None


class UserMessageChunkUpdate(ACPBaseModel):
    """Replayed user prompt chunk."""

    sessionUpdate: Literal["user_message_chunk"] = "user_message_chunk"
    content: TextContent


class AgentMessageChunkUpdate(ACPBaseModel):
    """Streamed or replayed assistant message delta chunk."""

    sessionUpdate: Literal["agent_message_chunk"] = "agent_message_chunk"
    content: TextContent


class AgentThoughtChunkUpdate(ACPBaseModel):
    """Streamed agent progress or reasoning note."""

    sessionUpdate: Literal["agent_thought_chunk"] = "agent_thought_chunk"
    content: TextContent


class ToolCallUpdateStart(ACPBaseModel):
    """Initial tool execution announcement."""

    sessionUpdate: Literal["tool_call"] = "tool_call"
    toolCallId: str
    title: str
    kind: Literal[
        "read",
        "edit",
        "delete",
        "move",
        "search",
        "execute",
        "think",
        "fetch",
        "other",
    ]
    status: Literal["pending", "in_progress", "completed", "failed"]
    rawInput: Any = None
    locations: list[ToolCallLocation] = Field(default_factory=list)


class ToolCallUpdateDelta(ACPBaseModel):
    """Subsequent update to a tool call with output, structured diffs, and locations."""

    sessionUpdate: Literal["tool_call_update"] = "tool_call_update"
    toolCallId: str
    status: Literal["pending", "in_progress", "completed", "failed"] | None = None
    content: list[ToolCallContentBlock] = Field(default_factory=list)
    locations: list[ToolCallLocation] = Field(default_factory=list)
    rawOutput: Any = None


class PlanUpdate(ACPBaseModel):
    """Ordered task progress plan emitted on ``session/update``."""

    sessionUpdate: Literal["plan"] = "plan"
    entries: list[PlanEntry]


class AvailableCommandsUpdate(ACPBaseModel):
    """Slash-command advertisement update."""

    sessionUpdate: Literal["available_commands_update"] = "available_commands_update"
    availableCommands: list[AvailableCommand]


class CurrentModeUpdate(ACPBaseModel):
    """Mode-change notification."""

    sessionUpdate: Literal["current_mode_update"] = "current_mode_update"
    currentModeId: str


class ConfigOptionUpdate(ACPBaseModel):
    """Configuration options refresh notification."""

    sessionUpdate: Literal["config_option_update"] = "config_option_update"
    configOptions: list[SessionConfigOption]


SessionUpdatePayload = Annotated[
    UserMessageChunkUpdate
    | AgentMessageChunkUpdate
    | AgentThoughtChunkUpdate
    | ToolCallUpdateStart
    | ToolCallUpdateDelta
    | PlanUpdate
    | AvailableCommandsUpdate
    | CurrentModeUpdate
    | ConfigOptionUpdate,
    Field(discriminator="sessionUpdate"),
]


class SessionUpdateNotificationParams(ACPBaseModel):
    """Params envelope for ``session/update`` notifications."""

    sessionId: str
    update: SessionUpdatePayload


class PermissionOption(ACPBaseModel):
    """Interactive choice offered in ``session/request_permission``."""

    optionId: str
    name: str
    kind: Literal["allow_once", "allow_always", "reject_once", "reject_always"]


class SessionRequestPermissionParams(ACPBaseModel):
    """Outbound server-to-client request params for ``session/request_permission``."""

    sessionId: str
    toolCall: dict[str, Any]
    options: list[PermissionOption]


class FsReadTextFileParams(ACPBaseModel):
    """Outbound server-to-client request params for ``fs/read_text_file``."""

    sessionId: str
    path: str
    line: int | None = None
    limit: int | None = None


_RESULT_MODELS: dict[str, type[ACPBaseModel]] = {
    "initialize": InitializeResult,
    "authenticate": AuthenticateResult,
    "session/new": SessionNewResult,
    "session/load": SessionLoadResult,
    "session/list": SessionListResult,
    "session/set_config_option": SessionSetConfigOptionResult,
    "session/set_mode": SessionSetModeResult,
    "session/prompt": SessionPromptResult,
}

_OUTBOUND_REQUEST_MODELS: dict[str, type[ACPBaseModel]] = {
    "session/request_permission": SessionRequestPermissionParams,
    "fs/read_text_file": FsReadTextFileParams,
}


def _validate_request_or_notification(msg: dict[str, Any]) -> None:
    msg_method = str(msg["method"])
    params = msg.get("params")
    if not isinstance(params, dict):
        raise ValueError(f"Expected dict params for method '{msg_method}'")
    if "id" not in msg and msg_method == "session/update":
        SessionUpdateNotificationParams.model_validate(params)
        return
    if "id" in msg and msg_method in _OUTBOUND_REQUEST_MODELS:
        _OUTBOUND_REQUEST_MODELS[msg_method].model_validate(params)


def _validate_response_frame(msg: dict[str, Any], method: str | None) -> None:
    if "error" in msg and msg["error"] is not None:
        err = msg["error"]
        if not isinstance(err, dict) or not isinstance(err.get("code"), int):
            raise ValueError("Invalid JSON-RPC error object")
        if not isinstance(err.get("message"), str):
            raise ValueError("Invalid JSON-RPC error message")
        return
    if "result" not in msg:
        raise ValueError("JSON-RPC response missing 'result' and 'error'")
    if method and method in _RESULT_MODELS:
        _RESULT_MODELS[method].model_validate(msg["result"])


def validate_acp_frame(msg: dict[str, Any], *, method: str | None = None) -> None:
    """Validate an ACP JSON-RPC 2.0 frame against the protocol schemas."""
    if msg.get("jsonrpc") != "2.0":
        raise ValueError("Missing or invalid jsonrpc='2.0'")
    if "method" in msg:
        _validate_request_or_notification(msg)
        return
    _validate_response_frame(msg, method)
