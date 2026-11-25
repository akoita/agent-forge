"""Plan/dry-run and approval-gated apply tools for GCP, AWS, and Terraform."""

from __future__ import annotations

import posixpath
import re
import shlex
import time
from typing import TYPE_CHECKING, Any

from agent_forge.tools.base import Tool, ToolResult, validate_path
from plugins.cloud_deploy.policy import PlanGateTracker, default_tracker

if TYPE_CHECKING:
    from agent_forge.sandbox.base import Sandbox

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}$")
_TERRAFORM_PLAN_FILENAME = ".agent_forge_tfplan"


def _validate_identifier(value: object, label: str) -> str:
    """Validate safe CLI identifier tokens without shell metacharacters or whitespace."""
    if not isinstance(value, str):
        msg = f"Missing required string argument: {label}"
        raise ValueError(msg)
    cleaned = value.strip()
    if not _IDENTIFIER_RE.fullmatch(cleaned):
        msg = (
            f"Invalid {label} '{value}': allowed characters are letters, digits, "
            "and '._:/@-' with no whitespace or shell operators."
        )
        raise ValueError(msg)
    return cleaned


def _validate_workspace_dir(raw: object, *, default: str = "/workspace") -> str:
    """Normalize and confine *raw* directory inside ``/workspace``."""
    candidate = default if raw is None else str(raw).strip()
    if not candidate:
        candidate = default
    return validate_path(candidate)


def _format_exec_output(stdout: str, stderr: str) -> str:
    parts = [part.strip() for part in (stdout, stderr) if part and part.strip()]
    return "\n".join(parts)


class TerraformPlanTool(Tool):
    """Run ``terraform plan`` and record an inspectable plan artifact inside ``/workspace``."""

    def __init__(self, tracker: PlanGateTracker | None = None) -> None:
        self._tracker = tracker or default_tracker()

    @property
    def name(self) -> str:
        return "terraform_plan"

    @property
    def description(self) -> str:
        return (
            "Run a read-only 'terraform plan' in a workspace directory and record a plan_id "
            "required before calling 'terraform_apply'."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "working_dir": {
                    "type": "string",
                    "description": "Directory under /workspace containing Terraform configs.",
                    "default": "/workspace",
                },
                "var_file": {
                    "type": "string",
                    "description": "Optional .tfvars path under /workspace.",
                },
            },
        }

    @property
    def side_effecting(self) -> bool:
        return False

    async def execute(self, arguments: dict[str, Any], sandbox: Sandbox) -> ToolResult:
        """Execute ``terraform plan`` and record a :class:`PlanRecord` on success."""
        started = time.monotonic()
        try:
            work_dir = _validate_workspace_dir(arguments.get("working_dir"))
            raw_var = arguments.get("var_file")
            var_file = validate_path(str(raw_var)) if raw_var else None
        except ValueError as exc:
            return ToolResult(output="", error=str(exc), exit_code=1)

        plan_file = posixpath.join(work_dir, _TERRAFORM_PLAN_FILENAME)
        var_flag = f" -var-file={shlex.quote(var_file)}" if var_file else ""
        cmd = (
            f"cd {shlex.quote(work_dir)} && "
            f"terraform plan -input=false -no-color{var_flag} -out={shlex.quote(plan_file)}"
        )
        res = await sandbox.exec(cmd, timeout_seconds=120)
        combined = _format_exec_output(res.stdout, res.stderr)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if res.exit_code != 0:
            return ToolResult(
                output=combined,
                error=f"terraform plan failed with exit code {res.exit_code}",
                exit_code=res.exit_code,
                execution_time_ms=elapsed_ms,
            )

        rec = self._tracker.record(
            "terraform",
            work_dir,
            combined[:2000],
            plan_artifact=plan_file,
        )
        header = f"[plan_id={rec.plan_id} working_dir={work_dir} plan_file={plan_file}]"
        output = f"{header}\n{combined}".strip()
        return ToolResult(
            output=output,
            exit_code=0,
            execution_time_ms=elapsed_ms,
            metadata={"plan_id": rec.plan_id, "plan_file": plan_file, "working_dir": work_dir},
        )


