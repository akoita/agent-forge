# Agent Forge vision

Agent Forge is a **managed multi-agent system cloud infrastructure and coding platform** built on a small, measurable, domain-agnostic execution harness.

A developer or team can specify, test, deploy, monitor, and administer anything from a single coding agent to complex production multi-agent topologies — including a **Super-Agent** orchestrator that repartitions incoming project requests across verified, isolated workers. A high-level UX and DX spans declarative system specs (`system.yaml` and typed SDK), the CLI, any ACP-compatible IDE, and Agent Forge Studio (Web, Electron desktop, and Cloud Admin Console).

Every managed deployment guarantees four production properties:

- **Robust:** Verification-gated outputs tied to a known base revision, durable execution state across restarts, versioned releases, and instant canary rollback.
- **Secure:** Multi-tenant sandbox isolation, default-deny egress allowlists, host-brokered secrets, RBAC, and explicit approval policies.
- **Monitored:** Distributed traces across router and worker hops, live event streams, token/cost attribution, and continuous eval regression metrics.
- **Administrable:** Enforced spend and compute quotas, immutable policy audit logs, and live pause, drain, and emergency kill-switch controls.

The core (`agent_forge/*`) remains a compact, domain-agnostic coding harness checked mechanically for boundaries and measured by deterministic benchmarks. External extensions, Studio clients, and the managed cloud control plane layer around that shared verified runtime without custom agent wire protocols.

The [strategy](strategy.md) explains how to deliver and measure this platform. [ADR-031](adr/031-multi-agent-cloud-platform-and-super-agent.md) records the platform evolution, and the [roadmap](roadmap.md) tracks delivery milestones and issues.
