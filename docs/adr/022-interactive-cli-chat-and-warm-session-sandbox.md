# ADR-022: Interactive CLI Chat Runtime & Warm Session Sandbox

- **Status:** Accepted
- **Date:** 2026-11-24
- **Issue:** #207 (`M9: Interactive CLI dev session (agent-forge chat/session) with warm sandbox and /compact`)
- **Milestone:** 9. Agentic Development Environment (ADE) Sessions

## Context

One-shot `agent-forge run` invocations tear down the sandbox container and active PTY sessions at the end of every run. When developers iterate interactively in a terminal REPL (`agent-forge chat`), cold-starting a sandbox on every prompt destroys ephemeral container state (build caches, background processes, interactive PTY sessions) and forces users to restart long-running workflows. Developers also need manual context compaction (`/compact`), live token budget inspection (`/context`), model switching (`/model`), and offline session management (`agent-forge session list|show|compact|delete`) backed by durable `DevSession` snapshots.

## Decision

1. **`ChatSessionRuntime` (`agent_forge/agent/chat.py`):**
   - Owns a durable `DevSession`, `SessionStore`, and `ContextEngine` synchronized with the active model's context window.
   - Manages a warm session-scoped `Sandbox` kept alive across `run_turn()` calls via `execute_agent_run(..., manage_sandbox=False)`, checking `sandbox.is_alive()` and restarting cleanly if idle time exceeds `idle_timeout_seconds` or if `close()` / `/clear --sandbox` is invoked.
   - Implements deterministic local slash-command dispatch (`/compact [keep_recent]`, `/context`, `/model [spec]`, `/clear [--sandbox]`, `/help`, `/exit`, `/quit`) through `SlashCommandResult`, plus `run_chat_repl()` with injectable I/O hooks for deterministic testing.
2. **CLI Commands (`agent_forge/cli.py`):**
   - Add `agent-forge chat` supporting `--repo`, `--resume <id|latest>`, `--session-id`, `--sessions-dir`, `--idle-timeout`, and standard provider/model/sandbox overrides.
   - Add `agent-forge session list|show|compact|delete` with `--sessions-dir` and `--output-format text|json`.
3. **Read-Only Budget Inspection (`agent_forge/agent/context.py`):**
   - Add `ContextEngine.assembled_token_estimate()` so `/context` computes both raw and post-compaction prompt tokens without mutating compaction state.

## Consequences

- Multi-turn terminal development sessions preserve sandbox filesystem and PTY state across prompts while keeping `--network none` security guarantees intact.
- Stored sessions remain interoperable across CLI `agent-forge chat`, `agent-forge session`, and `agent-forge acp` via `SessionStore`.
- All new modules reside in generic core packages without domain-specific vocabulary (`check_boundaries.py` baseline remains `0`).
