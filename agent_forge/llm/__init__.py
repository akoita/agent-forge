"""LLM client layer — provider adapters and data models."""

from agent_forge.llm.anthropic import AnthropicProvider
from agent_forge.llm.base import (
    LLMConfig,
    LLMProvider,
    LLMResponse,
    Message,
    Role,
    TokenUsage,
    ToolCall,
    ToolDefinition,
)
from agent_forge.llm.capabilities import (
    CapabilityRequirement,
    ProviderCapabilities,
    is_local_endpoint,
    query_provider_capabilities,
    resolve_capabilities,
)
from agent_forge.llm.errors import (
    AgentForgeError,
    InvalidStateTransitionError,
    LLMAuthError,
    LLMContextOverflowError,
    LLMError,
    LLMRateLimitError,
    LLMResponseError,
    LLMTimeoutError,
    SandboxError,
    SandboxStartupError,
    SandboxTimeoutError,
    ToolError,
    ToolExecutionError,
    ToolNotFoundError,
    ToolTimeoutError,
)
from agent_forge.llm.factory import create_provider, provider_kwargs_from_settings
from agent_forge.llm.gemini import GeminiProvider
from agent_forge.llm.openai import OpenAIProvider
from agent_forge.llm.openrouter import OpenRouterProvider
from agent_forge.llm.router import (
    ModelCandidate,
    RoutingDecision,
    RoutingLLMProvider,
    RoutingPolicy,
    TurnRoutingContext,
    select_route,
)

__all__ = [
    "AgentForgeError",
    "AnthropicProvider",
    "CapabilityRequirement",
    "GeminiProvider",
    "InvalidStateTransitionError",
    "LLMAuthError",
    "LLMConfig",
    "LLMContextOverflowError",
    "LLMError",
    "LLMProvider",
    "LLMRateLimitError",
    "LLMResponse",
    "LLMResponseError",
    "LLMTimeoutError",
    "Message",
    "ModelCandidate",
    "OpenAIProvider",
    "OpenRouterProvider",
    "ProviderCapabilities",
    "Role",
    "RoutingDecision",
    "RoutingLLMProvider",
    "RoutingPolicy",
    "SandboxError",
    "SandboxStartupError",
    "SandboxTimeoutError",
    "TokenUsage",
    "ToolCall",
    "ToolDefinition",
    "ToolError",
    "ToolExecutionError",
    "ToolNotFoundError",
    "ToolTimeoutError",
    "TurnRoutingContext",
    "create_provider",
    "is_local_endpoint",
    "provider_kwargs_from_settings",
    "query_provider_capabilities",
    "resolve_capabilities",
    "select_route",
]
