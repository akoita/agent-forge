"""Trace context — run_id propagation across components.

Provides a lightweight trace context using ``contextvars`` so that every log
line automatically includes ``run_id`` and ``iteration`` without explicit
passing through every call-site.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Trace Context
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TraceContext:
    """Immutable trace context bound to a single agent run."""

    run_id: str
    iteration: int | None = None


_trace_ctx_var: ContextVar[TraceContext | None] = ContextVar(
    "agent_forge_trace_ctx", default=None
)


def set_trace_context(
    run_id: str, *, iteration: int | None = None
) -> TraceContext:
    """Set the current trace context (thread/task-local).

    Returns the newly created ``TraceContext``.
    """
    ctx = TraceContext(run_id=run_id, iteration=iteration)
    _trace_ctx_var.set(ctx)
    return ctx


def get_trace_context() -> TraceContext | None:
    """Return the current trace context, or ``None`` if unset."""
    return _trace_ctx_var.get()


def clear_trace_context() -> None:
    """Remove the current trace context."""
    _trace_ctx_var.set(None)


def update_iteration(iteration: int) -> TraceContext | None:
    """Update only the iteration number on the current trace context.

    Returns the updated context or ``None`` if no context is set.
    """
    current = _trace_ctx_var.get()
    if current is None:
        return None
    ctx = TraceContext(run_id=current.run_id, iteration=iteration)
    _trace_ctx_var.set(ctx)
    return ctx


# ---------------------------------------------------------------------------
# structlog Processor
# ---------------------------------------------------------------------------


def inject_trace_context(
    _logger: Any, _method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """structlog processor that injects ``run_id`` and ``iteration``.

    If no trace context is active the event dict is returned unchanged.
    """
    ctx = _trace_ctx_var.get()
    if ctx is not None:
        event_dict.setdefault("run_id", ctx.run_id)
        if ctx.iteration is not None:
            event_dict.setdefault("iteration", ctx.iteration)
    return event_dict