class TerraformApplyTool(Tool):
    """Apply a previously inspected Terraform plan behind ``ApprovalGate``."""

    def __init__(self, tracker: PlanGateTracker | None = None) -> None:
        self._tracker = tracker or default_tracker()

    @property
    def name(self) -> str:
        return "terraform_apply"

    @property
    def description(self) -> str:
        return (
            "Apply a previously inspected Terraform plan ('plan_id') under explicit "
            "ApprovalGate confirmation."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "plan_id": {
                    "type": "string",
                    "description": "Identifier returned by 'terraform_plan'.",
                },
                "working_dir": {
                    "type": "string",
                    "description": "Matching directory under /workspace passed to terraform_plan.",
                    "default": "/workspace",
                },
            },
            "required": ["plan_id"],
        }

    @property
    def requires_approval(self) -> bool:
        return True

    @property
    def approval_category(self) -> str:
        return "cloud_deploy_terraform"

    def approval_summary(self, arguments: dict[str, Any]) -> str:
        """Describe the Terraform directory and plan identifier awaiting approval."""
        work_dir = arguments.get("working_dir", "/workspace")
        plan_id = arguments.get("plan_id", "<missing>")
        return f"Apply Terraform plan '{plan_id}' in '{work_dir}'."

    async def execute(self, arguments: dict[str, Any], sandbox: Sandbox) -> ToolResult:
        """Validate *plan_id* matches *working_dir* and apply the saved plan."""
        started = time.monotonic()
        try:
            work_dir = _validate_workspace_dir(arguments.get("working_dir"))
            plan_id = str(arguments.get("plan_id") or "")
            rec = self._tracker.require(plan_id, provider="terraform", target=work_dir)
        except ValueError as exc:
            return ToolResult(output="", error=str(exc), exit_code=1)

        plan_file = rec.plan_artifact or posixpath.join(work_dir, _TERRAFORM_PLAN_FILENAME)
        cmd = (
            f"cd {shlex.quote(work_dir)} && "
            f"terraform apply -input=false -no-color -auto-approve {shlex.quote(plan_file)}"
        )
        res = await sandbox.exec(cmd, timeout_seconds=180)
        combined = _format_exec_output(res.stdout, res.stderr)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if res.exit_code != 0:
            return ToolResult(
                output=combined,
                error=f"terraform apply failed with exit code {res.exit_code}",
                exit_code=res.exit_code,
                execution_time_ms=elapsed_ms,
            )

        self._tracker.consume(rec.plan_id)
        return ToolResult(
            output=combined,
            exit_code=0,
            execution_time_ms=elapsed_ms,
            metadata={"plan_id": rec.plan_id, "applied": True, "working_dir": work_dir},
        )


class GcpCloudRunPlanTool(Tool):
    """Inspect a Cloud Run service configuration before gating deployment."""

    def __init__(self, tracker: PlanGateTracker | None = None) -> None:
        self._tracker = tracker or default_tracker()

    @property
    def name(self) -> str:
        return "gcp_cloud_run_plan"

    @property
    def description(self) -> str:
        return (
            "Inspect target GCP Cloud Run state and record a plan_id required before calling "
            "'gcp_cloud_run_deploy'."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "GCP project ID."},
                "region": {"type": "string", "description": "Cloud Run region."},
                "service": {"type": "string", "description": "Cloud Run service name."},
                "image": {"type": "string", "description": "Target Artifact Registry image URI."},
            },
            "required": ["project", "region", "service", "image"],
        }

    @property
    def side_effecting(self) -> bool:
        return False

    async def execute(self, arguments: dict[str, Any], sandbox: Sandbox) -> ToolResult:
        """Describe the current Cloud Run service and record an inspectable deployment plan."""
        started = time.monotonic()
        try:
            project = _validate_identifier(arguments.get("project"), "project")
            region = _validate_identifier(arguments.get("region"), "region")
            service = _validate_identifier(arguments.get("service"), "service")
            image = _validate_identifier(arguments.get("image"), "image")
        except ValueError as exc:
            return ToolResult(output="", error=str(exc), exit_code=1)

        cmd = (
            f"gcloud run services describe {shlex.quote(service)} "
            f"--project {shlex.quote(project)} "
            f"--region {shlex.quote(region)} "
            "--format yaml || true"
        )
        res = await sandbox.exec(cmd, timeout_seconds=60)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if res.exit_code != 0:
            return ToolResult(
                output=_format_exec_output(res.stdout, res.stderr),
                error=f"gcloud describe failed with exit code {res.exit_code}",
                exit_code=res.exit_code,
                execution_time_ms=elapsed_ms,
            )

        target = f"{project}/{region}/{service}"
        details = _format_exec_output(res.stdout, res.stderr) or "(new service)"
        summary = f"Target: {target}\nPlanned image: {image}\nCurrent state:\n{details}"
        rec = self._tracker.record("gcp", target, summary, fingerprint=image)
        return ToolResult(
            output=f"[plan_id={rec.plan_id}]\n{summary}",
            exit_code=0,
            execution_time_ms=elapsed_ms,
            metadata={"plan_id": rec.plan_id, "target": target, "image": image},
        )


