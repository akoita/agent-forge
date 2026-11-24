# ADR-021: Stateful Multi-Turn ACP Sessions and Disk-Backed `session/load` Replay

- **Status:** Accepted
- **Date:** 2026-11-24
- **Issue:** `#206`

## Context

Previously, `ACPSession` kept only ephemeral in-memory fields (`cwd`, `mcp_servers`, `allowed_categories`) while each `session/prompt` ran an independent one-shot `AgentRun` and `session/load` only registered an empty session. IDE clients lost multi-turn context between prompts and could not recover history after editor/server restarts.

## Decision

1. **Multi-turn `DevSession` integration in `run_agent_task` (`agent_forge/agent/runner.py`):**
   - Optional `dev_session` builds/restores `ContextEngine`, seeds subsequent turns via `DevSession.prepare_next_run()` while reusing `dev_session.system_prompt` for prompt-cache stability, and calls `DevSession.record_turn()` on completion.
2. **Disk-backed `ACPSession` & `ACPServer` (`agent_forge/acp/server.py`, `protocol.py`, `cli.py`):**
   - `ACPSession` owns a `DevSession` and cached `ContextEngine`.
   - `ACPServer` accepts `SessionStore` (exposed in CLI via `agent-forge acp --sessions-dir`), saving `DevSession` state, `allowed_categories`, and `mcp_servers` metadata atomically after `session/new`, `session/load` (new session), and every `session/prompt` turn.
   - `session/load` validates `sessionId`, restores persisted `DevSession` state when present, replays prior turns in chronological order via `build_replay_updates()` (`user_message_chunk`, `tool_call`, `tool_call_update`, `agent_message_chunk`) before sending the `session/load` result, and continues seamlessly on subsequent prompts without redundant repo discovery.

## Consequences

- Editor ACP integrations retain continuous context across turns and survive process restarts cleanly.
