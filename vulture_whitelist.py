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

# --- Dataclass fields accessed dynamically ---
Task.priority
Task.created_at

# --- RedisQueue extended API (used by Worker._process_task) ---
from agent_forge.orchestration.redis_queue import RedisQueue

RedisQueue.complete
RedisQueue.fail
RedisQueue.size

# --- Event fields accessed dynamically ---
from agent_forge.orchestration.events import Event, EventType

Event.data
Event.timestamp

# --- EventType members used by Worker + react_loop ---
EventType.TOOL_CALLED
EventType.TOOL_COMPLETED
EventType.ITERATION_STARTED
EventType.ITERATION_COMPLETED

# --- CLI entry point ---
import agent_forge.cli  # noqa: E402

agent_forge.cli.main
