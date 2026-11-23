"""Capability- and cost-aware task/turn routing across configured LLM candidates."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

from agent_forge.llm.base import LLMConfig, LLMProvider, LLMResponse, Message, ToolDefinition
from agent_forge.llm.capabilities import (
    CapabilityRequirement,
    ProviderCapabilities,
    resolve_capabilities,
)
from agent_forge.llm.factory import create_provider
from agent_forge.llm.qualification import (
    resolve_profile_capabilities,
    resolve_profile_credentials,
)
from agent_forge.observability.cost import COST_TABLE

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping, Sequence

    from agent_forge.config import ForgeConfig, ModelProfileSettings, RoutingSettings

RoutingStrategy = Literal["cost_first", "balanced", "capability_first"]


@dataclass(frozen=True)
class ModelCandidate:
    """Selectable model profile with declared capabilities and token pricing."""

    profile_name: str
    provider_name: str
    model: str
    capabilities: ProviderCapabilities
    input_cost_per_token: float = 0.0
    output_cost_per_token: float = 0.0
    base_url: str | None = None
    api_mode: str = "auto"
    capability_overrides: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.capability_overrides is None:
            object.__setattr__(self, "capability_overrides", {})

    @property
    def blended_cost_per_token(self) -> float:
        """Return weighted token cost reflecting typical input-heavy agent turns."""
        return 0.75 * self.input_cost_per_token + 0.25 * self.output_cost_per_token


@dataclass(frozen=True)
class RoutingPolicy:
    """Policy controlling task- and turn-level route selection."""

    enabled: bool = False
    candidates: tuple[str, ...] = ()
    strategy: RoutingStrategy = "balanced"
    downgrade_budget_fraction: float = 0.75
    escalate_on_failures: int = 2

    @classmethod
    def from_settings(cls, settings: RoutingSettings) -> RoutingPolicy:
        """Build a :class:`RoutingPolicy` from Pydantic :class:`RoutingSettings`."""
        return cls(
            enabled=settings.enabled,
            candidates=tuple(settings.candidates),
            strategy=settings.strategy,
            downgrade_budget_fraction=settings.downgrade_budget_fraction,
            escalate_on_failures=settings.escalate_on_failures,
        )


@dataclass(frozen=True)
class TurnRoutingContext:
    """Observable execution signals used to select a model for the upcoming turn."""

    task: str = ""
    iteration: int = 1
    estimated_prompt_tokens: int = 0
    consecutive_failures: int = 0
    is_remediation: bool = False
    needs_structured_output: bool = False
    needs_parallel_tools: bool = False
    spent_cost_usd: float = 0.0
    max_cost_usd: float | None = None
    spent_tokens: int = 0
    max_tokens_per_run: int | None = None


@dataclass(frozen=True)
class RoutingDecision:
    """Recorded routing choice and structured rationale for one turn."""

    candidate: ModelCandidate
    reason: str
    required_capabilities: tuple[str, ...]
    preferred_capabilities: tuple[str, ...]
    budget_fraction_used: float

    def to_dict(self) -> dict[str, Any]:
        """Serialize this decision into a journal-safe dictionary."""
        return {
            "profile": self.candidate.profile_name,
            "provider": self.candidate.provider_name,
            "model": self.candidate.model,
            "reason": self.reason,
            "required_capabilities": list(self.required_capabilities),
            "preferred_capabilities": list(self.preferred_capabilities),
            "budget_fraction_used": round(self.budget_fraction_used, 4),
        }


def _compute_budget_fraction_used(context: TurnRoutingContext) -> float:
    fractions: list[float] = []
    if context.max_cost_usd is not None and context.max_cost_usd > 0:
        fractions.append(context.spent_cost_usd / context.max_cost_usd)
    if context.max_tokens_per_run is not None and context.max_tokens_per_run > 0:
        fractions.append(context.spent_tokens / context.max_tokens_per_run)
    return max(fractions) if fractions else 0.0


def _derive_turn_requirements(
    context: TurnRoutingContext,
    policy: RoutingPolicy,
) -> tuple[CapabilityRequirement, str]:
    required: set[str] = set()
    preferred: set[str] = set()

    if context.needs_structured_output:
        required.add("structured_output")
    if context.needs_parallel_tools:
        preferred.add("parallel_tool_calls")
    if context.estimated_prompt_tokens >= 2_000:
        preferred.add("prompt_caching")

    budget_fraction = _compute_budget_fraction_used(context)
    budget_pressured = budget_fraction >= policy.downgrade_budget_fraction
    escalating = (
        context.consecutive_failures >= policy.escalate_on_failures or context.is_remediation
    )

    if budget_pressured:
        return (
            CapabilityRequirement(
                required=frozenset(required),
                preferred=frozenset(preferred),
                min_context_tokens=context.estimated_prompt_tokens,
            ),
            "budget_downgrade",
        )
    if escalating:
        required.add("reasoning_continuity")
        preferred.update({"context_editing", "prompt_caching", "stateful_response_ids"})
        return (
            CapabilityRequirement(
                required=frozenset(required),
                preferred=frozenset(preferred),
                min_context_tokens=context.estimated_prompt_tokens,
            ),
            "failure_escalation",
        )
    return (
        CapabilityRequirement(
            required=frozenset(required),
            preferred=frozenset(preferred),
            min_context_tokens=context.estimated_prompt_tokens,
        ),
        policy.strategy,
    )


def _matches_requirement(
    candidate: ModelCandidate,
    req: CapabilityRequirement,
) -> bool:
    caps = candidate.capabilities
    if req.min_context_tokens > 0 and caps.max_context_tokens < req.min_context_tokens:
        return False
    return all(caps.supports(flag) for flag in req.required)


def _rank_key(
    candidate: ModelCandidate,
    req: CapabilityRequirement,
    mode: str,
) -> tuple[object, ...]:
    caps = candidate.capabilities
    missing_pref = sum(1 for flag in req.preferred if not caps.supports(flag))
    enabled_total = len(caps.enabled_flags())
    cost = candidate.blended_cost_per_token

    if mode in ("cost_first", "budget_downgrade"):
        return (cost, missing_pref, -enabled_total, candidate.profile_name)
    if mode in ("capability_first", "failure_escalation"):
        return (missing_pref, -enabled_total, cost, candidate.profile_name)
    return (missing_pref, cost, -enabled_total, candidate.profile_name)


def select_route(
    candidates: Sequence[ModelCandidate],
    context: TurnRoutingContext,
    policy: RoutingPolicy,
) -> RoutingDecision:
    """Select the best candidate strictly from declared capabilities and blended cost."""
    if not candidates:
        msg = "At least one ModelCandidate is required for routing."
        raise ValueError(msg)

    req, mode = _derive_turn_requirements(context, policy)
    budget_fraction = _compute_budget_fraction_used(context)
    eligible = [c for c in candidates if _matches_requirement(c, req)]
    pool = eligible if eligible else list(candidates)
    best = min(pool, key=lambda c: _rank_key(c, req, mode))
    reason = mode if eligible else f"{mode}_fallback"
    return RoutingDecision(
        candidate=best,
        reason=reason,
        required_capabilities=tuple(sorted(req.required)),
        preferred_capabilities=tuple(sorted(req.preferred)),
        budget_fraction_used=budget_fraction,
    )


def _lookup_model_pricing(model: str) -> tuple[float, float]:
    pricing = COST_TABLE.get(model) or COST_TABLE.get(model.lower())
    if pricing is None:
        return 0.0, 0.0
    return (
        float(pricing.get("input_cost_per_token", 0.0)),
        float(pricing.get("output_cost_per_token", 0.0)),
    )


def _build_candidate(
    config: ForgeConfig,
    profile_name: str,
    profile: ModelProfileSettings,
) -> ModelCandidate:
    provider_cfg = config.providers.get(profile.provider)
    raw_base_url = profile.base_url or (provider_cfg.base_url if provider_cfg is not None else "")
    base_url = raw_base_url or None
    api_mode = profile.api_mode
    if api_mode == "auto" and provider_cfg is not None and provider_cfg.api_mode != "auto":
        api_mode = provider_cfg.api_mode
    merged_caps = resolve_profile_capabilities(config, profile)
    caps = resolve_capabilities(
        profile.provider,
        profile.model,
        api_mode=api_mode,
        base_url=base_url,
        overrides=merged_caps,
    )
    in_cost, out_cost = _lookup_model_pricing(profile.model)
    return ModelCandidate(
        profile_name=profile_name,
        provider_name=profile.provider,
        model=profile.model,
        capabilities=caps,
        input_cost_per_token=in_cost,
        output_cost_per_token=out_cost,
        base_url=base_url,
        api_mode=api_mode,
        capability_overrides=merged_caps,
    )


def _instantiate_candidate_provider(
    cand: ModelCandidate,
    api_key: str,
    shared_clients: dict[tuple[Any, ...], LLMProvider],
    provider_factory: Callable[..., LLMProvider],
) -> LLMProvider:
    caps_key = tuple(sorted((cand.capability_overrides or {}).items()))
    client_key = (cand.provider_name, api_key, cand.base_url, cand.api_mode, caps_key)
    if client_key not in shared_clients:
        kwargs: dict[str, Any] = {}
        if cand.base_url:
            kwargs["base_url"] = cand.base_url
        if cand.provider_name in ("openai", "openai-compatible"):
            kwargs["api_mode"] = cand.api_mode
            if cand.capability_overrides:
                kwargs["capability_overrides"] = dict(cand.capability_overrides)
        shared_clients[client_key] = provider_factory(cand.provider_name, api_key, **kwargs)
    return shared_clients[client_key]


class RoutingLLMProvider(LLMProvider):
    """LLMProvider wrapper delegating each turn to the chosen :class:`ModelCandidate`."""

    def __init__(
        self,
        candidates: Sequence[ModelCandidate],
        providers: Mapping[str, LLMProvider],
        *,
        policy: RoutingPolicy | None = None,
    ) -> None:
        if not candidates:
            msg = "RoutingLLMProvider requires at least one candidate."
            raise ValueError(msg)
        self._candidates = tuple(candidates)
        self._providers = dict(providers)
        self.policy = policy or RoutingPolicy(enabled=True)
        self._active_candidate = self._candidates[0]
        self._last_route_profile: str | None = None
        self.decisions: list[RoutingDecision] = []

    @classmethod
    def from_config(
        cls,
        config: ForgeConfig,
        *,
        provider_factory: Callable[..., LLMProvider] = create_provider,
    ) -> RoutingLLMProvider:
        """Construct a :class:`RoutingLLMProvider` from ``config.routing``."""
        explicit_names = list(config.routing.candidates)
        profile_names = explicit_names or list(config.model_profiles)

        candidates: list[ModelCandidate] = []
        providers: dict[str, LLMProvider] = {}
        shared_clients: dict[tuple[Any, ...], LLMProvider] = {}

        for name in profile_names:
            profile = config.model_profiles.get(name)
            if profile is None:
                if explicit_names:
                    msg = f"Unknown routing candidate model profile '{name}'."
                    raise ValueError(msg)
                continue
            key_env, api_key, key_required = resolve_profile_credentials(config, profile)
            if key_required and not api_key:
                if explicit_names and provider_factory is create_provider:
                    msg = f"Missing credential variable {key_env} for routing candidate '{name}'."
                    raise ValueError(msg)
                if not explicit_names:
                    continue
                api_key = "test-key"

            cand = _build_candidate(config, name, profile)
            providers[name] = _instantiate_candidate_provider(
                cand, api_key, shared_clients, provider_factory
            )
            candidates.append(cand)

        if not candidates:
            msg = "No credential-ready model profiles available for routing."
            raise ValueError(msg)

        return cls(
            candidates,
            providers,
            policy=RoutingPolicy.from_settings(config.routing),
        )

    @property
    def last_decision(self) -> RoutingDecision | None:
        """Return the most recent routing decision made by this router."""
        return self.decisions[-1] if self.decisions else None

    def capabilities(self, model: str | None = None) -> ProviderCapabilities:
        """Return capabilities of the matching candidate or active candidate."""
        if model:
            for cand in self._candidates:
                if model in (cand.model, cand.profile_name):
                    return cand.capabilities
        return self._active_candidate.capabilities

    def select_for_turn(self, context: TurnRoutingContext) -> RoutingDecision:
        """Choose and activate the optimal candidate for *context*."""
        decision = select_route(self._candidates, context, self.policy)
        self._active_candidate = decision.candidate
        self.decisions.append(decision)
        return decision

    def _prepare_turn_call(
        self,
        config: LLMConfig | None,
    ) -> tuple[ModelCandidate, LLMProvider, LLMConfig]:
        cand = self._active_candidate
        provider = self._providers[cand.profile_name]
        base_cfg = replace(config) if config is not None else LLMConfig()
        base_cfg.model = cand.model
        if self._last_route_profile is not None and self._last_route_profile != cand.profile_name:
            base_cfg.previous_response_id = None
        return cand, provider, base_cfg

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: LLMConfig | None = None,
    ) -> LLMResponse:
        """Execute a completion against the currently routed provider/model."""
        cand, provider, turn_cfg = self._prepare_turn_call(config)
        self._last_route_profile = cand.profile_name
        return await provider.complete(messages, tools=tools, config=turn_cfg)

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: LLMConfig | None = None,
    ) -> AsyncIterator[LLMResponse]:
        """Stream a completion from the currently routed provider/model."""
        cand, provider, turn_cfg = self._prepare_turn_call(config)
        self._last_route_profile = cand.profile_name
        async for chunk in provider.stream(messages, tools=tools, config=turn_cfg):
            yield chunk

    async def close(self) -> None:
        """Close all unique underlying provider HTTP clients."""
        seen: set[int] = set()
        for provider in self._providers.values():
            ident = id(provider)
            if ident in seen:
                continue
            seen.add(ident)
            await provider.close()
