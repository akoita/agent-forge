"""Unit tests for ``scripts/check_boundaries.py`` (the domain-boundary ratchet).

The script lives under ``scripts/`` rather than the ``agent_forge`` package, so
it is loaded here by file path via :mod:`importlib`.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

    import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_boundaries.py"


def _load_module() -> ModuleType:
    """Import ``check_boundaries.py`` as a module from its file path."""
    spec = importlib.util.spec_from_file_location("check_boundaries", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cb: ModuleType = _load_module()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pkg(root: Path) -> Path:
    """Create and return a fake ``agent_forge`` package under ``root``."""
    pkg = root / "agent_forge"
    pkg.mkdir()
    return pkg


def _write(pkg: Path, name: str, content: str) -> None:
    """Write ``content`` to ``pkg/name``."""
    (pkg / name).write_text(content, encoding="utf-8")


def _baseline(root: Path, *entries: str) -> Path:
    """Write a baseline file listing ``entries`` and return its path."""
    path = root / "baseline.txt"
    path.write_text("\n".join(entries) + "\n", encoding="utf-8")
    return path


def _run(pkg: Path, baseline: Path) -> int:
    """Invoke the checker against ``pkg`` with ``baseline``."""
    return int(cb.main(["--core-dir", str(pkg), "--baseline", str(baseline)]))


# ---------------------------------------------------------------------------
# Ratchet behaviour
# ---------------------------------------------------------------------------


def test_clean_tree_passes(tmp_path: Path) -> None:
    pkg = _make_pkg(tmp_path)
    _write(pkg, "mod.py", "def hello() -> str:\n    return 'ok'\n")
    assert _run(pkg, _baseline(tmp_path)) == 0


def test_new_violation_outside_baseline_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pkg = _make_pkg(tmp_path)
    _write(pkg, "leak.py", "# harmless\nSEVERITY = 'high'\n")
    code = _run(pkg, _baseline(tmp_path))
    err = capsys.readouterr().err
    assert code == 1
    assert "leak.py" in err
    assert ":2:" in err
    assert "severity" in err


def test_violation_in_baselined_file_passes(tmp_path: Path) -> None:
    pkg = _make_pkg(tmp_path)
    _write(pkg, "leak.py", "severity = 1\n")
    baseline = _baseline(tmp_path, "agent_forge/leak.py")
    assert _run(pkg, baseline) == 0


def test_clean_baselined_file_is_stale(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pkg = _make_pkg(tmp_path)
    _write(pkg, "clean.py", "x = 1\n")
    baseline = _baseline(tmp_path, "agent_forge/clean.py")
    code = _run(pkg, baseline)
    err = capsys.readouterr().err
    assert code == 1
    assert "agent_forge/clean.py" in err
    assert "stale" in err.lower()


def test_missing_baselined_file_is_stale(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pkg = _make_pkg(tmp_path)
    _write(pkg, "present.py", "x = 1\n")
    baseline = _baseline(tmp_path, "agent_forge/gone.py")
    code = _run(pkg, baseline)
    err = capsys.readouterr().err
    assert code == 1
    assert "agent_forge/gone.py" in err


def test_boundary_ok_pragma_exempts_line(tmp_path: Path) -> None:
    pkg = _make_pkg(tmp_path)
    _write(pkg, "mod.py", "note = 'a real finding worth reporting'  # boundary-ok\n")
    assert _run(pkg, _baseline(tmp_path)) == 0


# ---------------------------------------------------------------------------
# Word-boundary semantics
# ---------------------------------------------------------------------------


def test_alphabetic_extension_does_not_match(tmp_path: Path) -> None:
    """Documented choice: ASCII-alnum boundaries treat "auditor" as a distinct
    word, so it does NOT trip "audit"; likewise "poang" does not trip "poa"."""
    pkg = _make_pkg(tmp_path)
    _write(pkg, "mod.py", "auditor = 1\nauditing = 2\npoang = 3\n")
    assert _run(pkg, _baseline(tmp_path)) == 0


def test_snake_case_identifier_matches(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Underscores act as separators, so audit-domain identifiers are caught."""
    pkg = _make_pkg(tmp_path)
    _write(pkg, "mod.py", "def build_proof_of_audit_request() -> None:\n    return None\n")
    code = _run(pkg, _baseline(tmp_path))
    err = capsys.readouterr().err
    assert code == 1
    assert "proof-of-audit" in err


def test_term_matcher_boundaries() -> None:
    patterns = dict(cb._compile_terms(cb._FORBIDDEN_TERMS))
    audit = patterns["audit"]
    assert audit.search("an audit report") is not None
    assert audit.search("do_audit_now") is not None
    assert audit.search("AUDIT") is not None
    assert audit.search("auditor") is None
    assert audit.search("auditing") is None
    poa = patterns["poa"]
    assert poa.search("emit POA now") is not None
    assert poa.search("poang") is None
