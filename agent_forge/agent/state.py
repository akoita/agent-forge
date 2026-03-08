"""Run state machine — valid state transitions for AgentRun lifecycle."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_forge.agent.models import RunState
from agent_forge.llm.errors import InvalidStateTransitionError

__all__ = ["InvalidStateTransitionError", "transition"]

if TYPE_CHECKING:
    from agent_forge.agent.models import AgentRun

# ── Valid transition map ──────────────────────────────────────────────
# PENDING  → RUNNING
# RUNNING  → COMPLETED | FAILED | TIMEOUT | CANCELLED

VALID_TRANSITIONS: dict[RunState, set[RunState]] = {
    RunState.PENDING: {RunState.RUNNING},
    RunState.RUNNING: {
        RunState.COMPLETED,
        RunState.FAILED,
        RunState.TIMEOUT,
        RunState.CANCELLED,
    },
    RunState.COMPLETED: set(),
    RunState.FAILED: set(),
    RunState.TIMEOUT: set(),
    RunState.CANCELLED: set(),
}


def transition(run: AgentRun, new_state: RunState) -> None:
    """Validate and apply a state transition on an AgentRun.

    Raises InvalidStateTransitionError if the transition is not allowed.
    """
    allowed = VALID_TRANSITIONS.get(run.state, set())
    if new_state not in allowed:
        msg = f"Invalid state transition: {run.state.value} → {new_state.value}"
        raise InvalidStateTransitionError(msg)
    run.state = new_state
