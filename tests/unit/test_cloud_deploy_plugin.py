"""Unit tests for the plugins/cloud_deploy extension pack (#226)."""

from __future__ import annotations

from importlib.metadata import EntryPoint
from typing import TYPE_CHECKING

import pytest

from agent_forge.agent.approvals import ApprovalGate, ApprovalRequest
from agent_forge.agent.core import react_loop
from agent_forge.agent.models import AgentConfig, AgentRun, RunState
from agent_forge.llm.base import (
    LLMConfig,
    LLMProvider,
    LLMResponse,
    Message,
    TokenUsage,
    ToolCall,
    ToolDefinition,
)
from agent_forge.profiles.profile import load_profiles
from agent_forge.sandbox.base import ExecResult, Sandbox, SandboxConfig
from agent_forge.sandbox.egress_proxy import is_domain_allowed
from agent_forge.tools.base import ToolRegistry
from agent_forge.tools.plugins import load_plugin_tools
from plugins.cloud_deploy import (
    BrokeredToken,
    HostAuthBroker,
    PlanGateTracker,
    ShortLivedAuthConfig,
    build_sandbox_egress_policy,
    create_cloud_deploy_tools,
    get_extension_info,
    get_profiles_dir,
    get_prompt_fragment,
    validate_host_brokered_auth_env,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


class _RecordingSandbox(Sandbox):
    """Minimal workspace sandbox recording shell commands for plan/apply gating tests."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    async def start(self, repo_path: str, config: SandboxConfig | None = None) -> None:
        del repo_path, config

    async def exec(self, command: str, *, timeout_seconds: int = 30) -> ExecResult:
        del timeout_seconds
        self.commands.append(command)
        if "terraform plan" in command:
            return ExecResult(
                exit_code=0, stdout="Plan: 1 to add, 0 to change, 0 to destroy.", stderr=""
            )
        if "terraform apply" in command:
            return ExecResult(exit_code=0, stdout="Apply complete! Resources: 1 added.", stderr="")
        if "gcloud run services describe" in command:
            return ExecResult(
                exit_code=0, stdout="status: READY\nurl: https://svc.a.run.app", stderr=""
            )
        if "gcloud run deploy" in command:
            return ExecResult(
                exit_code=0, stdout="Deploying container to Cloud Run service.", stderr=""
            )
        if "--no-execute-changeset" in command:
            return ExecResult(
                exit_code=0, stdout="Successfully created changeset cs-123.", stderr=""
            )
        if "aws cloudformation deploy" in command:
            return ExecResult(exit_code=0, stdout="Successfully created/updated stack.", stderr="")
        return ExecResult(exit_code=0, stdout="ok", stderr="")

    async def read_file(self, path: str) -> str:
        del path
        return ""

    async def write_file(self, path: str, content: str) -> None:
        del path, content

    async def list_files(self, path: str = "/workspace") -> list[str]:
        del path
        return []

    async def stop(self) -> None:
        return None

    async def is_alive(self) -> bool:
        return True


class _QueuedLLM(LLMProvider):
    """Deterministic scripted LLM consuming responses in sequence or via callable."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = responses
        self._idx = 0
        self.seen_tool_messages: list[str] = []

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: LLMConfig | None = None,
    ) -> LLMResponse:
        del tools, config
        for msg in messages:
            if msg.role.value == "tool" and msg.content not in self.seen_tool_messages:
                self.seen_tool_messages.append(msg.content)
        resp = self._responses[min(self._idx, len(self._responses) - 1)]
        self._idx += 1
        return resp

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: LLMConfig | None = None,
    ) -> AsyncIterator[LLMResponse]:
        yield await self.complete(messages, tools=tools, config=config)


def test_cloud_deploy_extension_metadata_profiles_and_entry_points() -> None:
    info = get_extension_info()
    assert info.name == "cloud-deploy"
    assert set(info.profiles) == {
        "cloud-deploy-gcp",
        "cloud-deploy-aws",
        "cloud-deploy-terraform",
    }
    assert "plan/dry-run" in get_prompt_fragment()

    profiles = load_profiles(
        dirs=[get_profiles_dir()],
        include_builtins=False,
        discover_entry_points=False,
    )
    assert set(profiles) == {
        "cloud-deploy-gcp",
        "gcp-deploy",
        "cloud-run-deploy",
        "cloud-deploy-aws",
        "aws-deploy",
        "cloud-deploy-terraform",
        "terraform-deploy",
        "tf-deploy",
    }
    assert profiles["gcp-deploy"] is profiles["cloud-deploy-gcp"]
    assert "approval-gated" in profiles["cloud-deploy-terraform"].capabilities

    eps = [
        EntryPoint(
            name=tool_name,
            value=f"plugins.cloud_deploy.tools:{cls_name}",
            group="agent_forge.tools",
        )
        for tool_name, cls_name in (
            ("terraform_plan", "TerraformPlanTool"),
            ("terraform_apply", "TerraformApplyTool"),
            ("gcp_cloud_run_plan", "GcpCloudRunPlanTool"),
            ("gcp_cloud_run_deploy", "GcpCloudRunDeployTool"),
            ("aws_deploy_plan", "AwsDeployPlanTool"),
            ("aws_deploy_apply", "AwsDeployApplyTool"),
        )
    ]
    loaded = load_plugin_tools(entry_points_factory=lambda: eps)
    by_name = {t.name: t for t in loaded}
    assert by_name["terraform_plan"].requires_approval is False
    assert by_name["gcp_cloud_run_plan"].requires_approval is False
    assert by_name["aws_deploy_plan"].requires_approval is False
    assert by_name["terraform_apply"].requires_approval is True
    assert by_name["gcp_cloud_run_deploy"].requires_approval is True
    assert by_name["aws_deploy_apply"].requires_approval is True


