# ADR-025: Chat Workspace Controls (`/diff`, `/undo`, `/sessions`, `/resume`) & Warm Docker Verification

- **Status:** Accepted
- **Date:** 2026-11-24
- **Issue:** #215 (`M10: Interactive workspace controls (/diff, /undo, /sessions) and warm Docker integration tests`)
- **Milestone:** 10. Daily-Driver ADE UX & v0.7.0 Release

## Context

Developers using `agent-forge chat` needed in-session controls to inspect modified workspace files (`/diff`), safely revert mistaken agent turns without overwriting unrelated local edits (`/undo`), switch among persisted sessions without restarting the CLI (`/sessions`, `/resume`), and verify with a real Docker container that filesystem changes and interactive PTY sessions remain warm across consecutive turns.

## Decision

1. **Pre/Post Turn Workspace Snapshots & Safe Undo (`agent_forge/agent/workspace_undo.py`, `agent_forge/agent/chat.py`):**
   - Capture non-ignored workspace file states (`git ls-files --cached --others --exclude-standard` when in a Git repo, or bounded directory walk otherwise) before and after `execute_agent_run()`.
   - Record minimal reversible `TurnWorkspaceSnapshot` entries containing only touched files' pre-turn bytes and post-turn SHA-256 hashes.
   - `undo_last_turn()` verifies current on-disk hashes still match post-turn hashes before restoring pre-turn bytes or unlinking turn-created files, aborting with conflict details unless `/undo --force` is requested.
   - `render_workspace_diff()` summarizes `git status --short` and `git diff` (`--stat` optional) cleanly.
2. **In-Place Session Listing & Switching (`agent_forge/agent/chat.py`):**
   - Add `/sessions [--all] [N]` (`list_stored_sessions()`) and `/resume <session_id|latest>` (`switch_session()`), keeping the warm sandbox alive when switching within the same resolved repository path and shutting it down cleanly when switching repositories.
3. **Real Docker Integration Coverage (`tests/integration/test_warm_chat_sandbox_integration.py`):**
   - Exercise `ChatSessionRuntime` directly against a real `DockerSandbox` (`agent-forge-sandbox:latest`), asserting identical `container_id`, persisted `/workspace` files, live Python PTY variables across turns, container rotation on `idle_timeout_seconds` expiry, and clean shutdown on `close()`.

## Consequences

- Multi-turn terminal chat supports safe rollbacks and workspace inspection without leaving uncommitted repository state vulnerable.
- Warm sandbox lifecycle guarantees are tested end-to-end against real Docker containers in `CI/Integration Tests`.
