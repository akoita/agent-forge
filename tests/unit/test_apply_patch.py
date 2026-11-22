"""Unit tests for the atomic apply_patch tool and patch event emission."""

from __future__ import annotations

import hashlib
import json
import shlex
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from agent_forge.agent.core import react_loop
from agent_forge.agent.models import AgentConfig, AgentRun, RunState
from agent_forge.agent.persistence import load_run, save_run
from agent_forge.llm.base import LLMResponse, TokenUsage, ToolCall
from agent_forge.orchestration.events import Event, EventBus, EventType
from agent_forge.sandbox.base import ExecResult, Sandbox, SandboxConfig
from agent_forge.tools import ApplyPatchTool, ToolRegistry

if TYPE_CHECKING:
    from pathlib import Path


class _InMemorySandbox(Sandbox):
    """Minimal in-memory sandbox double for testing multi-file patch transactions."""

    def __init__(
        self,
        files: dict[str, str] | None = None,
        *,
        fail_write_path: str | None = None,
    ) -> None:
        self.files: dict[str, str] = dict(files or {})
        self.fail_write_path = fail_write_path
        self.writes: list[str] = []
        self.exec_commands: list[str] = []
        self._alive = True

    async def start(self, repo_path: str, config: SandboxConfig | None = None) -> None:
        del repo_path, config
        self._alive = True

    async def stop(self) -> None:
        self._alive = False

    async def is_alive(self) -> bool:
        return self._alive

    async def read_file(self, path: str) -> str:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    async def write_file(self, path: str, content: str) -> None:
        if self.fail_write_path == path:
            raise OSError(f"Simulated disk failure on {path}")
        self.writes.append(path)
        self.files[path] = content

    async def exec(self, command: str, *, timeout_seconds: int = 30) -> ExecResult:
        del timeout_seconds
        self.exec_commands.append(command)
        if command.startswith("rm -f "):
            target = shlex.split(command)[2]
            self.files.pop(target, None)
            return ExecResult(exit_code=0, stdout="", stderr="")
        return ExecResult(exit_code=1, stdout="", stderr=f"Unsupported command: {command}")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class TestApplyPatchAtomicTransactions:
    """Validate multi-file staging, preconditions, diff preview, and rollback."""

    @pytest.mark.asyncio
    async def test_multi_file_modify_create_delete_and_manifest(self) -> None:
        sandbox = _InMemorySandbox(
            {
                "/workspace/app.py": "def health():\n    return 500\n",
                "/workspace/legacy.py": "OLD = True\n",
            }
        )
        tool = ApplyPatchTool()
        result = await tool.execute(
            {
                "operations": [
                    {
                        "path": "app.py",
                        "action": "modify",
                        "expected_sha256": _sha256("def health():\n    return 500\n"),
                        "hunks": [{"old_text": "return 500", "new_text": "return 200"}],
                    },
                    {
                        "path": "new_mod.py",
                        "action": "create",
                        "content": "VALUE = 42\n",
                    },
                    {
                        "path": "legacy.py",
                        "action": "delete",
                    },
                ]
            },
            sandbox,
        )

        assert result.exit_code == 0
        assert result.error is None
        assert sandbox.files["/workspace/app.py"] == "def health():\n    return 200\n"
        assert sandbox.files["/workspace/new_mod.py"] == "VALUE = 42\n"
        assert "/workspace/legacy.py" not in sandbox.files

        manifest = result.metadata["changed_files"]
        assert [entry["path"] for entry in manifest] == ["app.py", "new_mod.py", "legacy.py"]
        assert [entry["status"] for entry in manifest] == ["applied", "applied", "applied"]
        assert manifest[0]["sha256_after"] == _sha256("def health():\n    return 200\n")
        assert manifest[2]["sha256_after"] is None
        assert "-    return 500" in result.metadata["diff_preview"]
        assert "+    return 200" in result.metadata["diff_preview"]
        assert "+VALUE = 42" in result.metadata["diff_preview"]

    @pytest.mark.asyncio
    async def test_precondition_mismatch_aborts_before_any_writes(self) -> None:
        sandbox = _InMemorySandbox(
            {"/workspace/a.py": "alpha = 1\n", "/workspace/b.py": "beta = 1\n"}
        )
        tool = ApplyPatchTool()
        result = await tool.execute(
            {
                "operations": [
                    {"path": "a.py", "hunks": [{"old_text": "alpha = 1", "new_text": "alpha = 2"}]},
                    {
                        "path": "b.py",
                        "expected_sha256": "0" * 64,
                        "hunks": [{"old_text": "beta = 1", "new_text": "beta = 2"}],
                    },
                ]
            },
            sandbox,
        )

        assert result.exit_code == 1
        err = json.loads(result.error or "{}")
        assert err["failure"]["code"] == "precondition_mismatch"
        assert err["failure"]["file"] == "b.py"
        assert err["failure"]["details"]["actual_sha256"] == _sha256("beta = 1\n")
        assert sandbox.writes == []
        assert sandbox.files["/workspace/a.py"] == "alpha = 1\n"

    @pytest.mark.asyncio
    async def test_second_file_hunk_failure_leaves_first_file_untouched(self) -> None:
        sandbox = _InMemorySandbox({"/workspace/a.py": "one = 1\n", "/workspace/b.py": "two = 2\n"})
        tool = ApplyPatchTool()
        result = await tool.execute(
            {
                "operations": [
                    {"path": "a.py", "hunks": [{"old_text": "one = 1", "new_text": "one = 10"}]},
                    {"path": "b.py", "hunks": [{"old_text": "missing", "new_text": "two = 20"}]},
                ]
            },
            sandbox,
        )

        assert result.exit_code == 1
        err = json.loads(result.error or "{}")
        assert err["failure"]["code"] == "hunk_not_found"
        assert err["failure"]["file"] == "b.py"
        assert err["failure"]["hunk_index"] == 0
        assert "two = 2" in err["failure"]["details"]["file_preview"]
        assert sandbox.writes == []
        assert sandbox.files["/workspace/a.py"] == "one = 1\n"

    @pytest.mark.asyncio
    async def test_commit_failure_rolls_back_modified_and_created_files(self) -> None:
        sandbox = _InMemorySandbox(
            {
                "/workspace/a.py": "original a\n",
                "/workspace/c.py": "original c\n",
            },
            fail_write_path="/workspace/c.py",
        )
        tool = ApplyPatchTool()
        result = await tool.execute(
            {
                "operations": [
                    {
                        "path": "a.py",
                        "hunks": [{"old_text": "original a", "new_text": "updated a"}],
                    },
                    {"path": "b_new.py", "action": "create", "content": "created\n"},
                    {
                        "path": "c.py",
                        "hunks": [{"old_text": "original c", "new_text": "updated c"}],
                    },
                ]
            },
            sandbox,
        )

        assert result.exit_code == 1
        err = json.loads(result.error or "{}")
        assert err["status"] == "rolled_back"
        assert err["failed_path"] == "c.py"
        assert err["rolled_back"] == ["b_new.py", "a.py"]
        assert sandbox.files["/workspace/a.py"] == "original a\n"
        assert "/workspace/b_new.py" not in sandbox.files
        assert sandbox.files["/workspace/c.py"] == "original c\n"

    @pytest.mark.asyncio
    async def test_idempotent_reapplication_for_modify_create_delete(self) -> None:
        sandbox = _InMemorySandbox({"/workspace/app.py": "x = 1\n", "/workspace/old.py": "dead\n"})
        tool = ApplyPatchTool()
        args = {
            "operations": [
                {"path": "app.py", "hunks": [{"old_text": "x = 1", "new_text": "x = 2"}]},
                {"path": "new.py", "action": "create", "content": "hello\n"},
                {"path": "old.py", "action": "delete"},
            ]
        }

        first = await tool.execute(args, sandbox)
        assert first.exit_code == 0
        writes_after_first = len(sandbox.writes)

        second = await tool.execute(args, sandbox)
        assert second.exit_code == 0
        assert len(sandbox.writes) == writes_after_first
        assert [c["status"] for c in second.metadata["changed_files"]] == [
            "unchanged",
            "unchanged",
            "unchanged",
        ]

    @pytest.mark.asyncio
    async def test_ambiguous_hunk_traversal_duplicate_and_malformed_errors(self) -> None:
        sandbox = _InMemorySandbox({"/workspace/dup.py": "pass\npass\n"})
        tool = ApplyPatchTool()

        ambiguous = await tool.execute(
            {"operations": [{"path": "dup.py", "hunks": [{"old_text": "pass", "new_text": "x"}]}]},
            sandbox,
        )
        assert ambiguous.exit_code == 1
        assert json.loads(ambiguous.error or "{}")["failure"]["code"] == "hunk_ambiguous"

        traversal = await tool.execute(
            {"operations": [{"path": "../etc/passwd", "action": "delete"}]},
            sandbox,
        )
        assert traversal.exit_code == 1
        assert json.loads(traversal.error or "{}")["failure"]["code"] == "invalid_path"

        duplicate = await tool.execute(
            {
                "operations": [
                    {"path": "dup.py", "hunks": [{"old_text": "pass\npass", "new_text": "x"}]},
                    {"path": "./dup.py", "action": "delete"},
                ]
            },
            sandbox,
        )
        assert duplicate.exit_code == 1
        assert json.loads(duplicate.error or "{}")["failure"]["code"] == "duplicate_path"

        malformed = await tool.execute({"operations": []}, sandbox)
        assert malformed.exit_code == 1
        assert json.loads(malformed.error or "{}")["failure"]["code"] == "invalid_operations"

    @pytest.mark.asyncio
    async def test_dry_run_previews_without_modifying_sandbox(self) -> None:
        sandbox = _InMemorySandbox({"/workspace/app.py": "status = 500\n"})
        tool = ApplyPatchTool()
        result = await tool.execute(
            {
                "dry_run": True,
                "operations": [
                    {
                        "path": "app.py",
                        "hunks": [{"old_text": "status = 500", "new_text": "status = 200"}],
                    }
                ],
            },
            sandbox,
        )

        assert result.exit_code == 0
        assert result.metadata["dry_run"] is True
        assert "-status = 500" in result.metadata["diff_preview"]
        assert "+status = 200" in result.metadata["diff_preview"]
        assert sandbox.writes == []
        assert sandbox.files["/workspace/app.py"] == "status = 500\n"


