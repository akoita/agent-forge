"""Agent profile system — configurable persona and prompt scoping.

An agent profile configures the agent's behavior for a given task type:
prompt scope, LLM provider/model overrides, and iteration limits.
Profiles are defined as YAML files and loaded from one or more directories.

Domain-specific profiles (e.g. smart-contract audit, security scanning)
live in the extension layer (plugins/, --profiles-dir), not in core.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Built-in profile directory
# ---------------------------------------------------------------------------

_BUILTIN_PROFILES_DIR = Path(__file__).parent / "builtins"


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class AgentProfile(BaseModel):
    """Schema for a single agent profile YAML file."""

    id: str
    name: str
    description: str = ""
    prompt_scope: str = ""
    llm_provider: str | None = None
    llm_model: str | None = None
    max_iterations: int | None = Field(default=None, ge=1)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _load_yaml_file(path: Path) -> AgentProfile:
    """Parse a single YAML file into an ``AgentProfile``."""
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict):
        msg = f"profile file must contain a YAML mapping: {path}"
        raise ValueError(msg)
    return AgentProfile.model_validate(raw)


def load_profiles(
    dirs: list[Path] | None = None,
    *,
    include_builtins: bool = True,
) -> dict[str, AgentProfile]:
    """Load agent profiles from one or more directories.

    Args:
        dirs: Additional directories to scan for ``*.yaml``/``*.yml`` files.
            Later directories override earlier ones on duplicate ``id``.
        include_builtins: If ``True`` (default), load the built-in profiles
            shipped with the package before any user-provided directories.

    Returns:
        A mapping of profile ``id`` → ``AgentProfile``.
    """
    scan_dirs: list[Path] = []

    if include_builtins and _BUILTIN_PROFILES_DIR.is_dir():
        scan_dirs.append(_BUILTIN_PROFILES_DIR)

    if dirs:
        scan_dirs.extend(dirs)

    registry: dict[str, AgentProfile] = {}

    for directory in scan_dirs:
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if path.suffix not in {".yaml", ".yml"}:
                continue
            profile = _load_yaml_file(path)
            registry[profile.id] = profile

    return registry


def get_profile(
    profile_id: str,
    registry: dict[str, AgentProfile],
) -> AgentProfile:
    """Look up a profile by ID, raising a clear error if missing.

    Args:
        profile_id: The ``id`` field of the desired profile.
        registry: Profile registry returned by :func:`load_profiles`.

    Returns:
        The matching ``AgentProfile``.

    Raises:
        KeyError: If the profile is not found.
    """
    if profile_id in registry:
        return registry[profile_id]

    available = ", ".join(sorted(registry)) or "(none)"
    msg = f"unknown profile: '{profile_id}'. Available profiles: {available}"
    raise KeyError(msg)
