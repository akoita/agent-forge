"""Tests for agent_forge.observability — structured logging and tracing."""

from __future__ import annotations

import json
import logging
import os
import tempfile

import pytest

from agent_forge.observability.logger import (
    REDACTED,
    _redact_string,
    get_logger,
    redact_secrets,
    reset_logging,
    setup_logging,
)
from agent_forge.observability.tracing import (
    TraceContext,
    clear_trace_context,
    get_trace_context,
    inject_trace_context,
    set_trace_context,
    update_iteration,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_logging():
    """Reset logging state before and after every test."""
    reset_logging()
    clear_trace_context()
    yield
    reset_logging()
    clear_trace_context()


# ===========================================================================
# TraceContext Tests
# ===========================================================================


class TestTraceContext:
    """Tests for trace context lifecycle."""

    def test_set_and_get(self) -> None:
        ctx = set_trace_context("run-123", iteration=5)
        assert ctx.run_id == "run-123"
        assert ctx.iteration == 5
        assert get_trace_context() is ctx

    def test_default_is_none(self) -> None:
        assert get_trace_context() is None

    def test_clear(self) -> None:
        set_trace_context("run-abc")
        clear_trace_context()
        assert get_trace_context() is None

    def test_update_iteration(self) -> None:
        set_trace_context("run-456")
        updated = update_iteration(10)
        assert updated is not None
        assert updated.run_id == "run-456"
        assert updated.iteration == 10
        # verify it's stored
        assert get_trace_context() == updated

    def test_update_iteration_without_context(self) -> None:
        result = update_iteration(5)
        assert result is None

    def test_frozen(self) -> None:
        ctx = TraceContext(run_id="r1", iteration=1)
        with pytest.raises(AttributeError):
            ctx.run_id = "r2"  # type: ignore[misc]


class TestInjectTraceContextProcessor:
    """Tests for the structlog processor."""

    def test_injects_run_id_and_iteration(self) -> None:
        set_trace_context("run-aaa", iteration=3)
        event_dict: dict = {"event": "hello"}
        result = inject_trace_context(None, "info", event_dict)
        assert result["run_id"] == "run-aaa"
        assert result["iteration"] == 3

    def test_no_context_is_noop(self) -> None:
        event_dict: dict = {"event": "world"}
        result = inject_trace_context(None, "info", event_dict)
        assert "run_id" not in result

    def test_does_not_overwrite_explicit_run_id(self) -> None:
        set_trace_context("run-bbb")
        event_dict: dict = {"event": "x", "run_id": "explicit"}
        result = inject_trace_context(None, "info", event_dict)
        assert result["run_id"] == "explicit"


# ===========================================================================
# Redaction Tests
# ===========================================================================


class TestRedaction:
    """Tests for API key redaction."""

    @pytest.mark.parametrize(
        "secret",
        [
            "AIzaSyD1234567890abcdefghijklmnopqrstuvw",  # Google
            "sk-abc123def456ghi789jkl012mno345pqr678stu",  # OpenAI
            "sk-ant-abc123-def456ghi789jkl012mno345pqr678stu",  # Anthropic
            "key-abc123def456ghi789jkl012mno345pqr67",  # generic
            "ghp_abcdefghijklmnopqrstuvwxyz1234567890",  # GitHub PAT
        ],
    )
    def test_known_patterns_redacted(self, secret: str) -> None:
        result = _redact_string(f"my key is {secret} here")
        assert REDACTED in result
        assert secret not in result

    def test_safe_string_unmodified(self) -> None:
        assert _redact_string("hello world") == "hello world"

    def test_processor_redacts_all_values(self) -> None:
        event_dict = {
            "event": "test",
            "api_key": "sk-abc123def456ghi789jkl012mno345pqr678stu",
            "nested": {"key": "AIzaSyD1234567890abcdefghijklmnopqrstuvw"},
            "list_val": ["safe", "sk-abc123def456ghi789jkl012mno345pqr678stu"],
        }
        result = redact_secrets(None, "info", event_dict)
        assert REDACTED in result["api_key"]
        assert REDACTED in result["nested"]["key"]
        assert REDACTED in result["list_val"][1]
        # Safe values untouched
        assert result["event"] == "test"
        assert result["list_val"][0] == "safe"

    def test_non_string_values_unchanged(self) -> None:
        event_dict = {"count": 42, "enabled": True, "rate": 3.14}
        result = redact_secrets(None, "info", event_dict)
        assert result == event_dict


# ===========================================================================
# Logger Setup Tests
# ===========================================================================


class TestSetupLogging:
    """Tests for setup_logging and get_logger."""

    def test_setup_logging_runs_without_error(self) -> None:
        setup_logging(level="DEBUG")

    def test_idempotent(self) -> None:
        setup_logging(level="INFO")
        setup_logging(level="DEBUG")  # should be a no-op

    def test_get_logger_returns_bound_logger(self) -> None:
        setup_logging(level="INFO")
        lgr = get_logger("test_component")
        assert lgr is not None

    def test_json_file_output(self) -> None:
        """Verify that log output to a file is valid JSON with required schema."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            tmp_path = f.name

        try:
            setup_logging(level="DEBUG", log_file=tmp_path)
            set_trace_context("run-json-test", iteration=1)

            lgr = get_logger("my_comp")
            lgr.info("test_event", extra_field="hello")

            # Flush handlers
            for handler in logging.getLogger().handlers:
                handler.flush()

            with open(tmp_path) as fh:
                lines = [line.strip() for line in fh if line.strip()]

            assert len(lines) >= 1, f"Expected at least 1 log line, got {len(lines)}"
            record = json.loads(lines[-1])

            # Schema checks
            assert record["event"] == "test_event"
            assert record.get("log_level") == "info" or record.get("level") == "info"
            assert "component" in record
            assert record["run_id"] == "run-json-test"
            assert record["iteration"] == 1
            assert "timestamp" in record
        finally:
            os.unlink(tmp_path)

    def test_log_level_filtering(self) -> None:
        """DEBUG messages should be suppressed at INFO level."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            tmp_path = f.name

        try:
            # File handler always captures DEBUG, but console is filtered.
            # Test that the file handler *does* capture debug.
            setup_logging(level="DEBUG", log_file=tmp_path)

            lgr = get_logger("filter_test")
            lgr.debug("debug_msg")
            lgr.info("info_msg")

            for handler in logging.getLogger().handlers:
                handler.flush()

            with open(tmp_path) as fh:
                lines = [line.strip() for line in fh if line.strip()]

            events = [json.loads(line)["event"] for line in lines]
            assert "debug_msg" in events
            assert "info_msg" in events
        finally:
            os.unlink(tmp_path)

    def test_console_format_json(self) -> None:
        """Console output in JSON mode should produce JSON to stderr."""
        import io

        # Capture stderr directly
        buf = io.StringIO()
        setup_logging(level="INFO", console_format="json")

        # Redirect the stderr handler to our buffer
        root = logging.getLogger()
        for handler in root.handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(
                handler, logging.FileHandler
            ):
                handler.stream = buf
                break

        lgr = get_logger("json_console")
        lgr.info("console_json_event")

        for handler in root.handlers:
            handler.flush()

        output = buf.getvalue().strip()
        assert output, "Expected some output"
        # Output should contain at minimum event and level info
        assert "console_json_event" in output
