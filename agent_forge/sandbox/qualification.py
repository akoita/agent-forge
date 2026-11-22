"""Shared-use sandbox qualification checks across sandbox backends (#157)."""

from __future__ import annotations

import asyncio
import contextlib
import os
import platform
import shlex
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import docker
from docker.errors import NotFound

from agent_forge.sandbox.base import SandboxConfig
from agent_forge.sandbox.factory import create_sandbox

if TYPE_CHECKING:
    from agent_forge.sandbox.base import Sandbox


@dataclass(frozen=True)
class QualificationCheck:
    """Result of a single sandbox qualification check."""

    name: str
    passed: bool
    summary: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class QualificationReport:
    """Aggregate qualification report for a sandbox backend."""

    backend: str
    qualified_for_shared_use: bool
    checks: list[QualificationCheck]
    image: str | None = None
    environment: dict[str, str] = field(default_factory=dict)


async def qualify_sandbox_backend(
    backend: str,
    *,
    image: str = "agent-forge-sandbox:latest",
) -> QualificationReport:
    """Run all six shared-use qualification checks against ``backend``."""
    checks = [
        await _check_isolated_workspaces(backend, image=image),
        await _check_credential_handling(backend, image=image),
        await _check_egress_policy(backend, image=image),
        await _check_resource_limits(backend, image=image),
        await _check_cancellation(backend, image=image),
        await _check_cleanup(backend, image=image),
    ]
    qualified = all(check.passed for check in checks)
    env_info = {
        "platform": platform.system(),
        "python": platform.python_version(),
        "backend": backend,
        "bwrap": shutil.which("bwrap") or "missing",
    }
    return QualificationReport(
        backend=backend,
        qualified_for_shared_use=qualified,
        checks=checks,
        image=image if backend == "docker" else None,
        environment=env_info,
    )


def _sandbox_cfg(
    backend: str,
    *,
    image: str,
    env_vars: dict[str, str] | None = None,
) -> SandboxConfig:
    return SandboxConfig(
        backend=backend,
        image=image,
        cpu_limit=1.0,
        memory_limit="512m",
        timeout_seconds=30,
        network_enabled=False,
        env_vars=env_vars or {},
    )


async def _check_isolated_workspaces(backend: str, *, image: str) -> QualificationCheck:
    """Verify two concurrent sandboxes cannot inspect or mutate each other or rootfs."""
    with tempfile.TemporaryDirectory() as tmp_a, tempfile.TemporaryDirectory() as tmp_b:
        path_a, path_b = Path(tmp_a), Path(tmp_b)
        (path_a / "tenant_a.txt").write_text("alpha-secret", encoding="utf-8")
        (path_b / "tenant_b.txt").write_text("beta-secret", encoding="utf-8")

        sb_a = create_sandbox(_sandbox_cfg(backend, image=image))
        sb_b = create_sandbox(_sandbox_cfg(backend, image=image))
        await sb_a.start(str(path_a), _sandbox_cfg(backend, image=image))
        await sb_b.start(str(path_b), _sandbox_cfg(backend, image=image))
        try:
            read_a = await sb_a.read_file("/workspace/tenant_a.txt")
            read_b = await sb_b.read_file("/workspace/tenant_b.txt")
            with contextlib.suppress(FileNotFoundError):
                await sb_a.read_file("/workspace/tenant_b.txt")
                return QualificationCheck(
                    name="isolated_workspaces",
                    passed=False,
                    summary="sandbox A read tenant B workspace file",
                )
            with contextlib.suppress(FileNotFoundError):
                await sb_b.read_file("/workspace/tenant_a.txt")
                return QualificationCheck(
                    name="isolated_workspaces",
                    passed=False,
                    summary="sandbox B read tenant A workspace file",
                )

            await sb_a.write_file("/workspace/only_a.txt", "a-wrote")
            root_probe = await sb_a.exec("touch /escape_probe.txt")
            outside_probe = await sb_b.exec(f"cat {path_a}/tenant_a.txt")

            ok = (
                read_a == "alpha-secret"
                and read_b == "beta-secret"
                and (path_a / "only_a.txt").exists()
                and not (path_b / "only_a.txt").exists()
                and root_probe.exit_code != 0
                and outside_probe.exit_code != 0
            )
            return QualificationCheck(
                name="isolated_workspaces",
                passed=ok,
                summary="workspaces isolated; cross-workspace and host-path probes denied",
                details={
                    "root_write_rc": root_probe.exit_code,
                    "host_path_probe_rc": outside_probe.exit_code,
                },
            )
        finally:
            await sb_a.stop()
            await sb_b.stop()


