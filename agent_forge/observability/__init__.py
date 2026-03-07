"""Observability — structured logging, tracing, and cost tracking.

Public API
----------
.. autofunction:: setup_logging
.. autofunction:: get_logger
.. autoclass:: TraceContext
.. autofunction:: set_trace_context
.. autofunction:: get_trace_context
.. autofunction:: clear_trace_context
.. autofunction:: update_iteration
.. autoclass:: CostTracker
.. autoclass:: CostEntry
.. autofunction:: print_run_summary
.. autofunction:: save_summary
"""

from agent_forge.observability.cost import (
    CostEntry,
    CostTracker,
    print_run_summary,
    save_summary,
)
from agent_forge.observability.logger import get_logger, setup_logging
from agent_forge.observability.tracing import (
    TraceContext,
    clear_trace_context,
    get_trace_context,
    set_trace_context,
    update_iteration,
)

__all__ = [
    "clear_trace_context",
    "CostEntry",
    "CostTracker",
    "get_logger",
    "get_trace_context",
    "print_run_summary",
    "save_summary",
    "set_trace_context",
    "setup_logging",
    "TraceContext",
    "update_iteration",
]
