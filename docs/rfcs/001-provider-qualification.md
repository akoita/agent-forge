# RFC 001 — Qualify models through shared provider adapters

**Status:** Accepted · 2026-11-23 (adopted via [ADR-009](../adr/009-provider-api-v2.md))

A contributor should be able to add a model on a supported protocol using configuration and one qualification command. The [strategy](../strategy.md#keep-model-qualification-repeatable) requires reproducible evidence before promoting a default. Track delivery in [#42](https://github.com/akoita/agent-forge/issues/42) and [#135](https://github.com/akoita/agent-forge/issues/135).

## Proposal

Use the existing provider factory in the CLI, worker, and hosted path. Describe each model with an API identifier, adapter, supported settings, and capabilities. Let that adapter handle protocol-specific continuation after tool calls. Keep the tool calls and their results in the common run record. Design resumable state with the checkpoint contract and record how state is scoped to the model and endpoint.

The proposed `agent-forge models validate` command should run a deterministic contract check, a budgeted live tool round trip, and an independently graded fix. Record which checks ran, their result, the requested model, its API-reported identity where provided, and usage. A qualification report should distinguish protocol failures from failed patches.

## Alternatives and decision evidence

A gateway could reduce the number of adapters maintained. It needs the same tests for continuation, model identity, usage, and error handling. Direct adapters expose native features and cost additional maintenance. Test the common contract with Gemini and a second protocol before choosing how to integrate additional providers.

Retain existing configuration while migrating the entry points. Use fixture parity and two live qualifications to review the interface. Record the adopted contract in an ADR after review.
