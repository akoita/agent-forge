"""Run state machine — valid state transitions for AgentRun lifecycle."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_forge.agent.models import RunState

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


class InvalidStateTransitionError(Exception):
    """Raised when an invalid state transition is attempted."""

    def __init__(self, current: RunState, target: RunState) -> None:
        self.current = current
        self.target = target
        super().__init__(
            f"Invalid state transition: {current.value} → {target.value}"
        )


def transition(run: AgentRun, new_state: RunState) -> None:
    """Validate and apply a state transition on an AgentRun.

    Raises InvalidStateTransitionError if the transition is not allowed.
    """
    allowed = VALID_TRANSITIONS.get(run.state, set())
    if new_state not in allowed:
        raise InvalidStateTransitionError(run.state, new_state)
    run.state = new_state
