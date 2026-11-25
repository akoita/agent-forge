"""Unit tests for the plugins/evm_foundry extension pack (#227)."""

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
from agent_forge.tools.base import ToolRegistry
from agent_forge.tools.plugins import load_plugin_tools
from plugins.evm_foundry import (
    FOUNDRY_SANDBOX_IMAGE,
    SimulationGateTracker,
    build_foundry_sandbox_config,
    create_evm_foundry_tools,
    get_extension_info,
    get_profiles_dir,
    get_prompt_fragment,
    validate_sandbox_signer_isolation,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


class _FoundryRecordingSandbox(Sandbox):
    """Workspace sandbox mock recording forge build/test/script/broadcast commands."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    async def start(self, repo_path: str, config: SandboxConfig | None = None) -> None:
        del repo_path, config

    async def exec(self, command: str, *, timeout_seconds: int = 30) -> ExecResult:
        del timeout_seconds
        self.commands.append(command)
        if "forge build" in command:
            return ExecResult(exit_code=0, stdout="| Contract | Runtime Size |", stderr="")
        if "forge test" in command:
            return ExecResult(
                exit_code=0, stdout="Suite result: ok. 12 passed; 0 failed;", stderr=""
            )
        if "forge script" in command and "--broadcast" not in command:
            return ExecResult(
                exit_code=0,
                stdout="Script ran successfully.\nEstimated total gas used for script: 184220\n",
                stderr="",
            )
        if "forge script" in command and "--broadcast" in command:
            return ExecResult(
                exit_code=0, stdout="ONCHAIN EXECUTION COMPLETE & SUCCESSFUL.", stderr=""
            )
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


class _QueuedFoundryLLM(LLMProvider):
    """Deterministic scripted LLM emitting sequential responses."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = responses
        self._idx = 0

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: LLMConfig | None = None,
    ) -> LLMResponse:
        del messages, tools, config
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


def test_evm_foundry_metadata_profiles_and_plugin_tools() -> None:
    info = get_extension_info()
    assert info.name == "evm-foundry"
    assert set(info.profiles) == {"evm-foundry-dev", "evm-foundry-deploy"}
    assert "forge_simulate_script" in get_prompt_fragment()

    profiles = load_profiles(
        dirs=[get_profiles_dir()],
        include_builtins=False,
        discover_entry_points=False,
    )
    assert set(profiles) == {
        "evm-foundry-dev",
        "foundry-dev",
        "evm-dev",
        "evm-foundry-deploy",
        "foundry-deploy",
        "evm-deploy",
    }
    assert profiles["foundry-deploy"] is profiles["evm-foundry-deploy"]
    assert "simulation-gated" in profiles["evm-foundry-deploy"].capabilities

    eps = [
        EntryPoint(
            name=name,
            value=f"plugins.evm_foundry.tools:{cls_name}",
            group="agent_forge.tools",
        )
        for name, cls_name in (
            ("forge_build", "ForgeBuildTool"),
            ("forge_test", "ForgeTestTool"),
            ("forge_simulate_script", "ForgeSimulateScriptTool"),
            ("forge_broadcast_script", "ForgeBroadcastScriptTool"),
        )
    ]
    by_name = {t.name: t for t in load_plugin_tools(entry_points_factory=lambda: eps)}
    assert by_name["forge_build"].requires_approval is False
    assert by_name["forge_test"].requires_approval is False
    assert by_name["forge_simulate_script"].requires_approval is False
    assert by_name["forge_broadcast_script"].requires_approval is True


def test_foundry_sandbox_config_and_signer_isolation_policies() -> None:
    offline_cfg = build_foundry_sandbox_config()
    assert offline_cfg.image == FOUNDRY_SANDBOX_IMAGE
    assert offline_cfg.network_enabled is False
    assert offline_cfg.allowed_egress_domains == []

    fork_cfg = build_foundry_sandbox_config(rpc_domains=["rpc.sepolia.org", "rpc.sepolia.org"])
    assert fork_cfg.network_enabled is True
    assert fork_cfg.allowed_egress_domains == ["rpc.sepolia.org"]

    with pytest.raises(ValueError, match="IP literal"):
        build_foundry_sandbox_config(rpc_domains=["127.0.0.1"])

    with pytest.raises(ValueError, match="localhost"):
        build_foundry_sandbox_config(rpc_domains=["localhost"])

    hex_sample = "0x" + ("ab" * 32)
    pem_sample = "-----BEGIN PRIVATE " + "KEY-----"
    with pytest.raises(ValueError, match="PRIVATE_KEY"):
        validate_sandbox_signer_isolation({"PRIVATE_KEY": hex_sample})
    with pytest.raises(ValueError, match="MNEMONIC"):
        validate_sandbox_signer_isolation({"MNEMONIC": "test twelve words"})
    with pytest.raises(ValueError, match="private-key"):
        validate_sandbox_signer_isolation({"SIGNER_BLOB": pem_sample})
    with pytest.raises(ValueError, match="32-byte hex"):
        validate_sandbox_signer_isolation({"DEPLOY_SIGNER_HEX": hex_sample})
    with pytest.raises(ValueError, match="--private-key"):
        validate_sandbox_signer_isolation({}, ["forge", "script", "--private-key", hex_sample])
    with pytest.raises(ValueError, match="--unlocked"):
        validate_sandbox_signer_isolation({}, ["forge", "script", "--unlocked"])

    validate_sandbox_signer_isolation({"PATH": "/usr/local/bin"}, ["forge", "test", "-vv"])


