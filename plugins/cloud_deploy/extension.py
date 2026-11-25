"""Extension registration helpers for the cloud-deploy extension pack."""

from __future__ import annotations

from pathlib import Path

from agent_forge.extensions.discovery import ExtensionInfo


def get_profiles_dir() -> Path:
    """Return the directory containing cloud deployment YAML profiles."""
    return Path(__file__).resolve().parent / "profiles"


def get_prompt_fragment() -> str:
    """Return the shared system prompt safety rules for cloud deployment runs."""
    return (
        "## Cloud Deployment Safety Rules\n"
        "- Always execute plan/dry-run -> inspect diff -> request ApprovalGate confirmation "
        "before applying.\n"
        "- Never pass long-lived cloud keys or private-key blobs into the sandbox; use "
        "host-brokered OIDC/WIF.\n"
        "- Confine outbound sandbox traffic to declared provider endpoints via "
        "allowed_egress_domains."
    )


def get_extension_info() -> ExtensionInfo:
    """Return :class:`ExtensionInfo` metadata for ``plugins.cloud_deploy``."""
    return ExtensionInfo(
        name="cloud-deploy",
        version="0.1.0",
        description=(
            "Plan/apply gated cloud deployment extension pack for GCP, AWS, and Terraform."
        ),
        package="plugins.cloud_deploy",
        profiles=["cloud-deploy-gcp", "cloud-deploy-aws", "cloud-deploy-terraform"],
        tools=[
            "terraform_plan",
            "terraform_apply",
            "gcp_cloud_run_plan",
            "gcp_cloud_run_deploy",
            "aws_deploy_plan",
            "aws_deploy_apply",
        ],
        prompts=["cloud-deploy-safety"],
    )
