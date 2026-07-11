# Agent Forge Roadmap

**Status:** Canonical roadmap. Supersedes the phase roadmap formerly in
[`docs/spec.md` §12](spec.md#12-roadmap). Decision record:
[`docs/adr/002-harness-first-reset.md`](adr/002-harness-first-reset.md).
**Date:** 2026-07-11.

---

## Mission

Agent Forge is a small, measurable, high-correctness coding harness. It exists to
turn a coding task into a verified change, and to prove — with benchmarks — that it
does so reliably at a known cost and latency. We optimize for correctness and
verified task completion over feature breadth. The target bar is the capability of
the first public releases of Claude Code, Codex CLI, OpenHands, and aider: a core
that is small enough to reason about and good enough to measure.

## Principles

1. **Evaluation before features.** No feature is "complete" without benchmark
   evidence (or an explicit, justified N/A). We measure task success, cost,
   latency, and tool-error rate before we claim a capability works.
2. **Domain-agnostic core.** The core knows nothing about any specific domain.
   Domain work — for example Proof-of-Audit — lives in extensions, not in
   `agent_forge/`. The boundary is enforced mechanically by
   `scripts/check_boundaries.py` in CI, with a baseline that may only shrink.
3. **Simplicity is a feature.**
   [mini-SWE-agent](https://github.com/SWE-agent/mini-swe-agent) shows that a
   small, legible agent loop stays competitive. More framework does not make a
   better agent; every added abstraction must earn its place against a measured
   result.
4. **Standards before proprietary integrations.** We adopt open standards rather
   than build bespoke ones: [MCP](https://modelcontextprotocol.io) for tools,
   [ACP](https://agentclientprotocol.com/get-started/introduction) for IDE
   integration. We do not ship a custom agent-to-agent protocol or bespoke editor
   plugins when a standard covers the need.
5. **Verification-aware completion.** A run succeeds only when its completion
   contract is satisfied — the requested change exists, the patch applies, checks
   ran, and no failure is left unacknowledged. A run does not succeed merely
   because the model stopped calling tools.

---

## Milestones

Milestones are ordered by priority. The M0 evaluation gate comes first and gates
everything after it: subsequent milestones are only "done" once they show up as an
improvement (or a justified non-regression) in the eval harness.

### M0 — Evaluation harness & quality gate

**Outcome:** A reproducible coding benchmark that scores task success, cost,
latency, and tool-error rate from a clean checkout, with trajectory capture, so
every later milestone can be judged by evidence rather than assertion.

**Exit criteria:** `make eval` runs from a clean checkout and reports per-task
success/cost/latency/tool-error metrics with real patch assertions; a smoke subset
runs in CI on every PR.

**Tracking:** [#135](https://github.com/akoita/agent-forge/issues/135).

### M1 — Agent-computer interface

**Outcome:** A robust interface between the agent and the machine: atomic,
multi-file patches that either apply cleanly or roll back, and shell sessions the
agent can drive over long-running work without losing state or silently
truncating output.

**Exit criteria:**
- Atomic `apply_patch` with preconditions, rollback on failure, and diff
  manifests ([#136](https://github.com/akoita/agent-forge/issues/136)).
- Resumable PTY sessions with polling, cancellation, and explicit truncation
  metadata ([#137](https://github.com/akoita/agent-forge/issues/137)).

The SWE-agent agent-computer interface (ACI) result shows that interface design
materially affects agent performance, independent of the underlying model
([SWE-agent, arXiv:2405.15793](https://arxiv.org/abs/2405.15793)); this milestone
treats the interface as a first-class, measured surface.

### M2 — Context engine

**Outcome:** The agent assembles the right context for a task and keeps it within
budget across a long run, instead of dumping files into the prompt.

**Exit criteria:** Layered `AGENTS.md`/`CLAUDE.md` discovery; a repository map with
symbol ranking (precedent: [aider repository map](https://aider.chat/docs/repomap.html));
working-set management; compaction and stale tool-result pruning; prompt caching;
and durable summaries — landed and shown to help (or not hurt) on the M0 eval
([#24](https://github.com/akoita/agent-forge/issues/24)).

### M3 — Provider API v2

**Outcome:** A provider abstraction built around a capability model, so the harness
can exploit modern provider features rather than target a lowest common
denominator.

**Exit criteria:**
- Capability model covering reasoning continuity, parallel tool calls, prompt
  caching, structured output, and stateful response IDs; the OpenAI adapter
  migrates to the
  [Responses API](https://developers.openai.com/api/docs/guides/migrate-to-responses)
  ([#42](https://github.com/akoita/agent-forge/issues/42)).
- Configurable OpenAI-compatible endpoints (Ollama, vLLM, gateways)
  ([#46](https://github.com/akoita/agent-forge/issues/46)).

### M4 — Policy & approval engine

**Outcome:** Operator-controlled policies over what the agent may do, with a clear
record of every approval decision. Sandboxing (isolation) and approvals
(authorization) are treated as separate concerns.

**Exit criteria:**
- P0 policies: read-only, workspace-write, and elevated modes; command
  classification; and recorded approval decisions
  ([#43](https://github.com/akoita/agent-forge/issues/43)).
- Budget, pause, and cancellation policy
  ([#29](https://github.com/akoita/agent-forge/issues/29)).

### M5 — Durable execution & verification-aware completion

**Outcome:** Runs that survive interruption and that only report success when a
completion contract is satisfied.

**Exit criteria:**
- Step-level checkpoints with resume, replay, and idempotency
  ([#138](https://github.com/akoita/agent-forge/issues/138)).
- A completion contract: the change exists, the patch applies, checks ran, and no
  failure is left unacknowledged
  ([#139](https://github.com/akoita/agent-forge/issues/139)).
- Typed run-event streaming over a durable transport
  ([#27](https://github.com/akoita/agent-forge/issues/27)).

### M6 — Standards interop & parallelism

**Outcome:** Interoperation with the wider ecosystem through open standards, and
safe parallel execution.

**Exit criteria:**
- MCP client first ([#40](https://github.com/akoita/agent-forge/issues/40)).
- ACP server for IDE integration
  ([#47](https://github.com/akoita/agent-forge/issues/47)).
- Worktree-isolated parallel execution
  ([#33](https://github.com/akoita/agent-forge/issues/33)).

Hierarchical delegation ([#32](https://github.com/akoita/agent-forge/issues/32))
stays deferred until the M0 gate is credible: multi-agent orchestration is only
worth building once we can measure whether it helps.

### Cross-cutting — extraction

**Outcome:** The core becomes genuinely domain-agnostic and dependency-light.

**Exit criteria:**
- Proof-of-Audit moved out of core into an installable extension
  ([#140](https://github.com/akoita/agent-forge/issues/140)).
- A generic runner; `fastapi`/`uvicorn`/`google-cloud-storage` moved from base
  dependencies to extras ([#141](https://github.com/akoita/agent-forge/issues/141)).

---

## Out of core

The following are explicitly out of scope for the core harness. Hosted-service
concerns move to the future `agent-forge-cloud` project, parked under the umbrella
issue [#142](https://github.com/akoita/agent-forge/issues/142):

- Web dashboard and run-history UI.
- Multi-tenant auth, RBAC, billing, and quotas.
- Kubernetes scheduling, Firecracker fleets, warm pools, and capacity dashboards.
- A community tool marketplace.

Also not planned, in core or cloud:

- A custom agent-to-agent protocol (use standards).
- An orchestration DSL.
- Opaque vector memory as a default context mechanism.
- Bespoke VS Code / JetBrains plugins — superseded by the ACP server (M6).

---

## SaaS split

The line between the open-source core and the future hosted product:

| Agent Forge core (local & embeddable)          | agent-forge-cloud (separate deployable) | Extensions (installable packages)      |
| ---------------------------------------------- | --------------------------------------- | -------------------------------------- |
| Agent loop                                     | Tenancy                                 | Domain prompts                         |
| Tools                                          | Auth / RBAC                             | Report schemas                         |
| Policies                                       | Billing                                 | Profiles                               |
| Context engine                                 | Quotas                                  | Domain tools                           |
| Events                                         | Scheduler                               |                                        |
| Sandbox interface                              | Sandbox fleet                           |                                        |
| Runner protocol                                | Artifact storage                        |                                        |
|                                                | UI                                      |                                        |

The core is a library and CLI that runs locally and embeds into other programs.
`agent-forge-cloud` is a separate deployable that consumes the core's runner
contract and adds the multi-tenant, hosted concerns. Extensions are installable
packages that add domain behavior on top of the core.

---

## References

- [mini-SWE-agent](https://github.com/SWE-agent/mini-swe-agent) — a small,
  competitive agent loop.
- [SWE-agent (arXiv:2405.15793)](https://arxiv.org/abs/2405.15793) — the
  agent-computer interface result.
- [aider repository map](https://aider.chat/docs/repomap.html) — repository map
  with symbol ranking.
- [OpenAI: migrate to the Responses API](https://developers.openai.com/api/docs/guides/migrate-to-responses).
- [Model Context Protocol (MCP)](https://modelcontextprotocol.io).
- [Agent Client Protocol (ACP)](https://agentclientprotocol.com/get-started/introduction).
- [OpenHands SDK capability index](https://github.com/OpenHands/docs/blob/main/llms.txt).
- [Claude Code security](https://code.claude.com/docs/en/security).
- [Running Codex safely](https://openai.com/index/running-codex-safely/).
