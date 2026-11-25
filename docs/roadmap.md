# Agent Forge roadmap

Agent Forge will deliver a verified local coding workflow, measure its quality, and offer the same workflow through a hosted pilot. The [vision](vision.md) describes the user experience. The [strategy](strategy.md) sets the engineering priorities and measures.

[ADR-002](adr/002-harness-first-reset.md) established independent evaluation and a domain-agnostic core. [Proposed ADR-003](adr/003-local-agent-and-hosted-pilot.md) records the staged local and hosted direction for review.

## Delivery milestones

| Milestone | User outcome |
| --- | --- |
| [1. Establish the verified-fix baseline](https://github.com/akoita/agent-forge/milestone/1) | Developers can distinguish a correct patch from an empty or incorrect change. |
| [2. Ship a verified local fix](https://github.com/akoita/agent-forge/milestone/2) | A developer can run the CLI on a committed revision, inspect the verified patch, and retain existing local edits. |
| [3. Use the agent on everyday work](https://github.com/akoita/agent-forge/milestone/3) | A developer can control spend, interrupt work, and continue from a known state. |
| [4. Measure and choose a model default](https://github.com/akoita/agent-forge/milestone/4) | A maintainer can qualify model candidates and choose the default using measured results. |
| [5. Operate the coding agent on GCP](https://github.com/akoita/agent-forge/milestone/5) | An operator can submit work remotely and retrieve a verified result across a restart. |
| [6. Run an invite-only SaaS pilot](https://github.com/akoita/agent-forge/milestone/6) | Two invited developers can submit and retrieve their own runs under enforced quotas and isolation. |
| [7. End-to-End Engineering Readiness](https://github.com/akoita/agent-forge/milestone/7) | Local models (`openai-compatible`), host auth/`git_push`, polyglot toolchains, and greenfield verification work end-to-end. |
| [8. Universal Polyglot Runtime & Safe Deployments](https://github.com/akoita/agent-forge/milestone/8) | Stock Docker images, `.devcontainer.json`/`mise`, two-phase `setup_command`, declarative approval hooks, and host egress proxy. |
| [9. Agentic Development Environment (ADE) Sessions](https://github.com/akoita/agent-forge/milestone/9) | Durable `DevSession` snapshots, stateful `ACP` replay, and interactive `agent-forge chat`/`session` CLI with warm sandboxes. |
| [10. Daily-Driver ADE UX & v0.7.0 Release](https://github.com/akoita/agent-forge/milestone/10) | OpenRouter gateway, live streaming & prompt history, `/diff`/`/undo`/`/sessions`/`/resume`, warm Docker integration tests, and `v0.7.0`. |
| [11. SOTA Agent Loop: Subagents & Code Mode](https://github.com/akoita/agent-forge/milestone/11) | Bounded hierarchical subagent delegation ([#32](https://github.com/akoita/agent-forge/issues/32)) and benchmarked Code Mode execution ([#193](https://github.com/akoita/agent-forge/issues/193)). |
| [12. SOTA Harness Hardening, Real-Sandbox Verification & v0.8.0](https://github.com/akoita/agent-forge/milestone/12) | Real Docker integration + chat/ACP UX ([#223](https://github.com/akoita/agent-forge/issues/223)), polyglot tree-sitter symbol/diagnostics loop ([#224](https://github.com/akoita/agent-forge/issues/224)), and `v0.8.0` paired baseline ([#225](https://github.com/akoita/agent-forge/issues/225)). |
| [13. Extension Packs, Safe Cloud/On-Chain Deployments & Repo Memory](https://github.com/akoita/agent-forge/milestone/13) | Domain-isolated GCP/AWS/Terraform ([#226](https://github.com/akoita/agent-forge/issues/226)) and EVM/Foundry ([#227](https://github.com/akoita/agent-forge/issues/227)) plugin packs plus deterministic `.agent-forge/memory.md` ([#25](https://github.com/akoita/agent-forge/issues/25)). |

The [delivery plan](delivery-plan.md) records prerequisites, issue order, and exit criteria. GitHub tracks current assignments. Use the documented criteria and observed evidence to close a milestone.

## Product and engineering boundaries

- The core owns the agent loop, tools, configuration, policies, context, events, sandbox interface, and runner contract. Keep it portable and enforce the extension boundary with `scripts/check_boundaries.py`.
- Extensions own domain profiles, prompts, tools, and schemas. Deliver cloud/blockchain workflows via extension packs (`plugins/cloud-deploy/`, `plugins/evm-foundry/`).
- The hosted application owns identity, tenancy, queues, artifacts, deployment, and UI. Its release work is tracked in [#142](https://github.com/akoita/agent-forge/issues/142).
- Measure the effect of harness changes on task success, cost, latency, and tool failures. Add complexity when evidence shows it solves a user problem.

## Later work

Milestones 11 and 12 (`v0.8.0`, [#32](https://github.com/akoita/agent-forge/issues/32), [#193](https://github.com/akoita/agent-forge/issues/193), [#223](https://github.com/akoita/agent-forge/issues/223)–[#225](https://github.com/akoita/agent-forge/issues/225)) have landed. Active engineering prioritizes Milestone 13 extension packs + deterministic repo memory ([#226](https://github.com/akoita/agent-forge/issues/226), [#227](https://github.com/akoita/agent-forge/issues/227), [#25](https://github.com/akoita/agent-forge/issues/25)) and the Milestone 6 hosted pilot ([#142](https://github.com/akoita/agent-forge/issues/142)).

Fund fleet scaling, warm pools, and capacity dashboards when pilot measurements identify a bottleneck. The planned interfaces are the CLI, API, and ACP/pilot UI. Custom agent protocols, orchestration DSLs, vector memory by default, bespoke IDE plugins, and a tool marketplace remain outside the current roadmap.