async def _check_credential_handling(backend: str, *, image: str) -> QualificationCheck:
    """Verify host and explicit secret env vars are scrubbed from sandbox processes."""
    injected_host = {
        "GEMINI_API_KEY": "qual-host-gemini-key",
        "AGENT_FORGE_QUAL_HOST_SECRET": "qual-host-secret",
    }
    previous = {k: os.environ.get(k) for k in injected_host}
    os.environ.update(injected_host)

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg = _sandbox_cfg(
                backend,
                image=image,
                env_vars={
                    "SAFE_QUAL_VAR": "visible-ok",
                    "OPENAI_API_KEY": "config-api-key-leak",
                    "AGENT_FORGE_CUSTOM_TOKEN": "config-token-leak",
                },
            )
            sandbox = create_sandbox(cfg)
            await sandbox.start(tmp_dir, cfg)
            try:
                res = await sandbox.exec("env")
                env_output = res.stdout
                leaked = [
                    key
                    for key in (
                        "GEMINI_API_KEY",
                        "AGENT_FORGE_QUAL_HOST_SECRET",
                        "OPENAI_API_KEY",
                        "AGENT_FORGE_CUSTOM_TOKEN",
                    )
                    if f"{key}=" in env_output
                ]
                safe_present = "SAFE_QUAL_VAR=visible-ok" in env_output
                ok = res.exit_code == 0 and not leaked and safe_present
                return QualificationCheck(
                    name="credential_handling",
                    passed=ok,
                    summary="host and secret config env vars excluded from sandbox",
                    details={"leaked_keys": leaked, "safe_var_present": safe_present},
                )
            finally:
                await sandbox.stop()
    finally:
        for key, old_val in previous.items():
            if old_val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_val


async def _check_egress_policy(backend: str, *, image: str) -> QualificationCheck:
    """Verify external and cloud-metadata egress fail when network_enabled=False."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        cfg = _sandbox_cfg(backend, image=image)
        sandbox = create_sandbox(cfg)
        await sandbox.start(tmp_dir, cfg)
        try:
            probe_script = (
                "import socket\n"
                "results = {}\n"
                "for target in ('1.1.1.1', '169.254.169.254'):\n"
                "    s = socket.socket()\n"
                "    s.settimeout(1.5)\n"
                "    try:\n"
                "        s.connect((target, 80))\n"
                "        results[target] = 'OPEN'\n"
                "    except Exception as exc:\n"
                "        results[target] = type(exc).__name__\n"
                "    finally:\n"
                "        s.close()\n"
                "print(results)\n"
                "raise SystemExit(1 if 'OPEN' in results.values() else 0)"
            )
            probe_cmd = f"python3 -c {shlex.quote(probe_script)}"
            res = await sandbox.exec(probe_cmd, timeout_seconds=10)
            stdout = res.stdout.strip()
            blocked = (
                res.exit_code == 0
                and "'1.1.1.1':" in stdout
                and "'169.254.169.254':" in stdout
                and "'OPEN'" not in stdout
            )
            return QualificationCheck(
                name="egress_policy",
                passed=blocked,
                summary="public internet and GCP metadata egress blocked",
                details={
                    "probe_stdout": stdout,
                    "probe_stderr": res.stderr.strip(),
                    "exit_code": res.exit_code,
                },
            )
        finally:
            await sandbox.stop()


async def _check_resource_limits(backend: str, *, image: str) -> QualificationCheck:
    """Verify cgroup CPU/memory/PID bounds, read-only rootfs, and /tmp noexec."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        cfg = _sandbox_cfg(backend, image=image)
        sandbox = create_sandbox(cfg)
        await sandbox.start(tmp_dir, cfg)
        try:
            tmp_exec = await sandbox.exec(
                "printf '#!/bin/sh\\necho ran\\n' > /tmp/qual_exec.sh && "
                "chmod +x /tmp/qual_exec.sh && /tmp/qual_exec.sh"
            )
            if backend == "docker":
                return await _inspect_docker_limits(sandbox, tmp_exec_rc=tmp_exec.exit_code)
            return QualificationCheck(
                name="resource_limits",
                passed=False,
                summary=(
                    "bubblewrap isolates namespaces/FS but lacks cgroup CPU/RAM/PID bounds "
                    "and /tmp noexec; not qualified for shared multi-tenant execution"
                ),
                details={"tmp_exec_rc": tmp_exec.exit_code},
            )
        finally:
            await sandbox.stop()


