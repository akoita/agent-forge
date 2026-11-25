"""Agent core — ReAct loop and agent lifecycle."""

from agent_forge.agent.chat import ChatSessionRuntime, SlashCommandResult, run_chat_repl
from agent_forge.agent.code_mode import (
    CodeModeLimits,
    HostToolBridge,
    build_code_mode_registry,
    search_tool_definitions,
)
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
from agent_forge.agent.diagnostics import (
    DiagnosticItem,
    DiagnosticsReport,
    run_post_edit_diagnostics,
)
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
from agent_forge.agent.subagent import SubagentLimits, SubagentResult, SubagentRunner
from agent_forge.agent.treesitter import extract_treesitter_file_symbols, is_treesitter_available

__all__ = [
    "AgentConfig",
    "AgentRun",
    "ChatSessionRuntime",
    "ClauseResult",
    "CodeModeLimits",
    "CompletionContract",
    "ContextAssembly",
    "ContextEngine",
    "ContextPolicy",
    "ContractClause",
    "ContractContext",
    "ContractEvaluation",
    "DevSession",
    "DiagnosticItem",
    "DiagnosticsReport",
    "HostToolBridge",
    "InvalidStateTransitionError",
    "JournalEntry",
    "JsonArtifactClause",
    "ReplayResult",
    "RunJournal",
    "RunState",
    "SessionStore",
    "SessionSummary",
    "SessionTurn",
    "SlashCommandResult",
    "SubagentLimits",
    "SubagentResult",
    "SubagentRunner",
    "ToolInvocation",
    "WorkingSet",
    "artifact_report_contract",
    "build_code_mode_registry",
    "build_repo_map",
    "build_system_prompt",
    "coding_fix_contract",
    "compute_tool_idempotency_key",
    "discover_repo_instructions",
    "extract_treesitter_file_symbols",
    "is_treesitter_available",
    "load_run",
    "qa_contract",
    "react_loop",
    "replay_journal",
    "resolve_completion_contract",
    "run_chat_repl",
    "run_post_edit_diagnostics",
    "save_run",
    "search_tool_definitions",
    "transition",
]
