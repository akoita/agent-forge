<!--
Agent Forge is a harness-first project. Every PR must show evidence, not
intent. Fill in each section below — do not delete headings. Use "N/A" with a
one-line reason where a section genuinely does not apply.
See docs/roadmap.md and AGENTS.md for the quality gates enforced here.
-->

## What & why

What does this change do, and why now? Link the issue it closes.

Closes #<issue>

## Evidence

Paste the **actual commands you ran and their real output** — not a checklist.
At minimum:

```
# tests
$ make test-unit
...

# lint + types
$ make lint
...

# boundary ratchet
$ python scripts/check_boundaries.py
...
```

## Benchmark impact

How does this change move the M0 coding eval (success / cost / latency /
tool-error rate)? Paste the results delta, or write **N/A** with a reason
(benchmark harness tracked in #135).

## Boundary analysis

Does this touch `agent_forge/*` with domain concepts (audit / Proof-of-Audit
vocabulary)? The boundary check must be green and the baseline in
`scripts/boundary_baseline.txt` may only **shrink**, never grow. If you removed
a baseline entry, say which. If you added domain code, it belongs in
`plugins/`, not core.

## Docs

Which docs did you update in this branch (behaviour changes require doc updates
in the same PR)? Or state why none are needed.

## Rollback

How does a maintainer revert this safely if it misbehaves in production? Note
any migrations, state, or config that a plain `git revert` would not undo.

## ADR

Does this change architecture? If so, link the ADR under `docs/adr/`. Otherwise
state "no architectural change".
