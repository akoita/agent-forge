# Agent Forge — Claude Code notes

Project conventions (mission, git authority boundaries, architecture, testing)
live in `AGENTS.md`. It is imported below and applies in full.

@AGENTS.md

## Claude-specific notes

- Run `make lint` and `make test-unit` before committing.
- Use git worktrees for parallel tasks so branches stay isolated.
- Follow `.agents/workflows/start-issue.md` and `.agents/workflows/finish-issue.md`.
- Never grow `scripts/boundary_baseline.txt` — it may only shrink.
- When a change affects architecture, write an ADR in `docs/adr/`.
