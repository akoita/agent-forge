# ADR 004 — Verify a coding patch before reporting completion

**Status:** Proposed · 2026-11-21

## Context

A model can stop after describing a fix without producing one. A test run in the
agent's workspace can also depend on files that the exported patch omits.
The harness needs evidence that a coding change applies to its declared base
and passes a check chosen by the caller. Investigation tasks have a different
completion contract.

## Decision for review

Add an explicit verified-fix mode for direct CLI runs. Give the model an
editable checkout at the source repository's committed revision. Capture the
patch against that revision using separate Git metadata. Apply it in another
checkout and execute the caller's check in a fresh sandbox with egress disabled.
Set the final state after verification and persist the patch, revision, and
check result.

## Alternatives and consequences

A check in the agent workspace is cheaper and can use its build state.
Independent application exposes missing or nonportable changes before delivery.
Opt-in verification preserves the current interface for investigations and
extension clients. The check requires a committed Git revision and a sandbox
image with its dependencies installed. Queue and hosted integration can reuse
the completion callback when those entry points support the same inputs.

## Verification

Exercise the CLI with an empty change, a failing check, and a passing check.
Confirm that the resulting patch reaches a new checkout and that the original
working tree retains local edits. Track the workflow in
[#150](https://github.com/akoita/agent-forge/issues/150).
