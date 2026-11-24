# RFC 003 — Code Mode programmatic tool execution in sandboxed code

**Status:** Accepted (Experimental Opt-In · [ADR-027](../adr/027-code-mode-programmatic-tool-execution.md)) · 2026-11-24  
**Issue:** [#193](https://github.com/akoita/agent-forge/issues/193) · Related: [#40](https://github.com/akoita/agent-forge/issues/40), [ADR-012](../adr/012-mcp-client-tool-integration.md), [ADR-027](../adr/027-code-mode-programmatic-tool-execution.md), [strategy](../strategy.md#measure-the-harness-with-the-model)

> Design proposal only — no runtime code changes accompany this RFC. Implementation awaits review and M6 completion.

## Problem

With external MCP server integration ([ADR-012](../adr/012-mcp-client-tool-integration.md)), `ToolRegistry` can accumulate dozens or hundreds of tool schemas. Sending every JSON Schema on each LLM turn bloats prompts, while multi-step workflows force sequential round trips and copy raw intermediate outputs through the model's context window.

## Proposal

Introduce an opt-in **Code Mode** execution path (`[agent].execution_mode = "tool_calling" | "code_mode"`, defaulting to `"tool_calling"`) alongside classic JSON tool calling:

```mermaid
flowchart TD
    LLM["LLM Turn"] -->|search_tools(query)| Disc["Progressive Schema Index (Native + MCP)"]
    LLM -->|execute_code(python_script)| Sandbox["--network none Sandbox (zero secrets)"]
    Sandbox -->|RPC request: tool_name, args| Bridge["Host Tool Bridge"]
    Bridge --> Gate{"ApprovalGate + Policy"}
    Gate -->|Denied| Deny["Return denial error to script"]
    Gate -->|Approved| Journal["RunJournal (action_requested -> idempotency -> action_result)"]
    Journal -->|MCP tool| Breaker["MCPCircuitBreaker + Timeout"]
    Journal -->|Native tool| Exec["Sandbox Tool Execution"]
    Breaker --> Return["Structured result to script"]
    Exec --> Return
    Sandbox -->|Final filtered stdout / exit code| Context["Compact ToolResult in LLM Context"]
```

1. **Progressive disclosure (`search_tools`):** Instead of serializing all tool schemas upfront, expose a compact `search_tools` lookup over `ToolRegistry` so the model loads only the signatures needed for the current task.
2. **Programmatic orchestration (`execute_code`):** Allow the model to emit a short Python script that loops, filters, batches, and handles partial failures locally before returning condensed output to the conversation history.
3. **Credential-free sandbox + host bridge:** Execute generated scripts inside the existing `--network none` sandbox with zero LLM or MCP credentials in container env; proxy nested tool calls over a narrow host-side channel.

## Security and harness invariants

- **No blanket trust:** Every host-bridged native or MCP call inside `execute_code` still passes through `ApprovalGate` (`approvals.py`) with interactive prompt/deny parity.
- **Failure isolation preserved:** Remote MCP calls executed via the bridge remain subject to `MCPCircuitBreaker` and server timeouts.
- **Deterministic replay and resume:** Log each bridged tool call inside `RunJournal` (`run-journal-v1`) so `agent-forge replay` and `agent-forge resume` skip already-committed side effects accurately.

## Alternatives and trade-offs

- **Classic JSON tool calling:** Simpler per-turn journaling and lower overhead when fewer than ~15 compact tools are registered; remains the default (`execution_mode = "tool_calling"`).
- **MCP-server-side Code Mode only:** Reduces MCP schema size per server, but leaves native + multi-server composition fractured across servers and outside Agent Forge's central `ApprovalGate` and `RunJournal`.
- **Key design risks to resolve before coding:** Bounded script timeouts, interactive `prompt` approval round-trips mid-script, and script syntax-retry noise on weak models.

## Evaluation plan and decision criteria

Promote beyond experimental opt-in only after paired A/B benchmark evaluation (`make eval`) across M0 tasks and multi-tool MCP fixtures measures:

- **Tokens per task** (target: meaningful reduction on multi-step and 20+ tool scenarios),
- **Iterations and wall-clock latency per task**,
- **Verified-fix rate** (must match or exceed `tool_calling` with `0.0` false-positive rate), and
- **Full parity on approval enforcement, secret isolation, and journal replay.**
