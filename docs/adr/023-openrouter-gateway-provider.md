# ADR-023: OpenRouter Multi-Provider Gateway Adapter

- **Status:** Accepted
- **Date:** 2026-11-24
- **Issue:** #145 (`[FEATURE] Add OpenRouter as an optional multi-provider gateway`)
- **Milestone:** 10. Daily-Driver ADE UX & v0.7.0 Release

## Context

Agent Forge provides native adapters for Gemini, OpenAI, and Anthropic alongside a generic `openai-compatible` adapter. Users frequently want access to a broader multi-vendor model catalog through a single API key and managed fallback routing, while retaining upstream provider observability, reported USD billing, privacy/ZDR guarantees, and multi-turn `reasoning_details` continuity that a plain OpenAI base URL override does not preserve.

## Decision

1. **`OpenRouterProvider` (`agent_forge/llm/openrouter.py`):**
   - Subclasses `OpenAIProvider`, defaulting to `https://openrouter.ai/api/v1` and `chat_completions`.
   - Emits optional `HTTP-Referer` and `X-Title` attribution headers, requests `usage: {include: true}` (and `stream_options: {include_usage: true}` when streaming), and attaches optional fallback `models` plus `provider` preferences (`allow_fallbacks`, `require_parameters`, `data_collection`, `zdr`, `order`, `only`, `ignore`, `sort`).
   - Preserves OpenRouter `reasoning_details` round-trips across assistant tool-call turns and captures `reported_model`, `upstream_provider`, `native_finish_reason`, and gateway-reported `cost` (`TokenUsage.reported_cost_usd`).
2. **Centralized Factory & Config (`factory.py`, `config.py`, `agent-forge.toml`):**
   - Register `"openrouter": OpenRouterProvider` and consolidate adapter kwarg extraction into `provider_kwargs_from_settings()` used consistently by CLI, runner, qualification, benchmark, and router paths.
   - Configure `[providers.openrouter]` with `OPENROUTER_API_KEY` and `AGENT_FORGE_PROVIDERS_OPENROUTER_*` overrides.
3. **Observability & Cost Tracking (`observability/cost.py`, `agent/core.py`):**
   - Prefer provider-reported `TokenUsage.reported_cost_usd` when present; otherwise normalize OpenRouter-prefixed and tagged model IDs (`openrouter/<vendor>/<model>:tag`, `google/*` -> `gemini/*`) so `lookup_model_pricing()` resolves non-zero rates when known.
   - Record `reported_model`, `upstream_provider`, `native_finish_reason`, and `reported_cost_usd` in `CostTracker`, `RunJournal`, `EventType.TOKEN_USAGE`, and `summary.json`.

## Consequences

- `--provider openrouter` works seamlessly across direct CLI, queued workers, ACP, chat sessions, model qualification, and routing without replacing direct native adapters.
- OpenRouter privacy policies (`zdr`, `data_collection="deny"`, `require_parameters`) are first-class, testable config settings.