async def _inspect_docker_limits(
    sandbox: Sandbox,
    *,
    tmp_exec_rc: int,
) -> QualificationCheck:
    container_id = getattr(sandbox, "container_id", None)
    if not container_id:
        return QualificationCheck(
            name="resource_limits",
            passed=False,
            summary="docker container_id unavailable for limit inspection",
        )
    client = docker.from_env()
    attrs = await asyncio.to_thread(client.api.inspect_container, container_id)
    host_cfg = attrs.get("HostConfig", {})
    sec_opts = host_cfg.get("SecurityOpt") or []
    cap_drop = host_cfg.get("CapDrop") or []
    checks = {
        "tmp_noexec": tmp_exec_rc != 0,
        "readonly_rootfs": host_cfg.get("ReadonlyRootfs") is True,
        "cap_drop_all": "ALL" in cap_drop,
        "no_new_privileges": any("no-new-privileges" in str(opt) for opt in sec_opts),
        "pids_limit_256": host_cfg.get("PidsLimit") == 256,
        "nano_cpus_set": int(host_cfg.get("NanoCpus") or 0) == 1_000_000_000,
        "memory_limit_set": int(host_cfg.get("Memory") or 0) > 0,
        "network_none": host_cfg.get("NetworkMode") == "none",
    }
    passed = all(checks.values())
    return QualificationCheck(
        name="resource_limits",
        passed=passed,
        summary=(
            "Docker cgroup CPU/RAM/PID limits, cap_drop=ALL, read-only rootfs, "
            "and /tmp noexec enforced"
        ),
        details=checks,
    )


async def _check_cancellation(backend: str, *, image: str) -> QualificationCheck:
    """Verify cancelling an active command + stopping the sandbox aborts promptly."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        cfg = _sandbox_cfg(backend, image=image)
        sandbox = create_sandbox(cfg)
        await sandbox.start(tmp_dir, cfg)
        started = time.monotonic()
        cancelled = False
        try:
            task = asyncio.create_task(sandbox.exec("sleep 30", timeout_seconds=30))
            await asyncio.sleep(0.2)
            task.cancel()
            await sandbox.stop()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            cancelled = True
            elapsed = time.monotonic() - started
            return QualificationCheck(
                name="cancellation",
                passed=cancelled and elapsed < 5.0,
                summary=f"cancelled long-running command and stopped sandbox in {elapsed:.2f}s",
                details={"elapsed_seconds": round(elapsed, 3)},
            )
        finally:
            await sandbox.stop()


async def _wait_for_container_removal(
    container_id: str,
    *,
    timeout_seconds: float = 5.0,
) -> tuple[bool, str | None]:
    """Wait briefly for Docker ``--rm`` auto-removal after ``stop()`` returns."""
    client = docker.from_env()
    deadline = time.monotonic() + timeout_seconds
    last_status: str | None = None
    while True:
        try:
            container = await asyncio.to_thread(client.containers.get, container_id)
        except NotFound:
            return True, None
        last_status = str(getattr(container, "status", "unknown"))
        if last_status == "running":
            return False, last_status
        if time.monotonic() >= deadline:
            return False, last_status
        await asyncio.sleep(0.1)


async def _check_cleanup(backend: str, *, image: str) -> QualificationCheck:
    """Verify stop() leaves no live sandbox container or tracked child processes."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        cfg = _sandbox_cfg(backend, image=image)
        sandbox = create_sandbox(cfg)
        await sandbox.start(tmp_dir, cfg)
        container_id = getattr(sandbox, "container_id", None)
        await sandbox.exec("echo cleanup-probe")
        await sandbox.stop()
        alive_after = await sandbox.is_alive()

        container_gone = True
        last_status: str | None = None
        if backend == "docker" and container_id:
            container_gone, last_status = await _wait_for_container_removal(container_id)

        active_procs = len(getattr(sandbox, "_active_processes", ()))
        ok = (not alive_after) and container_gone and active_procs == 0
        details: dict[str, Any] = {
            "alive_after_stop": alive_after,
            "container_removed": container_gone,
            "active_processes": active_procs,
        }
        if last_status is not None:
            details["last_container_status"] = last_status
        return QualificationCheck(
            name="cleanup",
            passed=ok,
            summary="stop() terminated sandbox with zero leaked containers or processes",
            details=details,
        )
