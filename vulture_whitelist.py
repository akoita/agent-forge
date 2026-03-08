"""Vulture whitelist — intentionally 'unused' code.

These items are flagged by vulture but are used at runtime
(ABC methods, dunder protocols, exported symbols, lazy imports).
"""

# --- ABC abstract methods (overridden by subclasses) ---
from agent_forge.orchestration.queue import TaskQueue

TaskQueue.enqueue
TaskQueue.dequeue
TaskQueue.get_status
TaskQueue.cancel

# --- Dunder protocols ---
from agent_forge.orchestration.queue import Task

Task.__lt__

# --- Lazy module-level __getattr__ ---
import agent_forge.orchestration  # noqa: E402

agent_forge.orchestration.__getattr__

# --- Dataclass fields used by external callers ---
Task.task_description
Task.repo_path
Task.config
Task.priority
Task.created_at

# --- RedisQueue extended API (used by workers, not yet wired) ---
from agent_forge.orchestration.redis_queue import RedisQueue

RedisQueue.complete
RedisQueue.fail
RedisQueue.size
RedisQueue.close

# --- Event/EventBus (used by react_loop, not yet wired in CLI — see #80) ---
from agent_forge.orchestration.events import Event, EventBus, EventType

Event.event_type
Event.data
Event.timestamp
EventBus.subscribe
EventBus.emit
EventType.TASK_STARTED
EventType.TASK_COMPLETED
EventType.TASK_FAILED
EventType.TOOL_CALLED
EventType.TOOL_RESULT
EventType.ITERATION_START
EventType.ITERATION_END

# --- Worker (not yet wired in CLI — see #80) ---
from agent_forge.orchestration.worker import Worker

Worker.start
Worker.stop

# --- CLI entry point ---
import agent_forge.cli  # noqa: E402

agent_forge.cli.main
