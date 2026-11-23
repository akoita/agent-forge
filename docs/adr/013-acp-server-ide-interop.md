# ADR-013: Agent Client Protocol (ACP) Server for IDE Interop

- **Status:** Accepted
- **Date:** 2026-11-23
- **Issue:** `#47` (M6: ACP server — IDE interop via the Agent Client Protocol)

## Context

Maintaining bespoke editor plugins per IDE (VS Code, JetBrains, Neovim, Zed) duplicates UI wiring, approval dialogs, and event streaming across clients. Implementing the open **Agent Client Protocol (ACP)** over newline-delimited JSON-RPC 2.0 on `stdin`/`stdout` enables any ACP-capable editor to drive Agent Forge natively.

## Decision

Implement `agent_forge/acp/` (`protocol.py`, `server.py`) and expose `agent-forge acp` over `stdin`/`stdout` while keeping stdout exclusively reserved for JSON-RPC frames:

```mermaid
sequenceDiagram
    participant Editor as ACP Editor Client
    participant ACP as ACPServer (agent_forge/acp)
    participant Loop as run_agent_task / react_loop
    participant Gate as ApprovalGate

    Editor->>ACP: initialize (protocolVersion=1)
    ACP-->>Editor: agentInfo + agentCapabilities (loadSession)
    Editor->>ACP: session/new (cwd, mcpServers)
    ACP-->>Editor: {sessionId}
    Editor->>ACP: session/prompt (sessionId, prompt)
    par Prompt execution (background task)
        Loop->>ACP: EventBus -> StreamEvent -> session/update (tool_call / tool_call_update / chunks)
        ACP-->>Editor: notification session/update
        Loop->>Gate: evaluate(tool_name, args)
        Gate->>ACP: callback(ApprovalRequest)
        ACP->>Editor: request session/request_permission (allow_once / allow_always / reject_once)
        Editor-->>ACP: {outcome: selected}
        ACP-->>Gate: approved bool
    and Concurrent inbound reader
        Editor-->>ACP: session/cancel (optional)
        ACP->>Loop: CancellationToken.cancel()
    end
    Loop-->>ACP: AgentRun (terminal state)
    ACP-->>Editor: response {stopReason, _meta.runId}
```

1. **Non-blocking bidirectional JSON-RPC loop (`agent_forge/acp/server.py`):** Execute each `session/prompt` turn as a background `asyncio.Task` while the inbound reader loop continues consuming `stdin`, enabling concurrent handling of client responses to outbound `session/request_permission` requests and `session/cancel` notifications. Redirect `sys.stdout` to `sys.stderr` during `serve_stdio()` while writing JSON-RPC frames to the captured binary stdout stream.
2. **Typed run-event mapping (`agent_forge/acp/protocol.py`):** Sanitize `EventBus` events into `StreamEvent` objects (`sanitize_event_data`) and map `tool.called`, `tool.completed`, `patch.applied`, `iteration.started`, `approval.*`, `run.failed`, and `run.cancelled` onto ACP `session/update` notifications (`tool_call`, `tool_call_update`, `agent_thought_chunk`, `agent_message_chunk`).
3. **Policy & MCP bridging:** Wire `ApprovalGate` callbacks directly to `session/request_permission` (honoring `allow_once`, session-scoped `allow_always` by request category, and `reject_once`), and translate session `mcpServers` into `MCPServerSettings` (`ADR-012`) cleanly per session without mutating global config.
4. **Explicit non-goal:** Bespoke IDE extensions (VS Code, JetBrains) remain out of scope; ACP replaces per-editor plugins.

## Consequences

- Any ACP-compatible editor can launch `agent-forge acp`, stream structured tool/thought updates, approve or deny gated actions interactively, and cancel in-flight runs.
- Existing `ApprovalGate`, `RunJournal`, `StreamEvent`, and `MCPClientManager` invariants apply automatically inside ACP sessions.
