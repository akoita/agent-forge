# Issue 130 Implementation Plan

Issue: `#130` - auto-deploy to dev environment on merge to `main`

Branch: `feat/130-auto-deploy-dev`

## Scope in this repo

This branch covers the application-repo work only:

1. Add a GitHub Actions workflow at `.github/workflows/deploy-dev.yml`
2. Support both automatic deploys on push to `main` and manual deploys via `workflow_dispatch`
3. Authenticate to GCP with Workload Identity Federation using GitHub environment secrets
4. SSH to the dev VM and invoke the deploy script with the target ref
5. Verify service health after deploy
6. Document the required GitHub environment setup and runtime expectations

## Dependencies outside this repo

This workflow depends on infrastructure work in `agent-forge-iac`:

1. `/opt/agent-forge-deploy.sh` must exist on the VM
2. The GitHub deploy identity must have OS Login / SSH access to the VM
3. The WIF provider must allow `repo:akoita/agent-forge:*`

If any of those are not in place yet, the workflow can still be implemented here, but live deployment validation will remain blocked until the IaC side is merged and applied.

## Proposed implementation

### 1. Add deploy workflow

Create `.github/workflows/deploy-dev.yml` with:

1. Triggers:
   - `push` on `main`
   - `workflow_dispatch` with optional `ref`
2. Permissions:
   - `contents: read`
   - `id-token: write`
3. A single `deploy` job targeting the `dev` GitHub environment
4. GCP auth via `google-github-actions/auth`
5. Cloud SDK setup via `google-github-actions/setup-gcloud`
6. Remote deploy call via `gcloud compute ssh`
7. Post-deploy health verification via remote `curl` against `http://localhost:8080/healthz`

### 2. Keep config out of the workflow body

Use GitHub environment or repository variables for environment-dependent values where appropriate:

1. Project ID
2. Zone
3. VM name

Use environment secrets for:

1. `GCP_WORKLOAD_IDENTITY_PROVIDER`
2. `GCP_SERVICE_ACCOUNT`

### 3. Make the workflow robust

Include:

1. A deterministic ref selection rule:
   - `workflow_dispatch` uses the provided `ref` when present
   - otherwise deploy the triggering SHA
2. Strict shell settings in `run` steps
3. A short post-deploy wait before health verification
4. Clear step names and logs for debugging

### 4. Document the setup

Update docs to cover:

1. Required `dev` GitHub environment secrets
2. Any repo or environment variables needed by the workflow
3. The dependency on the IaC-side VM deploy script and WIF configuration
4. Manual dispatch usage for deploying a specific ref

## Validation plan

Local validation in this repo:

1. Lint the workflow YAML and check formatting
2. Review the workflow for secret handling and hardcoded environment values
3. Confirm docs match the workflow contract

Runtime validation after merge or once IaC is ready:

1. Manual dispatch to a known ref
2. Automatic deploy on merge to `main`
3. Confirm `/healthz` returns `200`

## Open questions to confirm before coding

1. Should VM metadata such as project / zone / instance name live in GitHub environment variables or remain inline in the workflow?
2. Do we want the workflow to fail fast when the optional `ref` does not resolve on the VM, or should the remote script own all ref validation?
3. Should docs for the required GitHub environment live in `docs/hosted-service.md`, a new deployment doc, or both?
