"""Evaluate curated patches against an independent copy of the fixture tests."""

import difflib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree

import pytest

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "sample_repo"
BUGGY_LINE = '    return jsonify({"status": "error"}), 500'


@pytest.mark.parametrize(
    ("replacement", "expected_failures"),
    [
        pytest.param(None, ["test_health_returns_200"], id="buggy-base"),
        pytest.param(BUGGY_LINE, ["test_health_returns_200"], id="empty-patch"),
        pytest.param(
            '    return jsonify({"status": "ok"}), 503',
            ["test_health_returns_200"],
            id="incorrect-patch",
        ),
        pytest.param('    return jsonify({"status": "ok"}), 200', [], id="known-good-patch"),
    ],
)
def test_health_patch(
    tmp_path: Path, replacement: str | None, expected_failures: list[str]
) -> None:
    """Apply a candidate on a fresh fixture and grade it with verifier-owned tests."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for name in ("app.py", "utils.py"):
        shutil.copyfile(FIXTURE / name, workspace / name)

    original = (workspace / "app.py").read_text(encoding="utf-8")
    assert original.count(BUGGY_LINE) == 1

    if replacement is not None:
        candidate = original.replace(BUGGY_LINE, replacement, 1)
        patch = "".join(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                candidate.splitlines(keepends=True),
                fromfile="a/app.py",
                tofile="b/app.py",
            )
        )
        if patch:
            subprocess.run(
                ["git", "apply", "-"],  # noqa: S607
                input=patch,
                cwd=workspace,
                text=True,
                check=True,
                timeout=10,
            )
        assert (workspace / "app.py").read_text(encoding="utf-8") == candidate

    verifier = tmp_path / "verifier"
    verifier.mkdir()
    shutil.copyfile(FIXTURE / "test_app.py", verifier / "test_app.py")
    report = verifier / "report.xml"
    checks = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pytest", "-q", "--junitxml", str(report), "test_app.py"],
        cwd=verifier,
        env={
            **os.environ,
            "PYTHONPATH": str(workspace),
            "PYTEST_ADDOPTS": "",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    output = checks.stdout + checks.stderr
    assert checks.returncode == (1 if expected_failures else 0), output
    cases = ElementTree.parse(report).findall(".//testcase")  # noqa: S314
    assert {case.attrib["name"] for case in cases} == {
        "test_index",
        "test_health_returns_200",
        "test_greet",
    }
    assert all(case.find("error") is None and case.find("skipped") is None for case in cases)
    assert [case.attrib["name"] for case in cases if case.find("failure") is not None] == (
        expected_failures
    )
