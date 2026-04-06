---
description: Finish work on a GitHub issue — verify, test, commit, push, PR, merge, clean up
---

# Finish Issue Workflow

When the user says "finish issue", "wrap up", or indicates the work is done, follow these steps **in order**:

## 1. Verify the branch

// turbo

- Run `git branch --show-current` to check the current branch
- If on `main`, identify the feature branch for the issue and switch to it
- If no feature branch exists, create one following the convention: `feat/<issue-number>-<short-kebab-description>` (or `fix/` for bugs)
  // turbo
- Run `git status` to see uncommitted changes

## 2. Verify implementation coverage

- **If an issue exists:** Fetch the issue details from GitHub using `issue_read` (owner: `akoita`, repo: `agent-forge`), re-read the acceptance criteria and scope, and review every modified/added file against the issue requirements. If anything is missing, implement it before proceeding.
- **If no issue:** Review the modified/added files to confirm the intended change is complete.

## 3. Ensure test coverage

- Identify all changed and new files: `git diff --name-only main`
- For each changed component/module, check if automated tests exist
- If tests are missing or outdated, create or update them
- Test files should follow the project's existing test conventions:
  - Unit tests in `tests/unit/`
  - Integration tests in `tests/integration/`
  - E2E tests in `tests/e2e/`

## 4. Run tests

- Run the full test suite: `make test`
- If any tests fail, fix the code or tests and re-run
- Do NOT proceed until all tests pass

## 5. Run linters

- Run `make lint` to check code quality
- Fix any lint errors before proceeding

## 6. Update documentation

- Review **all** documentation for relevance to the change:
  - `README.md` — usage examples, feature list, project structure
  - `spec.md` — technical specification, interface contracts
  - `docs/` — architecture, configuration, extending, testing guides
  - Inline docstrings in changed modules
- For each doc, decide whether it needs to be **updated**, **created**, or **removed**:
  - **Update** docs that describe changed behavior, CLI options, APIs, or architecture
  - **Create** new docs when introducing a feature, pattern, or component that users/developers need to understand
  - **Remove** or trim docs that describe deleted functionality or obsolete patterns
- Keep docs close to the code they describe — commit doc changes in the same branch
- Skip this step only if the change is trivial or purely internal refactoring with no user-facing impact

## 7. Clean commit(s)

- Review staged/unstaged changes: `git diff --cached` and `git diff`
- **Security check** — make sure NONE of these are committed:
  - `.env` files, API keys, secrets, tokens, private keys
  - **Hardcoded credentials in ANY file**
  - Large binary files, `__pycache__/`, build artifacts, `.venv/`
  - Database dumps, logs, local config overrides
- Check `.gitignore` covers suspicious files: `git status --ignored`
- If any sensitive files are tracked, add them to `.gitignore` first
- Make atomic, well-scoped commits:
  - **With issue:** `feat(#N): description` or `fix(#N): description`
  - **Without issue:** `feat: description` or `fix: description`
  - One logical change per commit — split if needed

## 8. Push the branch

// turbo

- Push to remote: `git push -u origin <branch-name>`
- Verify the push succeeded

## 9. Create PR

- Create a Pull Request targeting `main` with:
  - Title: concise description (referencing the issue number if one exists)
  - Body: summary of changes (+ `Closes #N` only if an issue exists)
- Verify the PR was created successfully before continuing

## 10. Verify PR CI passes and merge

- Check the PR's required status checks after the PR is open
- Prefer the repo's summary gate when configured: the `build` check must pass before merge
- **Poll until CI is conclusive:** if checks are pending, wait 30 seconds and re-check (repeat up to 10 times)
- If no CI checks are configured on the PR, **stop and ask the user** before proceeding
- If any required check fails, fix the issues locally, commit, push, and re-poll from the beginning
- **ONLY merge when the PR's required checks are green** (prefer squash merge for clean history)
- If CI fails on the PR, fix on the branch, push, and re-poll
- **NEVER merge a PR with pending or failing CI** — this is a hard stop

## 11. Verify main branch CI

- After merge, check that CI passes on the updated `main` branch
- If CI fails on main:
  - Create a fix branch: `fix/<issue-number>-<issue-title-kebab>-hotfix` (or `fix/<short-description>-hotfix` if no issue)
  - Fix the issue, push, create PR, merge
  - Repeat until main CI is green

## 12. Clean up branches

- Delete the feature branch remotely: `git push origin --delete <branch-name>`
- Delete the feature branch locally: `git branch -d <branch-name>`
- Delete any fix branches (remote + local) the same way
- **NEVER delete `main`**

## 13. Align local main

// turbo

- Switch to main: `git checkout main`
  // turbo
- Pull latest: `git pull origin main`
  // turbo
- Verify alignment: `git log --oneline -5`

## Important rules

- **NEVER push a file that contains clear private data** — no hardcoded credentials, API keys, passwords, private keys, or tokens in ANY file, regardless of file type. Scan every file before staging.
- **NEVER commit or push before user approval** — always ask first
- **NEVER force-push to `main`**
- **NEVER delete `main`** — only delete feature and fix branches
- **ALWAYS verify PR CI** before merging, and main CI after merging
- If in doubt about sensitive files, ask the user before committing
- If the merge creates conflicts, resolve them on the feature branch before merging