class GcpCloudRunDeployTool(Tool):
    """Deploy a container revision to Cloud Run behind ``ApprovalGate``."""

    def __init__(self, tracker: PlanGateTracker | None = None) -> None:
        self._tracker = tracker or default_tracker()

    @property
    def name(self) -> str:
        return "gcp_cloud_run_deploy"

    @property
    def description(self) -> str:
        return (
            "Deploy a planned Cloud Run revision ('plan_id') under explicit ApprovalGate control."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "plan_id": {
                    "type": "string",
                    "description": "Identifier returned by 'gcp_cloud_run_plan'.",
                },
                "project": {"type": "string", "description": "GCP project ID."},
                "region": {"type": "string", "description": "Cloud Run region."},
                "service": {"type": "string", "description": "Cloud Run service name."},
                "image": {"type": "string", "description": "Artifact Registry image URI."},
            },
            "required": ["plan_id", "project", "region", "service", "image"],
        }

    @property
    def requires_approval(self) -> bool:
        return True

    @property
    def approval_category(self) -> str:
        return "cloud_deploy_gcp"

    def approval_summary(self, arguments: dict[str, Any]) -> str:
        """Render Cloud Run service target, image, and plan ID awaiting approval."""
        return (
            f"Deploy Cloud Run service '{arguments.get('service', '<missing>')}' "
            f"in '{arguments.get('project', '?')}/{arguments.get('region', '?')}' "
            f"(image={arguments.get('image', '<missing>')}, "
            f"plan_id={arguments.get('plan_id', '<missing>')})."
        )

    async def execute(self, arguments: dict[str, Any], sandbox: Sandbox) -> ToolResult:
        """Verify matching Cloud Run plan & image, then run ``gcloud run deploy``."""
        started = time.monotonic()
        try:
            project = _validate_identifier(arguments.get("project"), "project")
            region = _validate_identifier(arguments.get("region"), "region")
            service = _validate_identifier(arguments.get("service"), "service")
            image = _validate_identifier(arguments.get("image"), "image")
            target = f"{project}/{region}/{service}"
            plan_id = str(arguments.get("plan_id") or "")
            rec = self._tracker.require(plan_id, provider="gcp", target=target)
            if rec.fingerprint != image:
                msg = (
                    f"Plan '{rec.plan_id}' inspected image '{rec.fingerprint}', "
                    f"refusing mismatched deploy image '{image}'."
                )
                raise ValueError(msg)
        except ValueError as exc:
            return ToolResult(output="", error=str(exc), exit_code=1)

        cmd = (
            f"gcloud run deploy {shlex.quote(service)} "
            f"--project {shlex.quote(project)} "
            f"--region {shlex.quote(region)} "
            f"--image {shlex.quote(image)} --quiet"
        )
        res = await sandbox.exec(cmd, timeout_seconds=180)
        combined = _format_exec_output(res.stdout, res.stderr)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if res.exit_code != 0:
            return ToolResult(
                output=combined,
                error=f"gcloud run deploy failed with exit code {res.exit_code}",
                exit_code=res.exit_code,
                execution_time_ms=elapsed_ms,
            )

        self._tracker.consume(rec.plan_id)
        return ToolResult(
            output=combined,
            exit_code=0,
            execution_time_ms=elapsed_ms,
            metadata={"plan_id": rec.plan_id, "applied": True, "target": target},
        )


class AwsDeployPlanTool(Tool):
    """Create a dry-run AWS CloudFormation change set before gated deployment."""

    def __init__(self, tracker: PlanGateTracker | None = None) -> None:
        self._tracker = tracker or default_tracker()

    @property
    def name(self) -> str:
        return "aws_deploy_plan"

    @property
    def description(self) -> str:
        return (
            "Generate an AWS CloudFormation dry-run changeset (--no-execute-changeset) and "
            "record a plan_id required before 'aws_deploy_apply'."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "stack_name": {"type": "string", "description": "CloudFormation stack name."},
                "region": {"type": "string", "description": "AWS region identifier."},
                "template_file": {
                    "type": "string",
                    "description": "Template file path under /workspace.",
                },
            },
            "required": ["stack_name", "region", "template_file"],
        }

    @property
    def side_effecting(self) -> bool:
        return False

    async def execute(self, arguments: dict[str, Any], sandbox: Sandbox) -> ToolResult:
        """Create a dry-run AWS changeset and record a :class:`PlanRecord`."""
        started = time.monotonic()
        try:
            stack_name = _validate_identifier(arguments.get("stack_name"), "stack_name")
            region = _validate_identifier(arguments.get("region"), "region")
            template_file = validate_path(str(arguments.get("template_file") or ""))
        except ValueError as exc:
            return ToolResult(output="", error=str(exc), exit_code=1)

        cmd = (
            "aws cloudformation deploy "
            f"--stack-name {shlex.quote(stack_name)} "
            f"--region {shlex.quote(region)} "
            f"--template-file {shlex.quote(template_file)} "
            "--no-execute-changeset"
        )
        res = await sandbox.exec(cmd, timeout_seconds=120)
        combined = _format_exec_output(res.stdout, res.stderr)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if res.exit_code != 0:
            return ToolResult(
                output=combined,
                error=f"aws changeset plan failed with exit code {res.exit_code}",
                exit_code=res.exit_code,
                execution_time_ms=elapsed_ms,
            )

        target = f"{region}/{stack_name}"
        rec = self._tracker.record(
            "aws",
            target,
            combined[:2000],
            plan_artifact=template_file,
        )
        return ToolResult(
            output=f"[plan_id={rec.plan_id} target={target} template={template_file}]\n{combined}",
            exit_code=0,
            execution_time_ms=elapsed_ms,
            metadata={"plan_id": rec.plan_id, "target": target, "template_file": template_file},
        )