@pytest.mark.asyncio
async def test_simulation_before_broadcast_and_validation_guards() -> None:
    sb = _FoundryRecordingSandbox()
    tracker = SimulationGateTracker()
    tools = {t.name: t for t in create_evm_foundry_tools(tracker)}

    res_build = await tools["forge_build"].execute({"working_dir": "/workspace"}, sb)
    assert res_build.exit_code == 0
    res_test = await tools["forge_test"].execute(
        {"working_dir": "/workspace", "match_contract": "VaultTest", "verbosity": 3}, sb
    )
    assert res_test.exit_code == 0

    missing = await tools["forge_broadcast_script"].execute(
        {
            "sim_id": "sim-missing",
            "script_path": "/workspace/script/Deploy.s.sol",
            "rpc_alias": "sepolia",
        },
        sb,
    )
    assert missing.exit_code == 1
    assert "Unknown or already consumed" in (missing.error or "")

    bad_rpc = await tools["forge_simulate_script"].execute(
        {"script_path": "/workspace/script/Deploy.s.sol", "rpc_alias": "sepolia; rm -rf /"}, sb
    )
    assert bad_rpc.exit_code == 1
    assert "Invalid rpc_alias" in (bad_rpc.error or "")

    bad_path = await tools["forge_simulate_script"].execute(
        {"script_path": "/workspace/../etc/passwd", "rpc_alias": "sepolia"}, sb
    )
    assert bad_path.exit_code == 1
    assert "Path traversal" in (bad_path.error or "")

    assert not any("--broadcast" in cmd for cmd in sb.commands)


@pytest.mark.asyncio
async def test_react_loop_simulation_then_approval_gated_broadcast(tmp_path: Path) -> None:
    sb = _FoundryRecordingSandbox()
    tracker = SimulationGateTracker()
    reg = ToolRegistry()
    for tool in create_evm_foundry_tools(tracker):
        reg.register(tool)

    # Simulate first (never passes --broadcast)
    sim_res = await reg.get("forge_simulate_script").execute(
        {
            "script_path": "/workspace/script/Deploy.s.sol",
            "rpc_alias": "sepolia",
            "working_dir": "/workspace",
            "target_contract": "DeployScript",
            "sig": "run()",
        },
        sb,
    )
    assert sim_res.exit_code == 0
    assert sim_res.metadata["broadcast"] is False
    assert sim_res.metadata["estimated_gas"] == 184220
    sim_id = str(sim_res.metadata["sim_id"])

    # 1. Deny gate blocks broadcast without consuming sim_id
    llm_deny = _QueuedFoundryLLM(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="bc-deny",
                        name="forge_broadcast_script",
                        arguments={
                            "sim_id": sim_id,
                            "script_path": "/workspace/script/Deploy.s.sol",
                            "rpc_alias": "sepolia",
                            "working_dir": "/workspace",
                            "target_contract": "DeployScript",
                        },
                    )
                ],
                usage=TokenUsage(10, 5, 15),
                model="test",
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content="Broadcast denied by policy.",
                usage=TokenUsage(10, 5, 15),
                model="test",
                finish_reason="stop",
            ),
        ]
    )
    run_deny = AgentRun(
        task="Broadcast deploy script",
        repo_path=str(tmp_path),
        config=AgentConfig(context_enabled=False),
    )
    await react_loop(
        run_deny,
        llm_deny,
        reg,
        sb,
        approval_gate=ApprovalGate(mode="deny"),
        emit_lifecycle=False,
    )
    assert run_deny.state == RunState.COMPLETED
    assert run_deny.tool_invocations[0].result.metadata["approval_denied"] is True
    assert not any("--broadcast" in cmd for cmd in sb.commands)

    # 2. Prompt approval permits broadcast via host_broker/hardware without raw private keys
    seen_approvals: list[ApprovalRequest] = []

    async def _approve(req: ApprovalRequest) -> bool:
        seen_approvals.append(req)
        return True

    llm_ok = _QueuedFoundryLLM(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="bc-ok",
                        name="forge_broadcast_script",
                        arguments={
                            "sim_id": sim_id,
                            "script_path": "/workspace/script/Deploy.s.sol",
                            "rpc_alias": "sepolia",
                            "working_dir": "/workspace",
                            "target_contract": "DeployScript",
                            "signer_mode": "hardware_ledger",
                        },
                    )
                ],
                usage=TokenUsage(10, 5, 15),
                model="test",
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content="Broadcast complete.",
                usage=TokenUsage(10, 5, 15),
                model="test",
                finish_reason="stop",
            ),
        ]
    )
    run_ok = AgentRun(
        task="Broadcast deploy script",
        repo_path=str(tmp_path),
        config=AgentConfig(context_enabled=False),
    )
    await react_loop(
        run_ok,
        llm_ok,
        reg,
        sb,
        approval_gate=ApprovalGate(mode="prompt", callback=_approve),
        emit_lifecycle=False,
    )
    assert run_ok.state == RunState.COMPLETED
    assert len(seen_approvals) == 1
    assert seen_approvals[0].category == "evm_broadcast"
    assert sim_id in seen_approvals[0].reason
    assert any("--broadcast" in cmd and "--ledger" in cmd for cmd in sb.commands)
    assert all("--private-key" not in cmd for cmd in sb.commands)

    # Consumed sim_id cannot be replayed
    replay = await reg.get("forge_broadcast_script").execute(
        {
            "sim_id": sim_id,
            "script_path": "/workspace/script/Deploy.s.sol",
            "rpc_alias": "sepolia",
            "working_dir": "/workspace",
            "target_contract": "DeployScript",
        },
        sb,
    )
    assert replay.exit_code == 1
    assert "Unknown or already consumed" in (replay.error or "")
