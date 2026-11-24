"""Polyglot sandbox toolchain auto-detection and image selection helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Final

DEFAULT_SANDBOX_IMAGE: Final[str] = "agent-forge-sandbox:latest"

TOOLCHAIN_IMAGES: Final[dict[str, str]] = {
    "python": DEFAULT_SANDBOX_IMAGE,
    "node": "agent-forge-sandbox:node",
    "go": "agent-forge-sandbox:go",
    "rust": "agent-forge-sandbox:rust",
    "jvm": "agent-forge-sandbox:jvm",
    "full": "agent-forge-sandbox:full",
}

VALID_TOOLCHAINS: Final[frozenset[str]] = frozenset({"auto", *TOOLCHAIN_IMAGES.keys()})

_TOOLCHAIN_MARKERS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("go", ("go.mod", "go.work")),
    ("rust", ("Cargo.toml",)),
    (
        "jvm",
        ("pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"),
    ),
    ("node", ("package.json", "pnpm-workspace.yaml", "tsconfig.json")),
    ("python", ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Pipfile")),
)


def detect_toolchain(repo_path: str | Path) -> str:
    """Detect the primary language toolchain from top-level project marker files.

    Falls back to ``"python"`` for empty greenfield or unrecognized directories.
    Returns ``"full"`` when both Python and Node markers are present.
    """
    root = Path(repo_path)
    if not root.is_dir():
        return "python"

    try:
        entries = {entry.name for entry in root.iterdir()}
    except OSError:
        return "python"

    matched: list[str] = []
    for toolchain, markers in _TOOLCHAIN_MARKERS:
        if any(marker in entries for marker in markers):
            matched.append(toolchain)

    if set(matched) == {"python", "node"}:
        return "full"
    return matched[0] if matched else "python"


def resolve_sandbox_image(
    image: str,
    *,
    toolchain: str = "auto",
    repo_path: str | Path | None = None,
) -> tuple[str, str]:
    """Resolve the sandbox container image and effective toolchain preset.

    Explicit custom images (anything other than ``DEFAULT_SANDBOX_IMAGE``) always
    take precedence over toolchain selection.
    """
    normalized = toolchain.strip().lower() or "auto"
    if normalized not in VALID_TOOLCHAINS:
        valid = ", ".join(sorted(VALID_TOOLCHAINS))
        msg = f"Unknown sandbox toolchain '{toolchain}'. Valid options: {valid}"
        raise ValueError(msg)

    if image and image != DEFAULT_SANDBOX_IMAGE:
        return image, normalized

    if normalized != "auto":
        return TOOLCHAIN_IMAGES[normalized], normalized

    if repo_path is not None:
        detected = detect_toolchain(repo_path)
        return TOOLCHAIN_IMAGES[detected], detected

    return image or DEFAULT_SANDBOX_IMAGE, "auto"
