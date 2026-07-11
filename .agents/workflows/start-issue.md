---
description: Start working on a GitHub issue — create branch, track work, open PR
---

# Start Issue Workflow

When the user says "start issue #N", "work on #N", or references working on a specific GitHub issue, follow these steps:

## 1. Read the issue

- Fetch the issue details from GitHub using `issue_read` (owner: `akoita`, repo: `agent-forge`)
- Understand the acceptance criteria and scope

## 2. Create a feature branch

// turbo

- Branch naming convention: `feat/<issue-number>-<short-kebab-description>`
  - Example: `feat/12-gemini-adapter`
  - For bugs: `fix/<issue-number>-<short-kebab-description>`
- Create the branch from `main` using: `git checkout -b feat/<issue-number>-<short-description>`
- Verify you're on the new branch: `git branch --show-current`

## 3. Update the issue

- Add a comment on the issue: "🚧 Work started on branch `feat/<issue-number>-...`"
- If not already labeled, add the `In Progress` label

## 4. Plan the work

- Create an implementation plan artifact in `.agents/plans/` for non-trivial issues
- Proceed once the plan is written; request review only when requirements are ambiguous

## 5. Commit conventions

- All commits on the branch should reference the issue: `feat(#N): description` or `fix(#N): description`
- Make atomic commits — one logical change per commit

## 6. When work is complete

- Push the branch
- Open a Pull Request targeting `main` with:
  - Title: same as branch purpose, referencing the issue
  - Body: summary of changes + `Closes #N`
  - Link back to the issue
- Request user review

## Important rules

- **Commits and pushes on the feature branch are autonomous** — no per-commit approval is needed
- **NEVER push to `main`** — always use a feature branch; the merge gate is defined in `AGENTS.md`
- **NEVER commit directly to `main`** — always use a feature branch
- **ALWAYS check current branch** before starting work with `git branch --show-current`
- If already on a feature branch for the issue, continue working there — don't create a new one
- If on a different branch, stash or commit current work before switching
