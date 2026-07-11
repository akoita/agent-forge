# Strategic Reset — Harness-First (July 2026)

**Status:** Plan of record. Approved direction from the July 2026 project audit.
**Decision record:** see `docs/adr/002-harness-first-reset.md`.
**Roadmap:** see `docs/roadmap.md` (canonical).

## Verdict

Agent Forge pivots from "broad agent platform" to a **small, measurable,
high-correctness coding harness**, targeting the capability bar of the first
releases of leading harnesses (Claude Code, Codex CLI, OpenHands, aider).

Verified findings that motivated the reset:

- Completion currently means "the model stopped calling tools"
  (`agent_forge/agent/core.py`), not "the requested change is correct and
  verified".
- E2E "fix" tests do not assert a patch was produced
  (`tests/e2e/test_pipeline_e2e.py`), and real-LLM tests accept failure.
- Audit-domain (Proof-of-Audit) vocabulary and schemas leak into six core
  modules (`agent/prompts.py`, `service/app.py`, `service/models.py`,
  `service/client.py`, `service/__init__.py`, `profiles/profile.py`),
  violating the repo's own domain-agnostic-core rule.
- The roadmap (spec §12) is stale (finished phases unchecked) and prioritizes
  breadth (dashboard, K8s, multi-agent, marketplace) over measured harness
  quality.
- Governance is contradictory: AGENTS.md demands explicit merge approval AND
  mandates autonomous merge; start-issue forbids committing before approval
  while finish-issue auto-merges.
- `docker-compose.yml` mounts the Docker socket (dev-only trust boundary);
  base deps include fastapi/uvicorn/google-cloud-storage.

## New direction (priority order)

1. **M0 — Evaluation before features.** Reproducible coding benchmark with
   real patch assertions, success/cost/latency/tool-error metrics, trajectory
   capture. No feature is "complete" without eval evidence.
2. **M1 — Agent-computer interface.** Atomic multi-file `apply_patch` with
   preconditions and rollback; resumable PTY shell sessions with polling,
   cancellation, and explicit truncation metadata.
3. **M2 — Context engine.** Hierarchical AGENTS.md/CLAUDE.md discovery,
   repository map, working-set management, compaction, prompt caching,
   durable summaries.
4. **M3 — Provider API v2.** Capability model (reasoning continuity, parallel
   tools, caching, structured output, stateful IDs); OpenAI adapter migrates
   to Responses; configurable OpenAI-compatible endpoints.
5. **M4 — Policy & approval engine.** Read-only / workspace-write / elevated
   policies; command classification; recorded approval decisions.
6. **M5 — Durable execution & verification-aware completion.** Step-level
   checkpoints, resume/replay, idempotency; completion contract (change
   exists, patch applies, checks ran, no unacknowledged failures).
7. **M6 — Standards interop.** MCP client first, ACP server for IDEs,
   worktree-isolated parallelism. Multi-agent only after M0 gate is credible.

**Out of core:** web dashboard, multi-tenant RBAC/billing/quotas, K8s
scheduling, Firecracker fleets, capacity dashboards, marketplace, custom A2A
protocol, orchestration DSL, opaque vector memory, bespoke IDE plugins.
SaaS lives in a future separate `agent-forge-cloud` project consuming the
core's runner contract. Proof-of-Audit moves entirely to
`plugins/proof-of-audit/` (installable extension).

## Work items (this reset)

- **W1 — Strategy docs:** `docs/roadmap.md` (new, canonical),
  `docs/adr/002-harness-first-reset.md`, `docs/spec.md` §12 replaced with a
  pointer, README direction blurb reconciled.
- **W2 — Governance & agent configs:** AGENTS.md rewritten (authority
  boundaries: autonomous on feature branches through PR; merge requires green
  CI + developer approval, standing approval allowed), new root CLAUDE.md
  importing AGENTS.md, `.codex/config.toml` (model "gpt-5.6",
  `model_reasoning_effort = "high"`), `.claude/settings.json` (secret-read
  denials, force-push protection), start/finish-issue workflows made
  consistent.
- **W3 — Enforcement:** `scripts/check_boundaries.py` (domain-vocabulary scan
  of `agent_forge/` with ratchet baseline of the six known violations +
  `# boundary-ok` pragma), unit tests, CI job, PR template requiring
  evidence/benchmark impact/boundary analysis/rollback, harness issue
  template.
- **W4 — Issue migration:** rewrite #24, #27, #29, #33, #40, #42, #46, #47;
  keep #32 (deferred) and #43 (elevated P0); close #23, #25, #26, #31, #34,
  #44, #45, #123 (superseded/not planned) and #28, #30, #35–#39, #41 (parked
  under the agent-forge-cloud umbrella issue); create new P0 issues for M0,
  patch layer, terminal protocol, durable execution, completion contract,
  PoA extraction, runner extraction.

## Quality gates going forward

- Eval evidence (or explicit N/A) required in every PR.
- Boundary check green in CI; baseline may only shrink.
- ADR required for architecture-affecting changes.
- Docs updated in the same branch as behavior changes.
