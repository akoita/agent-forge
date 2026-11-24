# ADR-024: Live Streaming, Tool Progress, and Prompt History in Interactive Chat

- **Status:** Accepted
- **Date:** 2026-11-24
- **Issue:** #214 (`M10: Live streaming output, tool progress, and prompt history in agent-forge chat`)
- **Milestone:** 10. Daily-Driver ADE UX & v0.7.0 Release

## Context

`agent-forge chat` previously buffered every assistant response until the end of `run_turn()`, gave no real-time visibility into tool execution during long multi-step turns, offered only single-line raw `input()` prompts without persistent history, and risked mixing interactive `ApprovalGate` prompts mid-line with terminal output.

## Decision

1. **Incremental Streaming in Core Loop (`agent_forge/orchestration/events.py`, `agent_forge/agent/core.py`, `agent_forge/agent/runner.py`):**
   - Add `EventType.ASSISTANT_DELTA = "assistant.delta"`.
   - Add opt-in `stream_tokens: bool = False` to `react_loop()` and `execute_agent_run()`, invoking `_invoke_llm()` which streams chunks via `llm.stream()`, emits `ASSISTANT_DELTA` events, merges incremental `ToolCall` deltas, and cleanly falls back to `llm.complete()` when an adapter does not implement streaming (`NotImplementedError` or empty stream).
2. **REPL Coordinator, History, Multiline Entry & Approval Flushing (`agent_forge/agent/chat.py`):**
   - Add `_ReplOutputCoordinator` in `run_chat_repl()` to flush trailing newlines cleanly whenever tool progress (`TOOL_CALLED`, `TOOL_COMPLETED`, `APPROVAL_REQUESTED`, `APPROVAL_DECIDED`), interactive approvals (`_CoordinatorApprovalGate`), or full-line messages interrupt partial streamed lines, while suppressing duplicate post-turn output when deltas were already streamed.
   - Add `_PromptHistory` (with optional stdlib `readline` integration) persisted to `--history-file` / `~/.agent-forge/chat_history`, plus `_read_repl_entry()` supporting trailing `\` continuation and `"""..."""` multiline blocks.
3. **CLI Wiring (`agent_forge/cli.py`):**
   - Add `--stream/--no-stream` and `--history-file` to `agent-forge chat` and align `--approval-mode` choices with `["auto", "prompt", "deny"]`.

## Consequences

- Interactive terminal sessions stream assistant tokens and real-time tool activity immediately while maintaining backwards compatibility for batch/API callers (`stream_tokens=False` default) and non-streaming tests.
- All prompt history and streaming hooks stay host-side inside `agent_forge/agent/chat.py` and `agent_forge/cli.py` without touching sandbox boundaries.
