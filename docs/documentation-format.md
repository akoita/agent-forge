# Writing documentation for Agent Forge

Start with the question a contributor, CLI user, or service operator needs to answer. Choose the page that already owns the answer and update it alongside the change in behavior.

## Choose a page

| Reader’s question | Where to write | What to include |
| --- | --- | --- |
| How do I get started? | [README](../README.md) | Prerequisites, a first run, its expected result, and a link to the next task. |
| How do I perform a task? | [Testing](testing.md), [extensions](extending.md), or [hosted service](hosted-service.md) | Starting conditions, ordered steps, verification, and recovery. Include rollback instructions for deployment changes. |
| How do I configure a setting? | [Configuration](configuration.md) | Exact name, type, units, default, precedence, and a minimal example. Link to the definition when its details matter. |
| How does the system work? | [Architecture](architecture.md) or [technical specification](spec.md) | Component responsibilities, public contracts, execution flow, and failure behavior. Verify claims against the relevant entry point. |
| Why did we choose this design? | [Architecture decisions](adr/) | Context, status, decision, alternatives, and consequences. Link a superseding decision when the choice changes. |
| What will ship next? | [Roadmap](roadmap.md) and the tracking issue | User outcome, dependencies, and observable exit criteria. Label proposed behavior explicitly. |

## Format a Markdown page

Use one top-level heading. Open with the result the reader can achieve and any condition that changes how they should proceed. Give sections searchable titles such as “Recover an interrupted run.” Keep headings stable when other pages link to them.

Keep one idea per paragraph. Around four sentences or 120 words, look for a natural split. Use numbered lists for steps, bullets for independent choices, and tables for repeated comparisons. Add a short contents list to pages with more than four sections.

Use inline code for commands, paths, options, and configuration keys. Put copyable commands in fenced blocks with a language tag and omit the shell prompt. Name the working directory before the block and explain the expected result immediately afterward. Use relative links between repository pages.

## Example procedure: check the core boundary

Before proposing a change under `agent_forge/`, run the boundary checker with Python 3 from the repository root.

```sh
python3 scripts/check_boundaries.py
```

A passing run prints `boundary check OK` and exits with status 0. If the checker identifies a new violation, inspect the reported line. Move domain-specific behavior into its extension where appropriate, then rerun the command.

## Review before submitting

- Follow the procedure from the stated starting point. Check command names, paths, links, and expected output on the revision under review.
- Match each capability claim to its public entry point. For example, a provider adapter can pass unit tests while the CLI still cannot select it. Verify the CLI path before documenting that provider as available there.
- Keep proposed commands in the design or tracking issue until users can run them. For benchmark claims, record the revision, model, and evaluation conditions.
- Read the page as its intended reader. Can they recognize success, diagnose the first likely failure, and find their next step?
