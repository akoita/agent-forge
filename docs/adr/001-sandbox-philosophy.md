# ADR-001: Sandbox Philosophy — Isolation, Not Restriction

**Status:** Accepted  
**Date:** 2026-03-08  
**Context:** [Issue #86](https://github.com/akoita/agent-forge/issues/86), [spec.md § 4.3](../spec.md)

---

## Context

Agent Forge describes itself as a _"sandboxed AI coding agent runtime."_ But what does "sandboxed" mean?

During Phase 1-2, the sandbox was implemented with maximum restrictions: no network, read-only root filesystem, Python-only Docker image, 120s command timeout. This made the agent secure but unable to perform real-world tasks like building a Node.js app (which requires `npm install`, network access, Node.js runtime, and writable cache directories).

## Decision

**Sandboxing means isolation, not restriction.**

The sandbox is an **isolation boundary** that prevents agent actions from affecting the host, other agents, or external services. Inside the sandbox, the agent should have access to all compute resources it needs.

This follows the model established by [E2B](https://e2b.dev): each agent runs in its own isolated environment with configurable access to filesystem, network, package managers, and runtimes.

### Principles

1. **Isolation** — Each run gets its own ephemeral container. No shared state.
2. **Secure by default** — Sandboxes start with restricted permissions.
3. **Configurable capabilities** — Permissions are opt-in, per-run, operator-controlled.
4. **Ephemeral** — Containers are destroyed after completion.

### Permission Model

```
Restricted ──────────────────────────────────────────▶ Capable

No network    Network enabled    Custom image    Full runtime
Read-only FS  Writable caches    Node/Go/Rust    Large tmpfs
120s timeout  600s timeout       Any packages    Exec allowed
```

Every capability starts disabled and is explicitly opted in via config or CLI flags.

### Hard Constraints (Never Relaxed)

- `--privileged` is **never** used
- `--security-opt no-new-privileges` is **always** applied
- Containers are **always** ephemeral (`--rm`)

## Consequences

- Phase 1-2 restrictions were correct as a **starting point**
- Phase 3+ must implement configurable capabilities (issue #86)
- The system prompt must adapt to the sandbox configuration
- Documentation must distinguish between isolation (always on) and restrictions (configurable)
