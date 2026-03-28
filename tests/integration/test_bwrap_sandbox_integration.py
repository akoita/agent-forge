"""Integration tests for tools executing in a real bubblewrap sandbox."""

from __future__ import annotations

import platform
import shutil
import tempfile
from pathlib import Path

import pytest

from agent_forge.sandbox.base import SandboxConfig
from agent_forge.sandbox.bwrap import BwrapSandbox

pytestmark = pytest.mark.integration


@pytest.fixture
async def sandbox() -> BwrapSandbox:
    if platform.system() != "Linux" or shutil.which("bwrap") is None:
        pytest.skip("bubblewrap is not available")

    repo = Path(tempfile.mkdtemp())
    (repo / "hello.py").write_text("print('hello world')\n", encoding="utf-8")

    sb = BwrapSandbox()
    await sb.start(str(repo), SandboxConfig(backend="bwrap", timeout_seconds=60))
    yield sb
    await sb.stop()


class TestBwrapSandboxIntegration:
    @pytest.mark.asyncio
    async def test_read_and_write(self, sandbox: BwrapSandbox) -> None:
        content = await sandbox.read_file("/workspace/hello.py")
        assert "hello world" in content

        await sandbox.write_file("/workspace/new.txt", "ok\n")
        new_content = await sandbox.read_file("/workspace/new.txt")
        assert new_content == "ok\n"
