# Agent Forge strategy

Start with one verified local fix and carry that same task through the hosted stack. This gives contributors a baseline for separating model failures, harness failures, and deployment failures. The [vision](vision.md) defines the intended user experience.

## Prove the local workflow

Grade changes on an independent checkout. Check that the patch applies to its declared base and that it changes the expected outcome. Keep a deterministic smoke test in pull-request CI and use separately reported live runs to measure verified fixes, latency, and cost.

## Keep model qualification repeatable

Use a shared provider factory for the CLI and service. Keep protocol details and continuation state inside adapters. Aim for configuration plus validation when a new model uses a supported protocol. Qualify current candidates from Google, OpenAI, Anthropic, and Meta when the benchmark starts, subject to API access. Record the model identifier, reasoning settings, harness revision, and budget. Promote one default using verified results.

## Deploy in stages

Extract a shared runner and preserve existing extension compatibility. Deploy an owner-operated GCP workflow with durable status, an authenticated canary, and private artifacts, then validate isolation, ownership, quotas, cancellation, and cleanup in the invite-only pilot.

Extend that verified foundation into the **Managed Multi-Agent System Cloud Infrastructure**:

- **Declarative DX first:** Validate single- and multi-agent topologies (`system.yaml` / SDK) locally with dry-run parity before cloud deployment.
- **Controlled rollout:** Version every multi-agent system revision with canary promotion, rollback, and multi-tenant isolation.
- **Operational governance:** Ship OpenTelemetry traces, cost/eval telemetry, immutable approval audit logs, and kill-switch/pause controls in the same control plane.

## Scale from single agents to verified multi-agent systems

Introduce project-level **Super-Agent** orchestration ([RFC-005](rfcs/005-super-agent-project-orchestration.md)) to break multi-part project inputs into dependency-ordered, worktree-isolated tickets under an explicit admission matrix and human attention budget. Measure multi-agent and Super-Agent orchestrations against single-session baselines on verified completion rate, merge-conflict rate, human-interruption count, and token cost before promoting defaults.

## Maintain the references

| Reference | Question it answers | When to update |
| --- | --- | --- |
| [Vision](vision.md) | Who is this for and what should they be able to do? | When the intended user, outcome, or product scope changes. |
| Strategy | How will the project reach that outcome and measure progress? | When evidence changes the delivery approach, priorities, or metrics. |
| [Roadmap](roadmap.md) and GitHub milestones | What will ship and how will we know it is ready? | When ordering, dependencies, or exit criteria change. |
| [Architecture](architecture.md) and operational guides | What does the delivered system do? | In the same change as the behavior they describe. |

For a substantial change, link the relevant vision or strategy objective and the evidence used to evaluate alignment in the issue or PR. If the proposal changes direction, update the affected reference with the decision.

Write an RFC for significant changes that still need a choice, such as the runner contract, provider state, or hosted execution boundary. Include the alternatives, trade-offs, compatibility and rollout, and verification. Once the choice is accepted, record it in an [ADR](adr/) and link the RFC when one exists. Link the implementation and update the living docs when the change ships. A later decision can supersede the ADR while retaining its rationale.
