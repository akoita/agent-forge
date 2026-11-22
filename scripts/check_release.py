#!/usr/bin/env python3
"""Verify package version consistency and release tag alignment.

Usage::

    python scripts/check_release.py
    python scripts/check_release.py --tag v0.3.0
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_DEFAULT_INIT = _REPO_ROOT / "agent_forge" / "__init__.py"
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
_VERSION_LINE_RE = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)


def read_pyproject_version(path: Path) -> str:
    """Return `[project].version` from *path*."""
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    project = data.get("project")
    if not isinstance(project, dict):
        msg = f"Missing [project].version in {path}"
        raise ValueError(msg)
    version = project.get("version")
    if not isinstance(version, str):
        msg = f"Missing [project].version in {path}"
        raise ValueError(msg)
    return version


def read_init_version(path: Path) -> str:
    """Return `__version__` from *path*."""
    match = _VERSION_LINE_RE.search(path.read_text(encoding="utf-8"))
    if match is None:
        msg = f"Missing __version__ assignment in {path}"
        raise ValueError(msg)
    return match.group(1)


def validate_release(
    pyproject_path: Path,
    init_path: Path,
    *,
    tag: str | None = None,
) -> str:
    """Validate version format, cross-file parity, and optional release tag.

    Returns the verified version string. Raises ``ValueError`` on mismatch.
    """
    pyproject_version = read_pyproject_version(pyproject_path)
    init_version = read_init_version(init_path)

    if not _SEMVER_RE.fullmatch(pyproject_version):
        msg = f"Invalid semver version in {pyproject_path}: '{pyproject_version}'"
        raise ValueError(msg)
    if pyproject_version != init_version:
        msg = (
            f"Version mismatch: {pyproject_path} has '{pyproject_version}' "
            f"but {init_path} has '{init_version}'"
        )
        raise ValueError(msg)

    if tag is not None:
        if not tag.startswith("v"):
            msg = f"Release tag '{tag}' must start with 'v' (expected 'v{pyproject_version}')"
            raise ValueError(msg)
        if tag[1:] != pyproject_version:
            msg = f"Release tag '{tag}' does not match package version '{pyproject_version}'"
            raise ValueError(msg)

    return pyproject_version


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for release version validation."""
    parser = argparse.ArgumentParser(description="Validate release version consistency.")
    parser.add_argument("--tag", help="Release git tag to verify against package version (vX.Y.Z)")
    parser.add_argument("--pyproject", type=Path, default=_DEFAULT_PYPROJECT)
    parser.add_argument("--init", type=Path, default=_DEFAULT_INIT)
    args = parser.parse_args(argv)

    try:
        version = validate_release(args.pyproject, args.init, tag=args.tag)
    except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
        print(f"Release check failed: {exc}", file=sys.stderr)
        return 1

    suffix = f" (tag {args.tag})" if args.tag is not None else ""
    print(f"Release version verified: {version}{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
