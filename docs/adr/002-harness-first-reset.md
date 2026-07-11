# ADR-002: Harness-First Strategic Reset

**Status:** Accepted  
**Date:** 2026-07-11  
**Context:** [Strategic Reset plan](../../.agents/plans/strategic-reset-2026-07.md), [roadmap.md](../roadmap.md), [spec.md § 12](../spec.md#12-roadmap)

---

## Context

Agent Forge began as a broad "agent platform": a coding agent plus a widening
surface of hosted-service, multi-tenant, and scaling ambitions. A July 2026 audit
of the codebase and roadmap found that this breadth had outrun the core's measured
quality. The verified findings:

- **Completion means "stopped", not "correct".** A run is treated as complete when
  the model stops calling tools (`agent_forge/agent/core.py`), not when the
  requested change is present, applies, and is verified.
- **Tests do not assert the work happened.** The end-to-end "fix" tests do not
  assert that a patch was produced (`tests/e2e/test_pipeline_e2e.py`), and the
  real-LLM tests accept failure as a passing outcome.
- **Domain vocabulary has leaked into the core.** Proof-of-Audit (PoA) vocabulary
  and schemas appear in six core modules — `agent/prompts.py`, `service/app.py`,
  `service/models.py`, `service/client.py`, `service/__init__.py`, and
  `profiles/profile.py` — violating the repo's own domain-agnostic-core rule.
- **The roadmap is stale.** The spec §12 phase list leaves finished phases
  unchecked and prioritizes breadth (web dashboard, Kubernetes, multi-agent,
  marketplace) over measured harness quality.
- **Governance is contradictory.** AGENTS.md simultaneously demands explicit merge
  approval and mandates autonomous merge; the start-issue workflow forbids
  committing before approval while the finish-issue workflow auto-merges.
- **Trust boundary and dependency weight.** `docker-compose.yml` mounts the Docker
  socket (a dev-only trust boundary presented without that caveat), and the base
  dependencies include `fastapi`, `uvicorn`, and `google-cloud-storage` — hosted
  concerns carried by every install.

Taken together, the project claimed capabilities it did not measure and carried
scope it could not yet justify.

## Decision

**Agent Forge pivots to a harness-first strategy: a small, measurable,
high-correctness coding harness**, targeting the capability bar of the first
public releases of Claude Code, Codex CLI, OpenHands, and aider. The
[roadmap](../roadmap.md) is canonical and reorganizes the work into milestones
M0–M6.

Concretely:

1. **Evaluation gate before features.** M0 builds a reproducible eval harness
   (`make eval`) with real patch assertions and success/cost/latency/tool-error
   metrics. No feature is "complete" without eval evidence, or an explicit,
   justified N/A.
2. **Proof-of-Audit extracted from core.** PoA moves entirely to
   `plugins/proof-of-audit/` as an installable extension. The core becomes
   domain-agnostic in fact, not just in aspiration.
3. **SaaS in a separate project.** Hosted, multi-tenant, and scaling concerns move
   to a future `agent-forge-cloud` project that consumes the core's runner
   contract. The core ships as a local, embeddable library and CLI.
4. **Standards-first interop.** MCP for tools and ACP for IDE integration, rather
   than bespoke agent-to-agent protocols or editor plugins.
5. **Verification-aware completion.** A run succeeds only when its completion
   contract is satisfied — the change exists, the patch applies, checks ran, and
   no failure is left unacknowledged.
6. **Governance made non-contradictory, with mechanical gates.** The agent
   configuration is rewritten so authority boundaries are consistent (autonomous
   through PR; merge requires green CI plus developer approval). Quality is
   enforced mechanically rather than by convention: a boundary check
   (`scripts/check_boundaries.py`) runs in CI with a baseline that may only
   shrink, PRs must carry eval/benchmark evidence, and architecture-affecting
   changes require an ADR.

## Consequences

### Positive

- **Measurable quality.** Every capability is judged against an eval, so claims in
  the README and docs are backed by evidence.
- **Smaller, legible core.** Removing PoA and hosted dependencies shrinks the core
  and makes it easier to reason about and to embed.
- **Credible open-source positioning.** A focused harness that competes on
  correctness is a clearer, more defensible story than a broad platform.
- **Non-contradictory governance.** Consistent authority boundaries and mechanical
  gates remove the ambiguity that previously stalled or corrupted the workflow.

### Negative / accepted costs

- **Issue backlog churn.** The reset rewrites, defers, and closes a large number of
  existing issues; contributors tracking the old roadmap must re-orient.
- **Hosted-service scope freeze.** Web dashboard, RBAC, billing, Kubernetes, and
  fleet scaling are frozen out of core and deferred to `agent-forge-cloud`
  (umbrella issue #142); users wanting those capabilities wait for that project.
- **Benchmark maintenance burden.** An eval gate is only useful if it is
  maintained; the project takes on the ongoing cost of keeping benchmarks
  reproducible and meaningful.
- **PoA users must install the extension.** Proof-of-Audit stops shipping in the
  core, so existing PoA users must install `plugins/proof-of-audit/` explicitly.
