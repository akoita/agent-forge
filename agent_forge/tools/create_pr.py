"""create_pr tool — open a GitHub pull request for the current repository."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

import httpx

from agent_forge.tools.base import Tool, ToolResult
from agent_forge.tools.git_common import github_token, parse_github_repo, validate_ref_name

if TYPE_CHECKING:
    from collections.abc import Callable

    from agent_forge.sandbox.base import Sandbox


class CreatePRTool(Tool):
    """Create a GitHub pull request via the REST API."""

    @property
    def name(self) -> str:
        return "create_pr"

    @property
    def description(self) -> str:
        return "Create a GitHub pull request for the current repository via the GitHub API"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Pull request title"},
                "body": {
                    "type": "string",
                    "description": "Optional pull request body",
                    "default": "",
                },
                "base": {
                    "type": "string",
                    "description": (
                        "Optional base branch name "
                        "(defaults to repository default branch)"
                    ),
                },
                "head": {
                    "type": "string",
                    "description": "Optional head branch name (defaults to current branch)",
                },
                "repo": {
                    "type": "string",
                    "description": "Optional owner/repo override (defaults to origin remote)",
                },
            },
            "required": ["title"],
        }

    @staticmethod
    def _timed_error(start: float, message: str, exit_code: int = 1) -> ToolResult:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return ToolResult(
            output="",
            error=message,
            exit_code=exit_code,
            execution_time_ms=elapsed_ms,
        )

    async def _resolve_repo(
        self,
        arguments: dict[str, Any],
        sandbox: Sandbox,
        start: float,
    ) -> str | ToolResult:
        repo = str(arguments.get("repo", "")).strip()
        if repo:
            if repo.count("/") != 1:
                return self._timed_error(start, "Invalid repo argument. Expected 'owner/repo'.")
            return repo

        remote_result = await sandbox.exec(
            "git -C /workspace remote get-url origin",
            timeout_seconds=30,
        )
        if remote_result.exit_code != 0:
            return self._timed_error(
                start,
                remote_result.stderr or "Unable to determine origin remote",
                exit_code=remote_result.exit_code,
            )

        try:
            return parse_github_repo(remote_result.stdout)
        except ValueError as exc:
            return self._timed_error(start, str(exc))

    async def _resolve_branch(
        self,
        *,
        arguments: dict[str, Any],
        sandbox: Sandbox,
        start: float,
        argument_key: str,
        field_name: str,
        command: str,
        default_error: str,
        transform: Callable[[str], str] | None = None,
    ) -> str | ToolResult:
        value = str(arguments.get(argument_key, "")).strip()
        if not value:
            result = await sandbox.exec(command, timeout_seconds=30)
            if result.exit_code != 0:
                return self._timed_error(
                    start,
                    result.stderr or default_error,
                    exit_code=result.exit_code,
                )
            value = result.stdout.strip()
            if transform is not None:
                value = transform(value)

        try:
            return validate_ref_name(value, field_name=field_name)
        except ValueError as exc:
            return self._timed_error(start, str(exc))

    async def _post_pull_request(
        self,
        *,
        repo: str,
        token: str,
        payload: dict[str, str],
        start: float,
    ) -> ToolResult:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        url = f"https://api.github.com/repos/{repo}/pulls"

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
                response = await client.post(url, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            return self._timed_error(start, f"GitHub API request failed: {exc}")

        elapsed_ms = int((time.monotonic() - start) * 1000)
        if response.status_code >= 400:
            details = response.text
            try:
                data = response.json()
            except json.JSONDecodeError:
                data = None
            if isinstance(data, dict) and isinstance(data.get("message"), str):
                details = data["message"]
            return ToolResult(
                output="",
                error=f"GitHub API error ({response.status_code}): {details}",
                exit_code=1,
                execution_time_ms=elapsed_ms,
            )

        data = response.json()
        pr_url = data.get("html_url", "")
        pr_number = data.get("number", "")
        output = pr_url or json.dumps(data)
        if pr_number:
            output = f"Created pull request #{pr_number}: {pr_url}"
        return ToolResult(output=output, exit_code=0, execution_time_ms=elapsed_ms)

    async def execute(self, arguments: dict[str, Any], sandbox: Sandbox) -> ToolResult:
        """Create a pull request using repository data from git and auth from env."""
        title = str(arguments.get("title", "")).strip()
        if not title:
            return ToolResult(output="", error="Missing required argument: title", exit_code=1)

        token = github_token()
        if not token:
            return ToolResult(
                output="",
                error="Missing GitHub token. Set GITHUB_TOKEN or GH_TOKEN.",
                exit_code=1,
            )

        start = time.monotonic()
        repo = await self._resolve_repo(arguments, sandbox, start)
        if isinstance(repo, ToolResult):
            return repo

        head = await self._resolve_branch(
            arguments=arguments,
            sandbox=sandbox,
            start=start,
            argument_key="head",
            field_name="head",
            command="git -C /workspace rev-parse --abbrev-ref HEAD",
            default_error="Unable to determine current branch",
        )
        if isinstance(head, ToolResult):
            return head

        base = await self._resolve_branch(
            arguments=arguments,
            sandbox=sandbox,
            start=start,
            argument_key="base",
            field_name="base",
            command="git -C /workspace symbolic-ref --quiet --short refs/remotes/origin/HEAD",
            default_error="Unable to determine default base branch",
            transform=lambda value: value.removeprefix("origin/"),
        )
        if isinstance(base, ToolResult):
            return base

        payload = {
            "title": title,
            "head": head,
            "base": base,
            "body": str(arguments.get("body", "")),
        }
        return await self._post_pull_request(repo=repo, token=token, payload=payload, start=start)
