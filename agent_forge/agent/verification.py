"""Capture a coding patch and verify it from an independently prepared checkout."""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from agent_forge.llm.errors import SandboxError
from agent_forge.sandbox.factory import create_sandbox

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import TracebackType

    from agent_forge.sandbox.base import Sandbox, SandboxConfig


@dataclass
class VerificationResult:
    """Evidence produced by the configured check."""

    base_revision: str
    patch: str
    reason: str
    command: str
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""

    @property
    def passed(self) -> bool:
        """Return whether the independent check succeeded."""
        return self.reason == "verified"


def _git(directory: Path, *args: str, patch: bytes | None = None) -> bytes:
    result = subprocess.run(  # noqa: S603
        ["git", "-c", "core.hooksPath=" + os.devnull, *args],  # noqa: S607
        cwd=directory,
        input=patch,
        env={
            "PATH": os.environ.get("PATH", os.defpath),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        },
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode:
        detail = result.stderr.decode(errors="replace").strip()
        msg = f"Git {args[0]} failed: {detail}"
        raise ValueError(msg)
    return result.stdout


class VerificationWorkspace:
    """Pair an editable checkout with an independent verifier checkout."""

    def __init__(
        self,
        repository: str,
        command: str,
        *,
        base_revision: str | None = None,
        initial_patch: str = "",
        extra_verifier_files: dict[str, str] | None = None,
    ) -> None:
        """Select the repository and caller-supplied verification command."""
        self._repository = Path(repository).resolve()
        self._command = command
        self._requested_base_revision = base_revision
        self._initial_patch = initial_patch
        self._extra_verifier_files = dict(extra_verifier_files or {})
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self.workspace: Path
        self.base_revision: str
        self._verifier: Path

    def __enter__(self) -> VerificationWorkspace:
        """Prepare both workspaces at the same committed revision."""
        temporary = tempfile.TemporaryDirectory(prefix="agent-forge-verify-")
        root = Path(temporary.name)
        self.workspace = root / "candidate"
        self._verifier = root / "verifier"
        try:
            self.base_revision = (
                _git(
                    self._repository,
                    "rev-parse",
                    "--verify",
                    self._requested_base_revision or "HEAD",
                )
                .decode()
                .strip()
            )
            for destination in (self.workspace, self._verifier):
                _git(
                    root,
                    "clone",
                    "--no-local",
                    "--no-checkout",
                    "--template=",
                    "--",
                    str(self._repository),
                    str(destination),
                )
                _git(destination, "checkout", "--detach", self.base_revision)
            if self._initial_patch:
                _git(
                    self.workspace,
                    "apply",
                    "-",
                    patch=self._initial_patch.encode(errors="surrogateescape"),
                )
        except BaseException:
            temporary.cleanup()
            raise
        self._temporary = temporary
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        """Remove the temporary checkouts."""
        if self._temporary is not None:
            self._temporary.cleanup()

    def _capture_patch(self) -> bytes:
        git_dir = str(self._verifier / ".git")
        _git(
            self._verifier,
            "--git-dir",
            git_dir,
            "--work-tree",
            str(self.workspace),
            "add",
            "--all",
            "--",
            ".",
        )
        try:
            return _git(
                self._verifier,
                "diff",
                "--cached",
                "--binary",
                "--no-ext-diff",
                "--no-textconv",
                self.base_revision,
                "--",
            )
        finally:
            _git(self._verifier, "read-tree", self.base_revision)

    def capture_patch(self) -> str:
        """Capture the candidate workspace diff against the pinned base revision."""
        return self._capture_patch().decode(errors="surrogateescape")

    def _write_extra_verifier_files(self) -> None:
        verifier_root = self._verifier.resolve()
        for rel_path, content in self._extra_verifier_files.items():
            if Path(rel_path).is_absolute():
                msg = f"Verifier file path must be relative: {rel_path}"
                raise ValueError(msg)
            target = (verifier_root / rel_path).resolve()
            if not target.is_relative_to(verifier_root) or target == verifier_root:
                msg = f"Verifier file path escapes verifier checkout: {rel_path}"
                raise ValueError(msg)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    async def verify(
        self,
        config: SandboxConfig,
        candidate: Sandbox,
        *,
        sandbox_factory: Callable[[SandboxConfig], Sandbox] | None = None,
    ) -> VerificationResult:
        """Apply the candidate patch in the verifier checkout and run the check."""
        result = VerificationResult(self.base_revision, "", "verification_error", self._command)
        try:
            await candidate.stop()
            # Git operations are bounded. Complete each operation before removing the workspaces.
            patch = self._capture_patch()
            if not patch:
                return replace(result, reason="empty_patch")
            result.patch = patch.decode(errors="surrogateescape")
            _git(self._verifier, "apply", "-", patch=patch)
            self._write_extra_verifier_files()
            verification_config = replace(
                config, network_enabled=False, writable_cache_mounts=False
            )
            sandbox = (sandbox_factory or create_sandbox)(verification_config)
            try:
                await sandbox.start(str(self._verifier), verification_config)
                check = await sandbox.exec(self._command, timeout_seconds=config.timeout_seconds)
            finally:
                await sandbox.stop()
            return replace(
                result,
                reason="verified" if check.exit_code == 0 else "check_failed",
                exit_code=check.exit_code,
                stdout=check.stdout,
                stderr=check.stderr,
            )
        except (
            ValueError,
            OSError,
            RuntimeError,
            SandboxError,
            subprocess.TimeoutExpired,
        ) as exc:
            return replace(result, stderr=str(exc))
