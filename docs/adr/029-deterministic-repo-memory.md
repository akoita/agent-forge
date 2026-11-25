# ADR-029: Deterministic Persistent Repository Memory (`.agent-forge/memory.md`)

- **Status:** Accepted
- **Date:** 2026-11-25
- **Issue:** [#25](https://github.com/akoita/agent-forge/issues/25)
- **Deciders:** `@akoita`, Agent Forge maintainers

## Context

Agents repeatedly rediscover repository-specific conventions (verification commands, test fixtures, environment workarounds) across fresh CLI/chat/hosted runs. A vector database would add heavy operational dependencies and non-deterministic retrieval before simpler Markdown memory is measured.

## Decision

1. **Git-friendly Markdown store (`agent_forge/agent/memory.py`):** Persist concise learnings in `<repo_root>/.agent-forge/memory.md` via `RepoMemoryStore` with deterministic SHA-256 entry IDs (`mem-XXXXXXXX`), duplicate prevention, atomic rewrites, repo-root confinement, and fallback parsing of plain human-authored Markdown bullets.
2. **Bounded, prompt-cache-safe injection (`ContextEngine` & `prompts.py`):** Inject up to `context.max_memory_tokens` (`800` default) and up to 30 recent entries inside the static `{repo_context}` prefix before `## Task`, preserving KV/prompt-cache prefix stability across different tasks.
3. **Explicit CLI & Chat controls:** Expose `agent-forge memory list|add|remove|clear` and the interactive `/memory` chat slash command, plus `context.memory_enabled` / `AGENT_FORGE_CONTEXT_MEMORY_ENABLED` and `context.memory_path`.

## Consequences

- Teams can inspect, edit, or check `.agent-forge/memory.md` into Git without external services.
- Embedding/vector retrieval stays deferred unless repository memory scales beyond bounded Markdown budgets.
