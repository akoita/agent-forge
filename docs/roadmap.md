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

The [delivery plan](delivery-plan.md) records prerequisites, issue order, and exit criteria. GitHub tracks current assignments. Use the documented criteria and observed evidence to close a milestone.

## Product and engineering boundaries

- The core owns the agent loop, tools, configuration, policies, context, events, sandbox interface, and runner contract. Keep it portable and enforce the extension boundary with `scripts/check_boundaries.py`.
- Extensions own domain profiles, prompts, tools, and schemas. Extract the remaining legacy report behavior through [#140](https://github.com/akoita/agent-forge/issues/140) and [#141](https://github.com/akoita/agent-forge/issues/141).
- The hosted application owns identity, tenancy, queues, artifacts, deployment, and UI. Its release work is tracked in [#142](https://github.com/akoita/agent-forge/issues/142).
- Measure the effect of harness changes on task success, cost, latency, and tool failures. Add complexity when evidence shows it solves a user problem.

## Later work

Use task traces to prioritize the [context engine](https://github.com/akoita/agent-forge/issues/24) and [resumable terminal sessions](https://github.com/akoita/agent-forge/issues/137). Open standards guide any later [MCP](https://github.com/akoita/agent-forge/issues/40) or [ACP](https://github.com/akoita/agent-forge/issues/47) work. Parallel execution and [delegation](https://github.com/akoita/agent-forge/issues/32) need evaluation evidence and workspace isolation.

Fund fleet scaling, warm pools, and capacity dashboards when pilot measurements identify a bottleneck. The planned interfaces are the CLI, API, and pilot UI. Custom agent protocols, orchestration DSLs, vector memory by default, bespoke IDE plugins, and a tool marketplace remain outside the current roadmap.