def test_egress_allowlist_and_host_brokered_auth_boundaries() -> None:
    domains = build_sandbox_egress_policy(["gcp", "aws", "terraform"])
    assert is_domain_allowed("run.googleapis.com", domains) is True
    assert is_domain_allowed("sts.amazonaws.com", domains) is True
    assert is_domain_allowed("registry.terraform.io", domains) is True
    assert is_domain_allowed("evil.example.com", domains) is False
    assert is_domain_allowed("169.254.169.254", domains) is False
    assert is_domain_allowed("localhost", domains) is False

    with pytest.raises(ValueError, match="Unsupported cloud provider"):
        build_sandbox_egress_policy(["unknown"])  # type: ignore[list-item]

    with pytest.raises(ValueError, match="AWS_SECRET_ACCESS_KEY"):
        validate_host_brokered_auth_env({"AWS_SECRET_ACCESS_KEY": "static"})

    with pytest.raises(ValueError, match="GOOGLE_APPLICATION_CREDENTIALS"):
        validate_host_brokered_auth_env({"GOOGLE_APPLICATION_CREDENTIALS": "/tmp/key.json"})

    with pytest.raises(ValueError, match="private-key"):
        validate_host_brokered_auth_env({"CUSTOM": "-----BEGIN PRIVATE KEY-----"})

    with pytest.raises(ValueError, match="ttl_seconds"):
        ShortLivedAuthConfig(
            provider="gcp_wif",
            audience="//iam.googleapis.com/projects/1/providers/p",
            role_or_service_account="deploy@proj.iam.gserviceaccount.com",
            ttl_seconds=7200,
        )

    cfg = ShortLivedAuthConfig(
        provider="aws_sts_oidc",
        audience="sts.amazonaws.com",
        role_or_service_account="arn:aws:iam::123456789012:role/deploy",
        ttl_seconds=900,
    )
    with pytest.raises(RuntimeError, match="OIDC/WIF"):
        HostAuthBroker().mint_ephemeral_token(cfg)

    expected_ephemeral = "-".join(("ephemeral", "oidc", "abc123"))
    broker = HostAuthBroker(
        exchanger=lambda c: BrokeredToken(
            provider=c.provider,
            token=expected_ephemeral,
            expires_in_seconds=c.ttl_seconds,
            principal=c.role_or_service_account,
        )
    )
    issued = broker.mint_ephemeral_token(cfg, sandbox_env={"PATH": "/usr/bin"})
    assert issued.token == expected_ephemeral
    scrubbed = broker.scrub_secrets(f"token={issued.token} ok", [issued.token])
    assert expected_ephemeral not in scrubbed
    assert "[REDACTED_TOKEN]" in scrubbed


@pytest.mark.asyncio
async def test_plan_before_apply_and_input_validation_guards() -> None:
    sb = _RecordingSandbox()
    tracker = PlanGateTracker()
    tools = {t.name: t for t in create_cloud_deploy_tools(tracker)}

    # Apply without prior plan is rejected before touching sandbox.exec
    res_no_plan = await tools["terraform_apply"].execute(
        {"plan_id": "terraform-missing", "working_dir": "/workspace"}, sb
    )
    assert res_no_plan.exit_code == 1
    assert "Unknown or already consumed" in (res_no_plan.error or "")
    assert sb.commands == []

    # Shell injection and path traversal are rejected cleanly
    bad_gcp = await tools["gcp_cloud_run_plan"].execute(
        {
            "project": "myproj; rm -rf /",
            "region": "us-central1",
            "service": "api",
            "image": "us-docker.pkg.dev/myproj/app/api:v1",
        },
        sb,
    )
    assert bad_gcp.exit_code == 1
    assert "Invalid project" in (bad_gcp.error or "")

    bad_tf = await tools["terraform_plan"].execute({"working_dir": "/etc/passwd"}, sb)
    assert bad_tf.exit_code == 1
    assert "within /workspace" in (bad_tf.error or "")
    assert sb.commands == []