class AwsDeployApplyTool(Tool):
    """Apply an inspected AWS CloudFormation stack deployment behind ``ApprovalGate``."""

    def __init__(self, tracker: PlanGateTracker | None = None) -> None:
        self._tracker = tracker or default_tracker()

    @property
    def name(self) -> str:
        return "aws_deploy_apply"

    @property
    def description(self) -> str:
        return (
            "Deploy a planned AWS CloudFormation stack ('plan_id') under explicit "
            "ApprovalGate confirmation."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "plan_id": {
                    "type": "string",
                    "description": "Identifier returned by 'aws_deploy_plan'.",
                },
                "stack_name": {"type": "string", "description": "CloudFormation stack name."},
                "region": {"type": "string", "description": "AWS region identifier."},
                "template_file": {
                    "type": "string",
                    "description": "Template file path under /workspace.",
                },
            },
            "required": ["plan_id", "stack_name", "region", "template_file"],
        }

    @property
    def requires_approval(self) -> bool:
        return True

    @property
    def approval_category(self) -> str:
        return "cloud_deploy_aws"

    def approval_summary(self, arguments: dict[str, Any]) -> str:
        """Render AWS stack, region, template, and plan ID awaiting approval."""
        return (
            f"Deploy AWS stack '{arguments.get('stack_name', '<missing>')}' "
            f"in '{arguments.get('region', '?')}' "
            f"(template={arguments.get('template_file', '<missing>')}, "
            f"plan_id={arguments.get('plan_id', '<missing>')})."
        )

    async def execute(self, arguments: dict[str, Any], sandbox: Sandbox) -> ToolResult:
        """Verify matching AWS plan & template, then run CloudFormation deploy."""
        started = time.monotonic()
        try:
            stack_name = _validate_identifier(arguments.get("stack_name"), "stack_name")
            region = _validate_identifier(arguments.get("region"), "region")
            template_file = validate_path(str(arguments.get("template_file") or ""))
            target = f"{region}/{stack_name}"
            plan_id = str(arguments.get("plan_id") or "")
            rec = self._tracker.require(plan_id, provider="aws", target=target)
            if rec.plan_artifact != template_file:
                msg = (
                    f"Plan '{rec.plan_id}' inspected template '{rec.plan_artifact}', "
                    f"refusing mismatched template '{template_file}'."
                )
                raise ValueError(msg)
        except ValueError as exc:
            return ToolResult(output="", error=str(exc), exit_code=1)

        cmd = (
            "aws cloudformation deploy "
            f"--stack-name {shlex.quote(stack_name)} "
            f"--region {shlex.quote(region)} "
            f"--template-file {shlex.quote(template_file)} "
            "--no-fail-on-empty-changeset"
        )
        res = await sandbox.exec(cmd, timeout_seconds=180)
        combined = _format_exec_output(res.stdout, res.stderr)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if res.exit_code != 0:
            return ToolResult(
                output=combined,
                error=f"aws cloudformation deploy failed with exit code {res.exit_code}",
                exit_code=res.exit_code,
                execution_time_ms=elapsed_ms,
            )

        self._tracker.consume(rec.plan_id)
        return ToolResult(
            output=combined,
            exit_code=0,
            execution_time_ms=elapsed_ms,
            metadata={"plan_id": rec.plan_id, "applied": True, "target": target},
        )


def create_cloud_deploy_tools(tracker: PlanGateTracker | None = None) -> list[Tool]:
    """Return instances of all six cloud-deploy plan/apply tools sharing *tracker*."""
    shared = tracker or PlanGateTracker()
    return [
        TerraformPlanTool(shared),
        TerraformApplyTool(shared),
        GcpCloudRunPlanTool(shared),
        GcpCloudRunDeployTool(shared),
        AwsDeployPlanTool(shared),
        AwsDeployApplyTool(shared),
    ]
