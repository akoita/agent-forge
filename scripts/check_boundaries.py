#!/usr/bin/env python3
"""Domain-boundary checker for the agent-forge core.

Enforces the domain-agnostic-core rule declared in ``AGENTS.md``: packages
under ``agent_forge/`` must stay free of audit-domain vocabulary. Domain work
belongs in ``plugins/`` (see the Proof-of-Audit extraction tracked in issue
#140).

The checker is a *ratchet*:

* Any forbidden term in a file that is **not** listed in the baseline is a new
  violation and fails the run.
* Every baseline entry must still contain at least one violation. A baseline
  file that is now clean (or that no longer exists) is a *stale* entry and also
  fails the run, so the baseline can only ever shrink.

A line containing the ``# boundary-ok`` pragma is exempt, for the rare
legitimate English use of a flagged word (e.g. "finding") in a comment or
docstring.

Usage::

    python scripts/check_boundaries.py
    python scripts/check_boundaries.py --baseline path/to/baseline.txt

Exit code ``0`` means the boundary is intact; ``1`` means a new violation or a
stale baseline entry was found.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_CORE_DIR = _REPO_ROOT / "agent_forge"
_DEFAULT_BASELINE = _REPO_ROOT / "scripts" / "boundary_baseline.txt"

_PRAGMA = "# boundary-ok"

# Audit-domain vocabulary that must not appear in core packages. Multiword and
# hyphenated terms (e.g. "proof-of-audit") match across ``-``, ``_`` or space
# separators. Matching is case-insensitive and anchored on ASCII-alphanumeric
# boundaries (rather than Python's ``\b``, which treats ``_`` as a word char).
# This means snake_case leakage such as ``build_proof_of_audit_request`` is
# caught, while an alphabetic extension like "auditor" does not trip "audit"
# and "poang" does not trip "poa".
_FORBIDDEN_TERMS: tuple[str, ...] = (
    "audit",
    "solidity",
    "vulnerability",
    "vulnerabilities",
    "severity",
    "finding",
    "findings",
    "detector",
    "detectors",
    "reentrancy",
    "proof-of-audit",
    "poa",
    "exploit",
)


def _compile_terms(terms: tuple[str, ...]) -> list[tuple[str, re.Pattern[str]]]:
    """Compile each forbidden term into a case-insensitive, boundary-anchored regex.

    Args:
        terms: The forbidden vocabulary. Multiword or hyphenated terms may use
            any of ``-``, ``_`` or space as separators.

    Returns:
        A list of ``(term, pattern)`` pairs preserving input order.
    """
    compiled: list[tuple[str, re.Pattern[str]]] = []
    for term in terms:
        parts = re.split(r"[-_ ]+", term)
        body = r"[-_ ]+".join(re.escape(part) for part in parts)
        pattern = rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])"
        compiled.append((term, re.compile(pattern, re.IGNORECASE)))
    return compiled


def _iter_py_files(root: Path) -> list[Path]:
    """Return every ``*.py`` file under ``root``, sorted for determinism."""
    return sorted(root.rglob("*.py"))


def _scan_file(
    path: Path, patterns: list[tuple[str, re.Pattern[str]]]
) -> list[tuple[int, str, str]]:
    """Scan a single file for forbidden terms.

    Args:
        path: The file to scan.
        patterns: Compiled ``(term, pattern)`` pairs from :func:`_compile_terms`.

    Returns:
        A list of ``(line_number, term, line_text)`` tuples, one per matched
        term on each line. Lines carrying the ``# boundary-ok`` pragma are
        skipped.
    """
    violations: list[tuple[int, str, str]] = []
    text = path.read_text(encoding="utf-8")
    for lineno, line in enumerate(text.splitlines(), start=1):
        if _PRAGMA in line:
            continue
        for term, pattern in patterns:
            if pattern.search(line):
                violations.append((lineno, term, line.strip()))
    return violations


def _load_baseline(baseline_path: Path) -> list[str]:
    """Load repo-relative paths from the baseline file.

    Blank lines and ``#`` comments are ignored. A missing baseline file yields
    an empty list (equivalent to "no exemptions").

    Args:
        baseline_path: Path to the baseline file.

    Returns:
        The list of repo-relative path strings, in file order.
    """
    if not baseline_path.exists():
        return []
    entries: list[str] = []
    for raw in baseline_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        entries.append(line)
    return entries


def main(argv: list[str] | None = None) -> int:
    """Run the boundary check.

    Args:
        argv: Optional argument vector (defaults to ``sys.argv[1:]``).

    Returns:
        ``0`` if the boundary is intact, ``1`` otherwise.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--core-dir",
        type=Path,
        default=_DEFAULT_CORE_DIR,
        help="Directory to scan for forbidden terms (default: agent_forge/).",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=_DEFAULT_BASELINE,
        help="Baseline file of ratcheted exemptions (default: scripts/boundary_baseline.txt).",
    )
    args = parser.parse_args(argv)

    core_dir: Path = args.core_dir.resolve()
    baseline_path: Path = args.baseline
    base_dir = core_dir.parent

    baseline_entries = _load_baseline(baseline_path)
    baseline_set = set(baseline_entries)
    patterns = _compile_terms(_FORBIDDEN_TERMS)

    files = _iter_py_files(core_dir)
    new_violations: list[tuple[str, int, str, str]] = []
    baseline_hits: set[str] = set()

    for path in files:
        rel = path.relative_to(base_dir).as_posix()
        file_violations = _scan_file(path, patterns)
        if not file_violations:
            continue
        if rel in baseline_set:
            baseline_hits.add(rel)
        else:
            for lineno, term, text in file_violations:
                new_violations.append((rel, lineno, term, text))

    stale_entries = [entry for entry in baseline_entries if entry not in baseline_hits]

    exit_code = 0
    if new_violations:
        exit_code = 1
        print("New boundary violations (not in baseline):", file=sys.stderr)
        for rel, lineno, term, text in new_violations:
            print(f"  {rel}:{lineno}: forbidden term '{term}' -> {text}", file=sys.stderr)
    if stale_entries:
        exit_code = 1
        print(
            "Stale baseline entries (now clean or missing) — remove them, "
            "the baseline may only shrink:",
            file=sys.stderr,
        )
        for entry in stale_entries:
            print(f"  {entry}", file=sys.stderr)

    if exit_code == 0:
        print(
            f"boundary check OK: scanned {len(files)} files, "
            f"baseline size {len(baseline_entries)}"
        )

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