@pytest.mark.asyncio
async def test_react_loop_approval_gate_deny_and_approve_across_all_providers(
    tmp_path: Path,
) -> None:
    # 1. Deny mode blocks terraform_apply after terraform_plan succeeds
    sb_deny = _RecordingSandbox()
    reg_deny = ToolRegistry()
    for tool in create_cloud_deploy_tools(PlanGateTracker()):
        reg_deny.register(tool)

    plan_res = await reg_deny.get("terraform_plan").execute(
        {"working_dir": "/workspace/infra"}, sb_deny
    )
    plan_id = str(plan_res.metadata["plan_id"])

    llm_deny = _QueuedLLM(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="tf-deny",
                        name="terraform_apply",
                        arguments={"plan_id": plan_id, "working_dir": "/workspace/infra"},
                    )
                ],
                usage=TokenUsage(10, 5, 15),
                model="test",
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content="Apply blocked by policy.",
                usage=TokenUsage(10, 5, 15),
                model="test",
                finish_reason="stop",
            ),
        ]
    )
    run_deny = AgentRun(
        task="Apply terraform",
        repo_path=str(tmp_path),
        config=AgentConfig(context_enabled=False),
    )
    await react_loop(
        run_deny,
        llm_deny,
        reg_deny,
        sb_deny,
        approval_gate=ApprovalGate(mode="deny"),
        emit_lifecycle=False,
    )
    assert run_deny.state == RunState.COMPLETED
    assert run_deny.tool_invocations[0].result.metadata["approval_denied"] is True
    assert not any("terraform apply" in cmd for cmd in sb_deny.commands)

    # 2. Prompt approval permits plan -> apply for Terraform, GCP, and AWS and consumes plan_id
    sb_ok = _RecordingSandbox()
    tracker = PlanGateTracker()
    reg_ok = ToolRegistry()
    for tool in create_cloud_deploy_tools(tracker):
        reg_ok.register(tool)

    tf_p = await reg_ok.get("terraform_plan").execute({"working_dir": "/workspace/infra"}, sb_ok)
    gcp_p = await reg_ok.get("gcp_cloud_run_plan").execute(
        {
            "project": "demo-proj",
            "region": "europe-west1",
            "service": "api",
            "image": "europe-west1-docker.pkg.dev/demo-proj/app/api:v2",
        },
        sb_ok,
    )
    aws_p = await reg_ok.get("aws_deploy_plan").execute(
        {
            "stack_name": "api-stack",
            "region": "eu-west-1",
            "template_file": "/workspace/infra/template.yaml",
        },
        sb_ok,
    )

    tf_id = str(tf_p.metadata["plan_id"])
    gcp_id = str(gcp_p.metadata["plan_id"])
    aws_id = str(aws_p.metadata["plan_id"])

    approvals: list[ApprovalRequest] = []

    async def _approve(req: ApprovalRequest) -> bool:
        approvals.append(req)
        return True

    llm_ok = _QueuedLLM(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="apply-tf",
                        name="terraform_apply",
                        arguments={"plan_id": tf_id, "working_dir": "/workspace/infra"},
                    )
                ],
                usage=TokenUsage(10, 5, 15),
                model="test",
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="apply-gcp",
                        name="gcp_cloud_run_deploy",
                        arguments={
                            "plan_id": gcp_id,
                            "project": "demo-proj",
                            "region": "europe-west1",
                            "service": "api",
                            "image": "europe-west1-docker.pkg.dev/demo-proj/app/api:v2",
                        },
                    )
                ],
                usage=TokenUsage(10, 5, 15),
                model="test",
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="apply-aws",
                        name="aws_deploy_apply",
                        arguments={
                            "plan_id": aws_id,
                            "stack_name": "api-stack",
                            "region": "eu-west-1",
                            "template_file": "/workspace/infra/template.yaml",
                        },
                    )
                ],
                usage=TokenUsage(10, 5, 15),
                model="test",
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content="All planned deployments applied.",
                usage=TokenUsage(10, 5, 15),
                model="test",
                finish_reason="stop",
            ),
        ]
    )
    run_ok = AgentRun(
        task="Apply planned deployments",
        repo_path=str(tmp_path),
        config=AgentConfig(context_enabled=False),
    )
    await react_loop(
        run_ok,
        llm_ok,
        reg_ok,
        sb_ok,
        approval_gate=ApprovalGate(mode="prompt", callback=_approve),
        emit_lifecycle=False,
    )
    assert run_ok.state == RunState.COMPLETED
    assert [req.category for req in approvals] == [
        "cloud_deploy_terraform",
        "cloud_deploy_gcp",
        "cloud_deploy_aws",
    ]
    assert tf_id in approvals[0].reason
    assert any("terraform apply" in cmd for cmd in sb_ok.commands)
    assert any("gcloud run deploy api" in cmd for cmd in sb_ok.commands)
    assert any("--no-fail-on-empty-changeset" in cmd for cmd in sb_ok.commands)

    # Consumed plan_id cannot be replayed
    replay = await reg_ok.get("terraform_apply").execute(
        {"plan_id": tf_id, "working_dir": "/workspace/infra"}, sb_ok
    )
    assert replay.exit_code == 1
    assert "Unknown or already consumed" in (replay.error or "")
