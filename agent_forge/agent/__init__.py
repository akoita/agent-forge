"""Agent core — ReAct loop and agent lifecycle."""

from agent_forge.agent.context import (
    ContextAssembly,
    ContextEngine,
    ContextPolicy,
    WorkingSet,
)
from agent_forge.agent.contracts import (
    ClauseResult,
    CompletionContract,
    ContractClause,
    ContractContext,
    ContractEvaluation,
    JsonArtifactClause,
    artifact_report_contract,
    coding_fix_contract,
    qa_contract,
    resolve_completion_contract,
)
from agent_forge.agent.core import react_loop
from agent_forge.agent.instructions import discover_repo_instructions
from agent_forge.agent.journal import (
    JournalEntry,
    ReplayResult,
    RunJournal,
    compute_tool_idempotency_key,
    replay_journal,
)
from agent_forge.agent.models import AgentConfig, AgentRun, RunState, ToolInvocation
from agent_forge.agent.persistence import load_run, save_run
from agent_forge.agent.prompts import build_system_prompt
from agent_forge.agent.repo_map import build_repo_map
from agent_forge.agent.session import DevSession, SessionStore, SessionSummary, SessionTurn
from agent_forge.agent.state import InvalidStateTransitionError, transition

__all__ = [
    "AgentConfig",
    "AgentRun",
    "ClauseResult",
    "CompletionContract",
    "ContextAssembly",
    "ContextEngine",
    "ContextPolicy",
    "ContractClause",
    "ContractContext",
    "ContractEvaluation",
    "DevSession",
    "InvalidStateTransitionError",
    "JournalEntry",
    "JsonArtifactClause",
    "ReplayResult",
    "RunJournal",
    "RunState",
    "SessionStore",
    "SessionSummary",
    "SessionTurn",
    "ToolInvocation",
    "WorkingSet",
    "artifact_report_contract",
    "build_repo_map",
    "build_system_prompt",
    "coding_fix_contract",
    "compute_tool_idempotency_key",
    "discover_repo_instructions",
    "load_run",
    "qa_contract",
    "react_loop",
    "replay_journal",
    "resolve_completion_contract",
    "save_run",
    "transition",
]
