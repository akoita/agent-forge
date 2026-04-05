"""Extension scaffolding — generate new extension project structure.

The ``scaffold_extension`` function creates a canonical extension project
with pre-configured entry points, a sample profile, a sample tool, and
a test file.
"""

from __future__ import annotations

import re
from pathlib import Path
from string import Template

# ---------------------------------------------------------------------------
# Naming helpers
# ---------------------------------------------------------------------------

_TEMPLATES_DIR = Path(__file__).parent / "templates"


def _to_package_name(name: str) -> str:
    """Convert an extension name to a valid Python package name.

    ``my-security-scanner`` → ``my_security_scanner``
    """
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _to_class_prefix(name: str) -> str:
    """Convert an extension name to a CamelCase class prefix.

    ``my-security-scanner`` → ``MySecurityScanner``
    """
    return "".join(word.capitalize() for word in re.split(r"[^a-zA-Z0-9]+", name) if word)


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------


def _render_template(template_path: Path, context: dict[str, str]) -> str:
    """Read a template file and substitute ``$placeholders``.

    Uses :class:`string.Template` for safe, dependency-free substitution.
    """
    raw = template_path.read_text(encoding="utf-8")
    return Template(raw).safe_substitute(context)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class ScaffoldError(ValueError):
    """Raised when scaffolding fails."""


def scaffold_extension(name: str, target_dir: Path | None = None) -> Path:
    """Create a new Agent Forge extension project.

    Args:
        name: Human-readable extension name (e.g. ``my-security-scanner``).
        target_dir: Parent directory to create the project in.
            Defaults to the current working directory.

    Returns:
        Path to the created project root directory.

    Raises:
        ScaffoldError: If the target directory already exists or the
            name is invalid.
    """
    if not name or not re.match(r"^[a-zA-Z][a-zA-Z0-9_-]*$", name):
        msg = (
            f"Invalid extension name: '{name}'. "
            "Must start with a letter and contain only letters, digits, hyphens, underscores."
        )
        raise ScaffoldError(msg)

    package_name = _to_package_name(name)
    class_prefix = _to_class_prefix(name)

    parent = target_dir or Path.cwd()
    project_root = parent / name

    if project_root.exists():
        msg = f"Directory already exists: {project_root}"
        raise ScaffoldError(msg)

    # Build substitution context
    context = {
        "extension_name": name,
        "package_name": package_name,
        "class_prefix": class_prefix,
    }

    # Create directory structure
    pkg_dir = project_root / package_name
    (pkg_dir / "profiles").mkdir(parents=True, exist_ok=True)
    (pkg_dir / "prompts").mkdir(parents=True, exist_ok=True)
    (pkg_dir / "workflows").mkdir(parents=True, exist_ok=True)
    (pkg_dir / "tools").mkdir(parents=True, exist_ok=True)
    (project_root / "tests").mkdir(parents=True, exist_ok=True)

    # Render and write templates
    _write_template("pyproject.toml.template", project_root / "pyproject.toml", context)
    _write_template("README.md.template", project_root / "README.md", context)
    _write_template("__init__.py.template", pkg_dir / "__init__.py", context)
    _write_template(
        "profiles/default.yaml.template",
        pkg_dir / "profiles" / "default.yaml",
        context,
    )
    _write_template(
        "prompts/system_prompt.md.template",
        pkg_dir / "prompts" / "system_prompt.md",
        context,
    )
    _write_template(
        "workflows/sample-workflow.md.template",
        pkg_dir / "workflows" / "sample-workflow.md",
        context,
    )
    _write_template(
        "tools/sample_tool.py.template",
        pkg_dir / "tools" / "sample_tool.py",
        context,
    )
    # Tools __init__ (empty)
    (pkg_dir / "tools" / "__init__.py").write_text(
        f'"""Tools for the {name} extension."""\n', encoding="utf-8"
    )
    _write_template(
        "tests/test_sample_tool.py.template",
        project_root / "tests" / "test_sample_tool.py",
        context,
    )

    return project_root


def _write_template(template_name: str, output_path: Path, context: dict[str, str]) -> None:
    """Render a template and write it to the output path."""
    template_path = _TEMPLATES_DIR / template_name
    if not template_path.is_file():
        msg = f"Template not found: {template_path}"
        raise ScaffoldError(msg)

    content = _render_template(template_path, context)
    output_path.write_text(content, encoding="utf-8")
