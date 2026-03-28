"""Shared helpers for git-aware tools."""

from __future__ import annotations

import os
import posixpath
import re
import shlex
from urllib.parse import urlparse

from agent_forge.tools.base import WORKSPACE_ROOT, validate_path

_BRANCH_NAME_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_REVISION_RE = re.compile(r"^[A-Za-z0-9._/@~^:-]+$")


def resolve_git_path(path: str) -> str:
    """Validate a workspace path and convert it to a git-relative pathspec."""
    resolved = validate_path(path)
    if resolved == WORKSPACE_ROOT:
        return "."
    return posixpath.relpath(resolved, WORKSPACE_ROOT)


def validate_ref_name(ref: str, *, field_name: str) -> str:
    """Validate a git ref or branch name used by tool arguments."""
    value = ref.strip()
    if not value:
        msg = f"Missing required argument: {field_name}"
        raise ValueError(msg)
    if value.startswith("-"):
        msg = f"Invalid {field_name}: cannot start with '-'"
        raise ValueError(msg)
    if ".." in value or value.endswith("/") or value.startswith("/") or "@{" in value:
        msg = f"Invalid {field_name}: '{ref}'"
        raise ValueError(msg)
    if not _BRANCH_NAME_RE.fullmatch(value):
        msg = f"Invalid {field_name}: '{ref}'"
        raise ValueError(msg)
    return value


def validate_revision(revision: str, *, field_name: str) -> str:
    """Validate a git revision expression used for diff-style lookups."""
    value = revision.strip()
    if not value:
        msg = f"Missing required argument: {field_name}"
        raise ValueError(msg)
    if value.startswith("-") or ".." in value or "@{" in value:
        msg = f"Invalid {field_name}: '{revision}'"
        raise ValueError(msg)
    if not _REVISION_RE.fullmatch(value):
        msg = f"Invalid {field_name}: '{revision}'"
        raise ValueError(msg)
    return value


def quote_pathspec(pathspec: str) -> str:
    """Quote a git pathspec for shell execution."""
    return shlex.quote(pathspec)


def github_token() -> str | None:
    """Return a GitHub API token from the process environment if present."""
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")


def parse_github_repo(remote_url: str) -> str:
    """Parse an origin remote URL into owner/repo form."""
    remote = remote_url.strip()
    if not remote:
        msg = "Unable to determine GitHub repository from git remote."
        raise ValueError(msg)

    if remote.startswith("git@github.com:"):
        suffix = remote.removeprefix("git@github.com:")
    else:
        parsed = urlparse(remote)
        if parsed.netloc != "github.com":
            msg = f"Unsupported git remote host: '{remote}'"
            raise ValueError(msg)
        suffix = parsed.path.lstrip("/")

    if suffix.endswith(".git"):
        suffix = suffix[:-4]

    parts = [part for part in suffix.split("/") if part]
    if len(parts) != 2:
        msg = f"Unsupported GitHub remote format: '{remote}'"
        raise ValueError(msg)
    return "/".join(parts)
