# Cloud Deploy Extension Pack (`plugins/cloud_deploy/`)

Domain-isolated extension pack providing plan-before-apply tools, declarative `ApprovalGate` enforcement, strict `allowed_egress_domains` allowlists, and host-brokered OIDC/WIF helpers for GCP, AWS, and Terraform (`#226`).

## Usage

Load the bundled profiles directly with `--profiles-dir`:

```sh
agent-forge run \
  --profiles-dir plugins/cloud_deploy/profiles \
  --profile cloud-deploy-terraform \
  --task "Review and plan infrastructure changes in infra/"
```

| Profile ID | Aliases | Plan Tool (`requires_approval=False`) | Apply Tool (`requires_approval=True`) |
| --- | --- | --- | --- |
| `cloud-deploy-gcp` | `gcp-deploy`, `cloud-run-deploy` | `gcp_cloud_run_plan` | `gcp_cloud_run_deploy` |
| `cloud-deploy-aws` | `aws-deploy` | `aws_deploy_plan` | `aws_deploy_apply` |
| `cloud-deploy-terraform` | `terraform-deploy`, `tf-deploy` | `terraform_plan` | `terraform_apply` |

## Safety Controls

1. **Plan-before-apply gate (`PlanGateTracker`):** Every apply tool requires a valid, unconsumed `plan_id` matching the target and artifact/image from the corresponding plan step.
2. **Mandatory `ApprovalGate`:** All apply tools set `requires_approval = True` so headless or unattended runs cannot mutate cloud resources without interactive approval.
3. **Egress allowlists (`build_sandbox_egress_policy`):** Restricts outbound proxy domains to provider endpoints (`GCP_EGRESS_DOMAINS`, `AWS_EGRESS_DOMAINS`, `TERRAFORM_EGRESS_DOMAINS`).
4. **Host-brokered OIDC/WIF (`HostAuthBroker`):** Rejects static credential variables (`AWS_SECRET_ACCESS_KEY`, `GOOGLE_APPLICATION_CREDENTIALS`, embedded PEM keys) inside sandbox environments and caps ephemeral tokens at 3,600 seconds.
