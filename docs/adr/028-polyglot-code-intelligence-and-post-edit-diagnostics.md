# ADR-028: Polyglot Code Intelligence and Post-Edit Diagnostics Loop

- **Status:** Accepted
- **Date:** 2026-11-25
- **Issue:** #224
- **Related ADRs:** [ADR-002](002-harness-first-reset.md), [ADR-017](017-zero-wrapper-stock-images-and-host-toolchain-bwrap.md), [ADR-020](020-durable-dev-session-and-context-state.md)

## Context

`ContextEngine` builds a PageRank-ranked repository map (`repo_map.py`) to
inject high-signal symbol outlines into prompt prefixes, and the ReAct loop
previously waited until the user or final verification command ran full tests
to discover trivial syntax or undefined-symbol mistakes made during edits.

## Decision

1. **Optional tree-sitter AST extraction with hardened polyglot fallback (`agent_forge/agent/treesitter.py`, `agent_forge/agent/repo_map.py`):**
   - Provide an optional `code-intel` extra (`tree-sitter`, `tree-sitter-language-pack`) mapped across TypeScript/TSX, JavaScript, Go, Rust, Java, C/C++, Ruby, C#, PHP, Kotlin, Swift, Shell, and `.sol`.
   - When tree-sitter grammars are absent, fall back seamlessly to comment/string-stripped polyglot extraction with brace-depth `parent` tracking so commented code and string literals never create false PageRank edges.
   - Python continues to use stdlib `ast`.

2. **Opt-in lightweight post-edit diagnostics (`agent_forge/agent/diagnostics.py`):**
   - Controlled via `[agent].post_edit_diagnostics = false` (env `AGENT_FORGE_AGENT_POST_EDIT_DIAGNOSTICS`, CLI `--diagnostics / --no-diagnostics`).
   - Runs automatically after successful `write_file`, `edit_file`, or non-dry-run `apply_patch` calls inside the active `Sandbox` (in both `tool_calling` and `code_mode` paths).
   - Selects fast language-native checkers (`python3 -m py_compile` + `ruff` critical syntax/name rules, `node --check`, `tsc --noEmit`, `go vet`, `cargo check`, `sh -n`, `python3 -m json.tool`) and gracefully skips when a toolchain binary is not present.
   - Appends a bounded `[POST-EDIT DIAGNOSTICS]` block when errors occur so the model can self-correct on the very next turn before invoking full test suites.

## Consequences

- Zero required native C-wheel dependencies are added to core installation; `agent-forge[code-intel]` upgrades polyglot AST precision cleanly.
- Opt-in default (`post_edit_diagnostics = false`) keeps existing benchmark trajectories and latency unchanged while giving developers immediate feedback when enabled.
