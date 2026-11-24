# ADR-026: Bounded Hierarchical Subagent Delegation

- **Status:** Accepted
- **Date:** 2026-11-24
- **Issue:** #32
- **Related ADRs:** [ADR-002](002-harness-first-reset.md), [ADR-013](013-worktree-isolation-for-parallel-runs.md), [ADR-019](019-declarative-tool-approvals-and-egress-proxy.md)

## Context

Long coding tasks suffer from parent-context pollution when multi-file codebase
exploration or isolated sub-task edits stream full file reads and intermediate
tool transcripts directly into the parent conversation window. Conversely,
unbounded recursive subagent spawning introduces cost explosions, write races
against a shared working tree, and hidden background mutations.

## Decision

Introduce explicit, bounded hierarchical subagent delegation inside the core
harness (`agent_forge/agent/subagent.py` and `agent_forge/tools/subagent.py`)
under `spawn_subagent` with two strictly scoped execution modes:

1. **`research` mode (read-only exploration):**
   - Receives `build_read_only_registry()` containing only read/inspection tools
     (`read_file`, `list_directory`, `search_codebase`, `git_diff`).
   - Reuses the caller's active `Sandbox` without stopping it.
   - Multiple `spawn_subagent(mode="research")` calls emitted in a single parent
     turn run concurrently via `_can_execute_batch_in_parallel` in
     `agent_forge/agent/core.py`.

2. **`worker` mode (isolated coding):**
   - Requires a Git repository and leases an isolated Git worktree and
     `<branch_prefix>/<subagent_id>` branch via `WorktreeManager` with its own
     dedicated child `Sandbox`.
   - Receives `build_worker_registry()`, excluding remote/recursion tools
     (`git_push`, `create_pr`, `spawn_subagent`).
   - Finalizes synchronously under the per-repo worktree lock: commits diffs to
     the temporary branch, extracts a bounded patch + branch reference for the
     parent, and removes the worktree directory cleanly (aborting and removing
     both worktree and branch on failure).

3. **Hard Resource Bounds & Opt-In Surface:**
   - Controlled via `[subagents]` (`SubagentSettings` in `agent_forge/config.py`)
     and `--subagents / --no-subagents` on `run` and `chat`.
   - Enforces `max_depth` (default `1`), `max_subagents_per_run` (default `8`),
     `max_iterations` (default `10`), `max_tokens` (default `60,000`), and
     `max_result_chars` (default `8,000`).
   - Emits `EventType.SUBAGENT_STARTED` and `EventType.SUBAGENT_COMPLETED` on the
     parent `EventBus` and folds child `TokenUsage` into the parent run total.

## Consequences

- Parent agents explore codebases in parallel without polluting the primary
  context window or risking uncontrolled recursion.
- Worker edits remain cleanly isolated from the parent checkout until explicitly
  reviewed or integrated, with zero orphan worktrees on success or failure.
- Default `subagents.enabled = false` preserves exact backward compatibility for
  existing runs and benchmark baselines unless explicitly opted in.
