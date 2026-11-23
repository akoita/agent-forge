"""Agent core — ReAct loop and agent lifecycle."""

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
from agent_forge.agent.state import InvalidStateTransitionError, transition

__all__ = [
    "AgentConfig",
    "AgentRun",
    "ClauseResult",
    "CompletionContract",
    "ContractClause",
    "ContractContext",
    "ContractEvaluation",
    "InvalidStateTransitionError",
    "JournalEntry",
    "JsonArtifactClause",
    "ReplayResult",
    "RunJournal",
    "RunState",
    "ToolInvocation",
    "artifact_report_contract",
    "build_system_prompt",
    "coding_fix_contract",
    "compute_tool_idempotency_key",
    "load_run",
    "qa_contract",
    "react_loop",
    "replay_journal",
    "resolve_completion_contract",
    "save_run",
    "transition",
]
