# ADR-027: Opt-In Code Mode Programmatic Tool Execution

- **Status:** Accepted (Experimental Opt-In)
- **Date:** 2026-11-24
- **Issue:** #193
- **Related RFC & ADRs:** [RFC-003](../rfcs/003-code-mode-execution.md), [ADR-012](012-mcp-client-tool-integration.md), [ADR-019](019-declarative-tool-approvals-and-egress-proxy.md)

## Context

Registering external MCP tool catalogs alongside built-in tools expands
`ToolRegistry` to 25+ tools. Serializing every full JSON Schema in every LLM
request bloats context windows, while sequential JSON tool round-trips force
raw intermediate payloads back through model context.

## Decision

Implement opt-in **Code Mode** (`[agent].execution_mode = "tool_calling" |
"code_mode"`, defaulting to `"tool_calling"`) across the core harness:

1. **Minimal 2-tool model surface (`agent_forge/tools/code_mode.py`):**
   When `execution_mode == "code_mode"`, `react_loop` wraps the underlying
   `ToolRegistry` via `build_code_mode_registry()` so only `search_tools`
   (progressive schema lookup) and `execute_code` (Python orchestration runner)
   are serialized to the LLM.

2. **Credential-free sandbox + host RPC bridge (`agent_forge/agent/code_mode.py`):**
   `HostToolBridge.execute_script()` runs Python scripts inside the active
   `Sandbox` (no network, zero host/MCP secrets) and proxies `call_tool(name,
   arguments)` over an ephemeral workspace RPC channel cleanly removed in
   `finally:`.

3. **Full approval, circuit-breaker, and journal parity:**
   Every bridged call passes through `ApprovalGate` (including `deny` and
   mid-script interactive `prompt` callbacks), underlying MCP timeouts/breakers,
   and `RunJournal` (`action_requested` -> SHA-256 `idempotency_key` ->
   `action_result`), so `replay_journal()` and crash-safe resume behave
   identically to classic JSON tool calling.

## Consequences

- Keeps `execution_mode = "tool_calling"` as the safe default for compact tool
  sets while giving large multi-tool / MCP workflows progressive schema loading
  and local script filtering.
- Maintains strict host/sandbox secret isolation and zero blanket approval trust
  inside scripted tool execution.
- Paired A/B eval (`test_code_mode_ab_benchmark_vs_tool_calling` in
  `tests/eval/test_benchmark_corpus.py`) measures prompt footprint, iteration
  count, token usage, verified-fix parity, approval parity, and replay validity.
