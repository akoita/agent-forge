"""Core budget and cooperative cancellation policies for agent runs."""

from __future__ import annotations

from dataclasses import dataclass

VALID_BUDGET_ACTIONS = frozenset({"timeout", "pause"})


@dataclass(frozen=True)
class BudgetDecision:
    """Outcome of evaluating token, cost, and time budgets."""

    exceeded: bool
    dimension: str
    current: float
    limit: float
    action: str
    reason: str = ""


class BudgetPolicy:
    """Evaluate token, cost, and elapsed-time limits against an action policy."""

    def __init__(
        self,
        *,
        max_tokens: int,
        max_cost_usd: float | None = None,
        max_duration_seconds: float | None = None,
        on_exceed: str = "timeout",
    ) -> None:
        normalized = on_exceed.strip().lower()
        if normalized not in VALID_BUDGET_ACTIONS:
            allowed = ", ".join(sorted(VALID_BUDGET_ACTIONS))
            msg = f"Invalid budget action '{on_exceed}'. Expected one of: {allowed}"
            raise ValueError(msg)
        self.max_tokens = max_tokens
        self.max_cost_usd = max_cost_usd
        self.max_duration_seconds = max_duration_seconds
        self.on_exceed = normalized

    def evaluate(
        self,
        *,
        total_tokens: int,
        total_cost_usd: float,
        elapsed_seconds: float,
    ) -> BudgetDecision:
        """Check configured limits and return the first violated dimension if any."""
        if self.max_tokens > 0 and total_tokens > self.max_tokens:
            return BudgetDecision(
                exceeded=True,
                dimension="tokens",
                current=float(total_tokens),
                limit=float(self.max_tokens),
                action=self.on_exceed,
                reason="Token budget exceeded",
            )
        if (
            self.max_cost_usd is not None
            and self.max_cost_usd > 0
            and total_cost_usd > self.max_cost_usd
        ):
            return BudgetDecision(
                exceeded=True,
                dimension="cost",
                current=total_cost_usd,
                limit=self.max_cost_usd,
                action=self.on_exceed,
                reason=f"Cost budget exceeded (${total_cost_usd:.4f} > ${self.max_cost_usd:.4f})",
            )
        if (
            self.max_duration_seconds is not None
            and self.max_duration_seconds > 0
            and elapsed_seconds > self.max_duration_seconds
        ):
            return BudgetDecision(
                exceeded=True,
                dimension="time",
                current=elapsed_seconds,
                limit=self.max_duration_seconds,
                action=self.on_exceed,
                reason=(
                    f"Time budget exceeded ({elapsed_seconds:.1f}s > "
                    f"{self.max_duration_seconds:.1f}s)"
                ),
            )
        return BudgetDecision(
            exceeded=False,
            dimension="",
            current=0.0,
            limit=0.0,
            action="continue",
        )


class CancellationToken:
    """Cooperative cancellation and recoverable pause signal checked between loop steps."""

    def __init__(self) -> None:
        self._cancelled = False
        self._interrupted = False
        self._reason = ""

    def cancel(
        self,
        reason: str = "Cancelled by operator",
        *,
        recoverable: bool = False,
    ) -> None:
        """Mark the token for cancellation or recoverable interruption."""
        self._reason = reason
        if recoverable:
            self._interrupted = True
            self._cancelled = False
        else:
            self._cancelled = True
            self._interrupted = False

    def interrupt(self, reason: str = "Interrupted by operator") -> None:
        """Mark the token as a recoverable pause request."""
        self.cancel(reason, recoverable=True)

    @property
    def is_cancelled(self) -> bool:
        """Return whether terminal cancellation has been requested."""
        return self._cancelled

    @property
    def is_interrupted(self) -> bool:
        """Return whether a recoverable interruption has been requested."""
        return self._interrupted

    @property
    def is_stop_requested(self) -> bool:
        """Return whether either terminal cancellation or recoverable interruption was requested."""
        return self._cancelled or self._interrupted

    @property
    def reason(self) -> str:
        """Reason recorded when cancelled or interrupted."""
        return self._reason
