# ADR-019: Declarative Tool Approval Gates and Domain-Allowlisted Egress Proxy

- **Status:** Accepted
- **Date:** 2026-11-24
- **Issue:** `#204`

## Context

External deployment tools provided via plugins (cloud CLIs, infrastructure orchestration, blockchain deployment tools) and workflows needing limited outbound network access (package registries, RPC endpoints) require uniform safety enforcement without hardcoding domain names or tool names inside core packages (`agent_forge/*`).

## Decision

1. **Declarative tool approval contract (`agent_forge/tools/base.py`, `agent/approvals.py`, `agent/core.py`):**
   - `Tool` defines default properties `requires_approval` (`False`), `approval_category` (`"tool_action"`), and method `approval_summary(arguments)`.
   - `classify_destructive_tool_call` inspects `requires_approval is True` generically on any tool (built-in or plugin) and produces an `ApprovalRequest` using the tool's own `approval_category` and `approval_summary(arguments)`.
   - `_can_execute_batch_in_parallel` prevents approval-gated tools from running via the parallel read-only shortcut even if marked non-side-effecting.
2. **Host-side domain-allowlisted HTTP/CONNECT egress proxy (`agent_forge/sandbox/egress_proxy.py`):**
   - `validate_egress_domain` validates exact domains and leading wildcards (`*.googleapis.com`) while rejecting schemes, ports, paths, `localhost`, IP literals, and bare `*`.
   - `EgressProxyServer` handles HTTP forwarding and HTTPS `CONNECT` tunneling, optionally enforces per-run `Proxy-Authorization` Basic credentials, records allowed/blocked hosts, blocks non-allowlisted destinations with HTTP `403`, and checks DNS resolution against private/loopback/link-local/reserved IPs to mitigate SSRF.
   - `DockerSandbox` and `BwrapSandbox` launch `EgressProxyServer` on demand when `sandbox.allowed_egress_domains` (or `--allow-domain`) is configured and advertise `HTTP_PROXY`/`HTTPS_PROXY` into the sandbox while `VerificationWorkspace.verify` clears `allowed_egress_domains`.

## Consequences

- Any plugin tool can opt into `ApprovalGate` prompting and audit events cleanly without core edits.
- Runs can grant scoped domain egress (e.g. `pypi.org`, `*.googleapis.com`) through the host proxy while keeping `VerificationWorkspace` strictly offline.
