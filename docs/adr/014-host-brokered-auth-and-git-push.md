# ADR-014: Host-Brokered Service Authentication and `git_push`

- **Status:** Accepted
- **Date:** 2026-11-24
- **Issue:** `#196` (M7: Host-brokered GitHub push and service credential management)

## Context

Agent Forge sandboxes run with `--network none` by default and strip host environment secrets via `sanitize_sandbox_env()`. Previously, the built-in toolset offered `git_commit` and `create_pr` (on the host), while pushing a branch still forced operators to export shell tokens manually or relax container network isolation. That broke offline isolation guarantees whenever an agent needed to push a feature branch to a remote.

## Decision

Keep credentials exclusively on the host and broker authenticated `git push` from the host process via `agent_forge/auth.py` and `GitPushTool` (`agent_forge/tools/git_push.py`):

```mermaid
sequenceDiagram
    participant Loop as ReAct Loop
    participant Gate as ApprovalGate
    participant Push as GitPushTool (Host)
    participant Store as CredentialStore (0600)
    participant Git as Host git + Ephemeral ASKPASS
    participant Sandbox as Sandbox (--network none)

    Loop->>Gate: git_push(remote="origin", branch?)
    Gate-->>Loop: category "git_push" or "git_force_push"
    Loop->>Push: execute(args, sandbox)
    Push->>Sandbox: read host_workspace_path (no secrets injected)
    Push->>Store: resolve_github_token() (env or credentials.json)
    Push->>Git: git push with temp GIT_ASKPASS script (0700)
    Git-->>Push: exit code + output
    Push->>Push: scrub_secret() before returning ToolResult
```

1. **Host-only credential store (`agent_forge/auth.py`):** Store service tokens in `~/.agent-forge/credentials.json` inside a `0700` directory with atomic `0600` file writes (`CredentialStore`), isolated outside any sandbox mount. Manage entries through `agent-forge auth status|login|logout|import-gh`.
2. **Precedence without ambient side effects:** `resolve_github_token()` checks `GITHUB_TOKEN`, `GH_TOKEN`, and then `CredentialStore`; importing credentials from the GitHub CLI requires explicit `agent-forge auth import-gh`. `CreatePRTool` (`git_common.py`) and `HttpMCPTransport` (`agent_forge/tools/mcp.py`) consume the same host resolver when environment variables are absent.
3. **Host-brokered `git_push` with safety gates (`agent_forge/tools/git_push.py`):** Expose `Sandbox.host_workspace_path` so `GitPushTool` executes `git push` on the host checkout with `GIT_TERMINAL_PROMPT=0` and an ephemeral `0700` `GIT_ASKPASS` script deleted immediately after the subprocess exits. Reject protected branch targets (`main`, `master`) unless `allow_protected=True`, enforce `--force-with-lease` via `ApprovalGate` category `git_force_push` (`git_push` otherwise), and scrub secrets (`scrub_secret`) from returned `stdout`/`stderr`.

## Consequences

- Sandboxes remain `--network none` with zero secret injection while supporting full branch -> commit -> push -> PR workflows.
- Destructive remote updates remain gated by `ApprovalGate`, and raw tokens never appear in container environments, git remote URLs, or tool transcripts.
