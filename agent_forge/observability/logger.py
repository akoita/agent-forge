"""Structured logging setup — JSON + colored console output.

Configures ``structlog`` with:
- **Console (stderr):** colored, human-friendly renderer
- **File:** JSON lines with consistent schema
- **API key redaction:** strips known secret patterns before output
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog

from agent_forge.observability.tracing import inject_trace_context

# ---------------------------------------------------------------------------
# API Key Redaction
# ---------------------------------------------------------------------------

# Patterns that likely represent API keys / tokens.
_SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"AIza[0-9A-Za-z\-_]{35}"),          # Google / Gemini
    re.compile(r"sk-[A-Za-z0-9]{20,}"),              # OpenAI
    re.compile(r"sk-ant-[A-Za-z0-9\-]{20,}"),        # Anthropic
    re.compile(r"key-[A-Za-z0-9]{20,}"),             # Generic key-* tokens
    re.compile(r"ghp_[A-Za-z0-9]{36,}"),             # GitHub PAT
    re.compile(r"ghs_[A-Za-z0-9]{36,}"),             # GitHub App token
    re.compile(r"glpat-[A-Za-z0-9\-_]{20,}"),        # GitLab PAT
]

REDACTED = "***REDACTED***"


def _redact_string(value: str) -> str:
    """Replace any substring matching a known secret pattern."""
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub(REDACTED, value)
    return value


def _redact_value(value: Any) -> Any:
    """Recursively redact secrets in strings, lists, and dicts."""
    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_redact_value(item) for item in value)
    return value


def redact_secrets(
    _logger: Any, _method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """structlog processor that redacts API key patterns from all values."""
    return {key: _redact_value(val) for key, val in event_dict.items()}


# ---------------------------------------------------------------------------
# Logger Setup
# ---------------------------------------------------------------------------

_CONFIGURED = False


def setup_logging(
    *,
    level: str = "INFO",
    log_file: str = "",
    console_format: str = "text",
) -> None:
    """Configure structlog + stdlib logging for the entire application.

    Call once at startup (CLI entrypoint).  Subsequent calls are no-ops.

    Args:
        level: Log level name (DEBUG, INFO, WARNING, ERROR).
        log_file: Path to a JSON-lines log file.  Empty string ⇒ no file.
        console_format: ``"text"`` for colored console, ``"json"`` for JSON.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    numeric_level = getattr(logging, level.upper(), logging.INFO)

    # --- shared structlog processors (run before final rendering) ----------
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        inject_trace_context,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        redact_secrets,
    ]

    # --- stdlib root logger setup ------------------------------------------
    root = logging.getLogger()
    root.setLevel(numeric_level)

    # Console handler (stderr) — always present
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(numeric_level)
    root.addHandler(console_handler)

    # File handler (JSON lines) — optional
    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)  # capture everything to file

        # File always renders JSON
        file_formatter = structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(),
            ],
            foreign_pre_chain=shared_processors,
        )
        file_handler.setFormatter(file_formatter)
        root.addHandler(file_handler)

    # Console formatter
    console_renderer: Any
    if console_format == "json":
        console_renderer = structlog.processors.JSONRenderer()
    else:
        console_renderer = structlog.dev.ConsoleRenderer()

    console_formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            console_renderer,
        ],
        foreign_pre_chain=shared_processors,
    )
    console_handler.setFormatter(console_formatter)

    # --- structlog configuration -------------------------------------------
    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


# ---------------------------------------------------------------------------
# Public Logger Factory
# ---------------------------------------------------------------------------


def get_logger(component: str) -> Any:
    """Return a structlog logger pre-bound with ``component``.

    Usage::

        from agent_forge.observability import get_logger
        logger = get_logger("agent_core")
        logger.info("iteration_started", iteration=1, max_iterations=25)
    """
    return structlog.get_logger(component=component)


def reset_logging() -> None:
    """Reset logging configuration. Intended **only** for tests."""
    global _CONFIGURED
    _CONFIGURED = False
    root = logging.getLogger()
    root.handlers.clear()
    structlog.reset_defaults()
