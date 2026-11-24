# ADR-020: Durable `DevSession` Store and Full `ContextEngine` State Serialization

- **Status:** Accepted
- **Date:** 2026-11-24
- **Issue:** `#205`

## Context

`AgentRun` persistence (`save_run` / `load_run`) captures individual one-shot runs under `~/.agent-forge/runs/<run_id>/`, whereas interactive ADE sessions span multiple prompts (`Turn 1`, `Turn 2`, ...) across IDE/CLI restarts. Rebuilding `ContextEngine` from scratch on every prompt or restart would lose compacted summaries, folded turn notes, Stage 1 pruned tool stubs, `WorkingSet` pins/failures/verification records, and force repeated repository instruction/map discovery.

## Decision

1. **Full `WorkingSet` & `ContextEngine` snapshots (`agent_forge/agent/context.py`):**
   - `WorkingSet.export_state()` / `from_state()` roundtrip all `WorkingFileEntry` records, sequence counters (`_seq`), `loaded_instruction_paths`, `active_failures`, `last_verification`, and eviction counters.
   - `ContextEngine.export_state()` / `restore_state()` / `from_state()` serialize `ContextPolicy`, `_initial_prepared`, `_base_instructions`, `_task_scoped_instructions`, `_repo_map`, `WorkingSet`, `_observed_invocations`, `_compaction_epoch`, `_compacted_group_cutoff`, `_folded_notes`, `_summary_message_text`, `_pruned_tool_stubs`, and `_tokens_saved_total`. Restored engines skip redundant initial instruction/map discovery.
   - `ContextEngine.force_compact()` exposes deterministic on-demand Stage 1 + Stage 2 compaction via shared `_run_compaction_pass`.
2. **Durable `DevSession` and `SessionStore` (`agent_forge/agent/session.py`, `persistence.py`):**
   - Shared JSONL codecs (`serialize_message`, `deserialize_message`, `serialize_tool_invocation`, `deserialize_tool_invocation`, `serialize_token_usage`, `deserialize_token_usage`) live in `persistence.py`.
   - `SessionStore` stores multi-turn sessions atomically under `~/.agent-forge/sessions/<session_id>/` (`session.json`, `messages.jsonl`, `events.jsonl`, `context_state.json`), supporting per-repo lookup (`list_sessions(repo_path=...)`, `latest_for_repo`), path-traversal validation, and `delete`.

## Consequences

- Downstream ACP stateful sessions (`#206`) and CLI interactive `chat`/`session` workflows (`#207`) share one durable session and context persistence substrate.
