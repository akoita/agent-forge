# Agent Forge delivery milestones

The [vision](vision.md) sets the user outcomes and the [strategy](strategy.md) defines how to measure them. The delivery sequence was planned on 2026-11-21. Track assignments in [GitHub milestones](https://github.com/akoita/agent-forge/milestones). The roadmap adoption is tracked in [#147](https://github.com/akoita/agent-forge/issues/147). Capacity is unconfirmed, so use exit criteria and re-estimate after the first local fix.

## Milestone sequence

| GitHub milestone and outcome | Issue order | Close when |
| --- | --- | --- |
| [1. Establish the verified-fix baseline](https://github.com/akoita/agent-forge/milestone/1). Establish evidence for a successful local correction and make provider selection observable. Reuse Sprint 1. | #135 deterministic slice → #139 first slice. In parallel, part of #42 wires existing adapters into normal entry points. | The pinned test fails on the base, passes with the known-good patch, and rejects empty and incorrect patches. PR CI runs the smoke check. The configured provider is used by the CLI, worker, and service. |
| [2. Ship a verified local fix](https://github.com/akoita/agent-forge/milestone/2). Deliver an isolated run that returns a tested patch. | After 1, #33 single-run slice → local delivery test. Add queue outcome ticket. Advance the model registry and qualification slice of #42 alongside the first correction. | A clean install produces a patch that applies to its declared base and passes independent checks. Existing user edits survive. Direct and queued runs agree on outcome. One contemporary model passes the qualification path. |
| [3. Use the agent on everyday work](https://github.com/akoita/agent-forge/milestone/3). Support interruption, review, and continuation on real repositories. | After 2, #136 and #29/#43 → bounded #138/#27 recovery and events. | A developer can cancel, inspect, and continue a real task from a known state. Multi-file failure leaves a consistent workspace. Budget enforcement and approvals are visible. |
| [4. Measure and choose a model default](https://github.com/akoita/agent-forge/milestone/4). Compare current coding models and one measured harness change. | Capture the baseline after 2, before changing the harness in 3. Finish #135 with the qualification slice of #42 and compare one candidate per provider. | Publish pinned task results, model settings, latency, verified fixes per attempt, and cost per verified fix including failures. A supported model can be added by configuration and qualified with the shared command. Document the selection. |
| [5. Operate the coding agent on GCP](https://github.com/akoita/agent-forge/milestone/5). Deliver the same verified result remotely with durable ownership and retrieval. | Once 2 fixes the outcome contract, coordinate #140/#141 → #26 → hosted durability → deployment canary. Check project access early. Carry the measured default from 4 into the release. | The CLI and hosted canary verify the same fixture. A caller can disconnect and retrieve the result. Duplicate dispatch and worker restart produce one authoritative outcome. Existing audit clients pass compatibility tests. |
| [6. Run an invite-only SaaS pilot](https://github.com/akoita/agent-forge/milestone/6). Let two developers submit and retrieve their own runs. | After 5, qualify the sandbox and implement the pilot portions of #41 and #28/#30. | Two real accounts complete the workflow. Isolation, quotas, cancellation, retention, and cleanup are verified. Capture feedback and correct one observed problem. |

## Carry-over and tracker changes

Milestone 1 reuses **Sprint 1 — Measure before build** and clears its elapsed July due date. The #140 extraction moves to milestone 5. Keep #135, #139, #42, #33, and #138 as parent trackers when their scope spans milestones. Assign their bounded children to the release that delivers each outcome.

Use #142 to track the hosted milestones and infrastructure work. Defer fleet scheduling, warm pools, billing, and full replay until after the pilot. Consider #24 and #137 when task traces show a concrete need. Leave provider expansion, MCP, ACP, and delegation outside the first release.

## New delivery issues

| Title | Milestone / repository | Acceptance |
| --- | --- | --- |
| [Adopt the vision, strategy, and delivery order](https://github.com/akoita/agent-forge/issues/147) | 1 / Agent Forge | Review the vision and strategy, update the canonical roadmap, reconcile #142, and link the superseding direction ADR to ADR-002. |
| [Preserve agent failure and timeout in queue mode](https://github.com/akoita/agent-forge/issues/152) | 2 / Agent Forge | A failed run returned without an exception cannot become a completed task. Status, events, run ID, and artifacts agree across entry points. |
| [Persist hosted runs and recover dispatch](https://github.com/akoita/agent-forge/issues/156) | 5 / Agent Forge | Store ownership, status, and idempotency keys. Simulate a failed dispatch, duplicate delivery, and expired lease. One result remains visible after an API and worker restart. |
| [Verify GCP access and deploy a coding canary](https://github.com/akoita/agent-forge-iac/issues/35) | 5 / agent-forge-iac | Record current project and federation evidence. Submit the authenticated fixture, download the patch, verify it independently, and record teardown. |
| [Qualify the hosted sandbox for shared use](https://github.com/akoita/agent-forge/issues/157) | 6 / Agent Forge | Verify independent workspaces, credential isolation, bounded resource use, cancellation, egress policy, and cleanup. Record the backend choice and supporting evidence. |

## Decisions to prepare

- Review [proposed ADR-003](adr/003-local-agent-and-hosted-pilot.md) as the successor to [ADR-002](adr/002-harness-first-reset.md) when the roadmap is accepted.
- Review the [provider RFC](rfcs/001-provider-qualification.md) during milestones 1–2 for the registry, continuation state, and qualification contract. Record the adopted interfaces after testing Gemini and a second protocol.
- Review the [hosted runner RFC](rfcs/002-hosted-runner.md) before committing to the API, durable job store, or sandbox backend. Record decisions after fixture parity, restart tests, and cloud isolation evidence.
