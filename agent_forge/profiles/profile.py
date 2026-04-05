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
    capabilities: list[str] = Field(default_factory=list)


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
    discover_entry_points: bool = True,
) -> dict[str, AgentProfile]:
    """Load agent profiles from one or more directories.

    Precedence (later overrides earlier on duplicate ``id``):

    1. Built-in profiles (``agent_forge/profiles/builtins/``)
    2. Extension profiles (``agent_forge.profiles`` entry-point group)
    3. User-provided directories (``--profiles-dir``)

    Args:
        dirs: Additional directories to scan for ``*.yaml``/``*.yml`` files.
            Later directories override earlier ones on duplicate ``id``.
        include_builtins: If ``True`` (default), load the built-in profiles
            shipped with the package before any user-provided directories.
        discover_entry_points: If ``True`` (default), discover profile
            directories from installed extensions via the
            ``agent_forge.profiles`` entry-point group.

    Returns:
        A mapping of profile ``id`` → ``AgentProfile``.
    """
    scan_dirs: list[Path] = []

    # Layer 1: Built-in profiles (lowest precedence)
    if include_builtins and _BUILTIN_PROFILES_DIR.is_dir():
        scan_dirs.append(_BUILTIN_PROFILES_DIR)

    # Layer 2: Extension profiles (entry-point discovery)
    if discover_entry_points:
        from agent_forge.extensions.discovery import discover_extension_profile_dirs

        scan_dirs.extend(discover_extension_profile_dirs())

    # Layer 3: User-provided directories (highest precedence)
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