class TestApplyPatchLoopIntegration:
    """Verify PATCH_APPLIED event emission, retry recovery, and metadata persistence."""

    @pytest.mark.asyncio
    async def test_react_loop_emits_patch_applied_and_recovers_on_retry(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "agent_forge.agent.core.save_run", lambda run: save_run(run, base_dir=tmp_path)
        )
        monkeypatch.setattr(
            "agent_forge.observability.cost.save_summary",
            lambda *_args: tmp_path / "summary.json",
        )

        registry = ToolRegistry()
        registry.register(ApplyPatchTool())
        sandbox = _InMemorySandbox({"/workspace/app.py": "return 500\n"})
        bus = EventBus()
        patch_events: list[Event] = []

        async def _capture(event: Event) -> None:
            patch_events.append(event)

        await bus.subscribe(EventType.PATCH_APPLIED, _capture)

        llm = AsyncMock()
        llm.complete.side_effect = [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call-bad",
                        name="apply_patch",
                        arguments={
                            "operations": [
                                {
                                    "path": "app.py",
                                    "hunks": [{"old_text": "wrong text", "new_text": "return 200"}],
                                }
                            ]
                        },
                    )
                ],
                usage=TokenUsage(10, 10, 20),
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call-good",
                        name="apply_patch",
                        arguments={
                            "operations": [
                                {
                                    "path": "app.py",
                                    "hunks": [{"old_text": "return 500", "new_text": "return 200"}],
                                }
                            ]
                        },
                    )
                ],
                usage=TokenUsage(20, 10, 30),
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content="Patched app.py successfully.",
                tool_calls=[],
                usage=TokenUsage(10, 10, 20),
                finish_reason="stop",
            ),
        ]

        run = AgentRun(
            task="Fix app.py",
            repo_path=str(tmp_path),
            config=AgentConfig(max_iterations=5, max_tokens_per_run=5000),
        )
        completed = await react_loop(
            run,
            llm,
            registry,
            sandbox,  # type: ignore[arg-type]
            event_bus=bus,
        )

        assert completed.state == RunState.COMPLETED
        assert sandbox.files["/workspace/app.py"] == "return 200\n"
        assert len(patch_events) == 1
        assert patch_events[0].data["changed_files"][0]["path"] == "app.py"
        assert "-return 500" in patch_events[0].data["diff_preview"]
        assert "+return 200" in patch_events[0].data["diff_preview"]

        reloaded = load_run(completed.id, base_dir=tmp_path)
        inv_meta = reloaded.tool_invocations[1].result.metadata
        assert inv_meta["changed_files"][0]["status"] == "applied"
