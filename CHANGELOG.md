# Changelog

All notable changes to Agent Forge are documented in this file.

## [0.7.0] - 2026-11-24

### Added
- **Provider API v2, OpenAI-Compatible & OpenRouter Adapters (`agent_forge/llm/`):**
  - Provider capability negotiation (`capabilities.py`), OpenAI Responses API migration, and policy-driven per-turn model routing (`router.py`).
  - Config-only `openai-compatible` support for local Ollama, vLLM, and `llama.cpp` endpoints (`#46`).
  - First-class `openrouter` gateway provider with fallback `models`, routing/privacy controls (`allow_fallbacks`, `require_parameters`, `data_collection`, `zdr`), `reasoning_details` multi-turn preservation, and gateway-reported cost tracking (`#145`).
  - Refreshed frontier model metadata, per-profile USD/1M pricing overrides, and reasoning/thinking knobs (`#198`).
- **Harness Core, Verification & Recovery (`agent_forge/agent/`, `agent_forge/orchestration/`):**
  - Context Engine (`context.py`, `instructions.py`, `repo_map.py`) with hierarchical instruction discovery, repository maps, working-set pinning, and two-stage compaction (`#24`).
  - Verification-aware `CompletionContract` enforcement (`#139`), fsynced `RunJournal` with idempotency keys & `replay` (`#138`), and worktree-isolated parallel agent execution (`#33`).
  - Resumable PTY terminal sessions with bounded pagination and PGID orphan cleanup (`#137`).
- **IDE Interop, External Tools & Universal Polyglot Sandbox (`agent_forge/acp/`, `agent_forge/tools/`, `agent_forge/sandbox/`):**
  - External MCP stdio/HTTP client with circuit breaker & `ApprovalGate` enforcement (`#40`), plus ACP JSON-RPC 2.0 server (`agent-forge acp`, `#47`).
  - Host-brokered service auth (`agent-forge auth`) and `git_push` (`#196`).
  - Polyglot toolchain auto-detection & greenfield verification (`#197`), zero-wrapper stock Docker images & Bubblewrap host-toolchain passthrough (`#202`), `.devcontainer.json` + `Dockerfile.universal` (`mise`) + two-phase `setup_command` (`#203`), and declarative `Tool` approval hooks & host egress proxy (`#204`).
- **Agentic Development Environment (ADE) Sessions (`agent_forge/agent/session.py`, `chat.py`, `workspace_undo.py`):**
  - Durable `DevSession` & `SessionStore` with `ContextEngine` snapshot serialization (`#205`) and stateful ACP `session/load` replay (`#206`).
  - Interactive `agent-forge chat` REPL & `agent-forge session list|show|compact|delete` with warm session-scoped Docker sandboxes (`#207`), live token streaming (`EventType.ASSISTANT_DELTA`), tool progress & persistent prompt history (`#214`), and workspace controls (`/diff`, `/undo`, `/sessions`, `/resume`, `#215`).

### Changed
- Package version bumped from `0.6.0` to `0.7.0` in `pyproject.toml` and `agent_forge/__init__.py`; `acp/protocol.py` and `tools/mcp.py` now read `__version__` directly instead of duplicating literal version strings.
- Checked in refreshed full deterministic eval baseline at `docs/eval/baseline-v0.7.0.{json,md}` (`#216`).
