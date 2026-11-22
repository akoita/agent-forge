# ADR 003 — Deliver a local coding agent and a hosted pilot

**Status:** Proposed · 2026-11-21

## Context

[ADR 002](002-harness-first-reset.md) established a small, measurable harness and parked the hosted control plane. The project now also aims to exercise that harness as a real cloud service. The [vision](../vision.md) defines the intended developer experience and the [strategy](../strategy.md) defines the evidence for each release.

## Proposed decision

Deliver a verified local fix first and use that fixture as the hosted canary. Include an owner-operated GCP service and an invite-only pilot in the delivery plan. Give the CLI and service a shared runner contract. Keep tenancy, deployment, dispatch, and artifact access in the hosted application.

If accepted, this decision supersedes the parked status of the hosted product in ADR 002. The evaluation gates, domain-agnostic core, and separation of deployment responsibilities still apply.

## Alternatives and consequences

Keeping the cloud work parked would concentrate effort on local quality, with fewer opportunities to learn about durability and service operation. Starting in the cloud would expose operational constraints sooner, while a missing local baseline would make failures harder to attribute.

Local-first delivery makes the initial proof small. It requires a runner contract that can survive migration to a remote worker. The [delivery proposal](../delivery-plan.md) stages that work and the [hosted runner RFC](../rfcs/002-hosted-runner.md) captures the remaining design choices.
