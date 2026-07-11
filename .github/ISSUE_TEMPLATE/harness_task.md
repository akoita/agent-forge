---
name: Harness task
about: Work item aligned with the harness-first roadmap
title: "[HARNESS] "
labels: direction/harness-first
assignees: ''
---

## Outcome

The concrete capability or behaviour that exists once this is done. Describe it
from the harness's point of view, not the implementation's.

## Exit criteria

Measurable, checkable conditions for "done". Prefer commands and observable
results over prose. Example:

- [ ] `python scripts/check_boundaries.py` stays green
- [ ] New behaviour covered by `tests/unit/test_*.py`
- [ ] ...

## Benchmark impact

How will the M0 coding eval detect that this succeeded (success rate, cost,
latency, tool-error rate)? If it cannot be measured yet, say so and explain the
interim signal.

## Boundary

Where does this live — core (`agent_forge/`), extension (`plugins/`), or cloud
(future `agent-forge-cloud`)? If it touches core, confirm it introduces no
audit-domain vocabulary (see AGENTS.md and `scripts/boundary_baseline.txt`).

## References

- Roadmap milestone: docs/roadmap.md (Mx — ...)
- Related issues: #...
