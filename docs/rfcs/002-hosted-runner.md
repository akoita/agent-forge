# RFC 002 — Deliver verified fixes through a shared hosted runner

**Status:** Proposed · 2026-11-21

A user should be able to submit a task and retrieve the same verified artifact they would receive locally. The service must retain run status across process restarts. This proposal supports the [hosted strategy](../strategy.md#deploy-in-stages) and the work in [#26](https://github.com/akoita/agent-forge/issues/26), [#141](https://github.com/akoita/agent-forge/issues/141), and [#142](https://github.com/akoita/agent-forge/issues/142).

## Proposal

Share the coding outcome contract across entry points. Return a base revision, patch, verification results, and terminal reason. Keep identity, run ownership, quotas, protected artifacts, and durable dispatch in the service. Preserve the existing report API through a compatibility adapter.

Use an owner-operated GCP deployment to run the local fixture remotely. Evaluate a managed API and worker with durable run state for the pilot. Compare execution backends on lifecycle support, cost, workspace isolation, credential handling, cancellation, and cleanup.

## Alternatives and decision evidence

The existing VM wiring can prove remote submission with less migration. A managed service may reduce operating work while requiring a different sandbox interface and explicit job recovery. Compare the same fixture across both paths where feasible.

Before a shared pilot, prove owner checks on run and artifact operations, duplicate dispatch recovery, API and worker restarts, and two-user workspace isolation. Record the runner contract and execution backend as separate ADRs after review.
