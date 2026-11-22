---
description: Cut and publish a GitHub release when closing a delivery milestone
---

# Release Milestone Workflow

Run this checklist when a milestone reaches its exit criteria so users and observers receive a verified release and clear summary of changes.

## 1. Confirm milestone completion

Verify zero open issues remain in milestone `<M>`:

```bash
gh issue list --repo akoita/agent-forge --milestone <M> --state open
```

## 2. Bump package version via PR

Update `[project].version` in `pyproject.toml` and `__version__` in `agent_forge/__init__.py` to `<X.Y.Z>`, verify parity, and merge the PR to `main` with green CI:

```bash
python scripts/check_release.py
```

## 3. Close the GitHub milestone

```bash
gh api -X PATCH repos/akoita/agent-forge/milestones/<M> -f state=closed
```

## 4. Tag and push on `main`

From an up-to-date `main` checkout:

```bash
git pull --ff-only origin main
python scripts/check_release.py --tag v<X.Y.Z>
git tag -a v<X.Y.Z> -m "v<X.Y.Z> — <Milestone title>"
git push origin v<X.Y.Z>
```

`.github/workflows/release.yml` verifies tag parity, builds and smoke-tests the wheel, generates `SHA256SUMS.txt`, and publishes the GitHub release with artifacts attached.

## 5. Curate release notes

Confirm the workflow succeeded and edit the published release to highlight user-visible milestone outcomes and evidence:

```bash
gh run list --repo akoita/agent-forge --workflow release.yml --limit 3
gh release edit v<X.Y.Z> --repo akoita/agent-forge --title "v<X.Y.Z> — <Milestone title>" --notes-file -
```
