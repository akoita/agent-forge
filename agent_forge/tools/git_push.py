"""git_push tool — host-brokered push using host credentials without container leakage."""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_forge.auth import scrub_secret
from agent_forge.tools.base import Tool, ToolResult
from agent_forge.tools.git_common import github_token, validate_ref_name

if TYPE_CHECKING:
    from agent_forge.sandbox.base import Sandbox

_PROTECTED_BRANCHES = frozenset({"main", "master"})


def _is_https_github_remote(remote_url: str) -> bool:
    cleaned = remote_url.strip().lower()
    return cleaned.startswith(("https://github.com/", "http://github.com/"))


async def _run_host_git(
    repo_root: Path,
    *args: str,
    env_extra: dict[str, str] | None = None,
    timeout: int = 60,
) -> tuple[int, str, str]:
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "GIT_TERMINAL_PROMPT": "0",
    }
    if env_extra:
        env.update(env_extra)
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(repo_root),
        *args,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_raw, stderr_raw = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()
        with contextlib.suppress(OSError):
            await proc.wait()
        return 124, "", f"git {args[0] if args else 'push'} timed out after {timeout}s"
    return (
        proc.returncode if proc.returncode is not None else 1,
        stdout_raw.decode("utf-8", errors="replace"),
        stderr_raw.decode("utf-8", errors="replace"),
    )


class GitPushTool(Tool):
    """Push a committed branch to a remote repository from the host."""

    @property
    def name(self) -> str:
        return "git_push"

    @property
    def description(self) -> str:
        return (
            "Push the current or specified committed branch to a Git remote using host credentials"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "remote": {
                    "type": "string",
                    "description": "Remote name to push to (default 'origin')",
                    "default": "origin",
                },
                "branch": {
                    "type": "string",
                    "description": "Optional branch name (defaults to current checked-out branch)",
                },
                "set_upstream": {
                    "type": "boolean",
                    "description": "Whether to pass -u/--set-upstream (default true)",
                    "default": True,
                },
                "force_with_lease": {
                    "type": "boolean",
                    "description": "Whether to pass --force-with-lease (default false)",
                    "default": False,
                },
                "allow_protected_branch": {
                    "type": "boolean",
                    "description": "Allow pushing directly to main or master (default false)",
                    "default": False,
                },
            },
        }

    def _error(self, started: float, message: str, *, exit_code: int = 1) -> ToolResult:
        return ToolResult(
            output="",
            error=message,
            exit_code=exit_code,
            execution_time_ms=int((time.monotonic() - started) * 1000),
        )

    async def _resolve_branch(
        self,
        repo_root: Path,
        arguments: dict[str, Any],
        started: float,
    ) -> str | ToolResult:
        raw_branch = str(arguments.get("branch", "")).strip()
        if not raw_branch:
            code, out, err = await _run_host_git(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
            if code != 0:
                return self._error(started, err.strip() or "Unable to resolve current branch.")
            raw_branch = out.strip()
        if raw_branch == "HEAD":
            return self._error(started, "Cannot push from detached HEAD; specify a branch.")
        try:
            return validate_ref_name(raw_branch, field_name="branch")
        except ValueError as exc:
            return self._error(started, str(exc))

    async def _resolve_push_token(
        self,
        repo_root: Path,
        remote: str,
        started: float,
    ) -> str | ToolResult | None:
        code, remote_url, err = await _run_host_git(repo_root, "remote", "get-url", remote)
        if code != 0:
            return self._error(started, err.strip() or f"Unknown git remote: '{remote}'.")
        if not _is_https_github_remote(remote_url):
            return None
        token = github_token()
        if not token:
            return self._error(
                started,
                "Missing GitHub token for HTTPS push. "
                "Set GITHUB_TOKEN/GH_TOKEN or run 'agent-forge auth login github'.",
            )
        return token

    @staticmethod
    def _build_push_args(remote: str, branch: str, arguments: dict[str, Any]) -> list[str]:
        args = ["push"]
        if bool(arguments.get("set_upstream", True)):
            args.append("--set-upstream")
        if bool(arguments.get("force_with_lease", False)):
            args.append("--force-with-lease")
        args.extend(["--", remote, branch])
        return args

    async def execute(self, arguments: dict[str, Any], sandbox: Sandbox) -> ToolResult:
        """Push a branch on the host while keeping tokens outside the sandbox."""
        started = time.monotonic()
        host_path_raw = sandbox.host_workspace_path
        if not host_path_raw or not Path(host_path_raw).is_dir():
            return self._error(
                started,
                "Host workspace path is unavailable for host-brokered git_push.",
            )
        repo_root = Path(host_path_raw).resolve()

        raw_remote = str(arguments.get("remote", "origin")).strip() or "origin"
        try:
            remote = validate_ref_name(raw_remote, field_name="remote")
        except ValueError as exc:
            return self._error(started, str(exc))

        branch = await self._resolve_branch(repo_root, arguments, started)
        if isinstance(branch, ToolResult):
            return branch

        if branch in _PROTECTED_BRANCHES and not bool(arguments.get("allow_protected_branch")):
            return self._error(
                started,
                f"Refusing to push directly to protected branch '{branch}'. "
                "Create a feature branch or set allow_protected_branch=true.",
            )

        token_or_err = await self._resolve_push_token(repo_root, remote, started)
        if isinstance(token_or_err, ToolResult):
            return token_or_err

        push_args = self._build_push_args(remote, branch, arguments)
        code, stdout, stderr = await self._execute_push(repo_root, push_args, token_or_err)
        clean_out = scrub_secret(stdout, token_or_err)
        clean_err = scrub_secret(stderr, token_or_err)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if code != 0:
            return ToolResult(
                output=clean_out,
                error=clean_err.strip() or f"git push exited with status {code}",
                exit_code=code,
                execution_time_ms=elapsed_ms,
            )
        summary = clean_err.strip() or clean_out.strip() or f"Pushed {branch} to {remote}"
        return ToolResult(output=summary, exit_code=0, execution_time_ms=elapsed_ms)

    async def _execute_push(
        self,
        repo_root: Path,
        push_args: list[str],
        token: str | None,
    ) -> tuple[int, str, str]:
        if not token:
            return await _run_host_git(repo_root, *push_args)
        with tempfile.TemporaryDirectory(prefix="af-askpass-") as tmp_dir:
            askpass_path = Path(tmp_dir) / "askpass.py"
            askpass_path.write_text(
                f"#!{sys.executable}\n"
                "import os, sys\n"
                "prompt = sys.argv[1].lower() if len(sys.argv) > 1 else ''\n"
                "if 'username' in prompt:\n"
                "    print(os.environ.get('AGENT_FORGE_GIT_USER', 'x-access-token'))\n"
                "else:\n"
                "    print(os.environ.get('AGENT_FORGE_GIT_TOKEN', ''))\n",
                encoding="utf-8",
            )
            os.chmod(askpass_path, 0o700)
            return await _run_host_git(
                repo_root,
                *push_args,
                env_extra={
                    "GIT_ASKPASS": str(askpass_path),
                    "AGENT_FORGE_GIT_USER": "x-access-token",
                    "AGENT_FORGE_GIT_TOKEN": token,
                },
            )
