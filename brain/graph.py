from __future__ import annotations

import logging
import threading
import time as _time
from langgraph.graph import StateGraph, START, END

from brain.state import AgentState
from brain.nodes.classifier import classify
from brain.nodes.planner import plan
from brain.nodes.executor import execute
from brain.nodes.auditor import audit
from brain.nodes.deliverer import deliver
from storage.db import sync_update_task_state
import config

logger = logging.getLogger(__name__)

# Thread-safe stage tracking for streaming status to Telegram
_task_stages: dict[str, str] = {}
_stage_lock = threading.Lock()


def set_stage(task_id: str, stage: str):
    """Update the current stage for a task (thread-safe)."""
    with _stage_lock:
        # D-2: Prevent unbounded growth — prune if over 100 entries
        if len(_task_stages) > 100:
            _task_stages.clear()
        _task_stages[task_id] = stage


def get_stage(task_id: str) -> str:
    """Get the current stage for a task (thread-safe)."""
    with _stage_lock:
        return _task_stages.get(task_id, "")


def clear_stage(task_id: str):
    """Remove stage tracking for a completed task."""
    with _stage_lock:
        _task_stages.pop(task_id, None)


def _wrap_node(name: str, func):
    """Wrap a node function to update stage tracking, record timing, and persist state."""
    def wrapper(state: AgentState) -> dict:
        set_stage(state["task_id"], name)
        t0 = _time.time()
        result = func(state)
        elapsed_ms = int((_time.time() - t0) * 1000)
        timings = list(state.get("stage_timings", []))
        timings.append({"name": name, "duration_ms": elapsed_ms})
        result["stage_timings"] = timings
        # Persist partial state after each node (graceful — never crashes pipeline)
        merged = {**state, **result}
        sync_update_task_state(state["task_id"], merged, name)
        return result
    return wrapper


def should_retry(state: AgentState) -> str:
    """Decide whether to retry execution or deliver the result."""
    if state.get("audit_verdict") == "pass":
        return "deliver"
    if state.get("retry_count", 0) >= config.MAX_RETRIES:
        logger.warning("Max retries reached for task %s", state["task_id"])
        return "deliver"
    logger.info("Retrying task %s (attempt %d)", state["task_id"], state.get("retry_count", 0))
    return "plan"


def build_graph():
    """Build and compile the agent state graph.

    Flow: classify → plan → execute → audit → (retry or deliver)
    Each node is wrapped to update stage tracking for streaming status.
    """
    graph = StateGraph(AgentState)

    # Add nodes with stage tracking wrappers
    graph.add_node("classify", _wrap_node("classifying", classify))
    graph.add_node("plan", _wrap_node("planning", plan))
    graph.add_node("execute", _wrap_node("executing", execute))
    graph.add_node("audit", _wrap_node("auditing", audit))
    graph.add_node("deliver", _wrap_node("delivering", deliver))

    # Wire edges
    graph.add_edge(START, "classify")
    graph.add_edge("classify", "plan")
    graph.add_edge("plan", "execute")
    graph.add_edge("execute", "audit")
    graph.add_conditional_edges("audit", should_retry, {"plan": "plan", "deliver": "deliver"})
    graph.add_edge("deliver", END)

    return graph.compile()


# Singleton compiled graph
agent_graph = build_graph()


def run_task(
    task_id: str,
    user_id: int,
    message: str,
    files: list[str] | None = None,
    conversation_context: str = "",
) -> AgentState:
    """Execute the full agent pipeline for a task. Returns final state."""
    initial_state: AgentState = {
        "task_id": task_id,
        "user_id": user_id,
        "message": message,
        "files": files or [],
        "task_type": "",
        "project_name": "",
        "project_config": {},
        "plan": "",
        "code": "",
        "execution_result": "",
        "audit_verdict": "",
        "audit_feedback": "",
        "retry_count": 0,
        "stage": "",
        "final_response": "",
        "artifacts": [],
        "extracted_params": {},
        "working_dir": "",
        "conversation_context": conversation_context,
        "auto_installed_packages": [],
        "stage_timings": [],
        "server_url": "",
        "deploy_url": "",
        "was_refused": False,
    }

    logger.info("Starting agent pipeline for task %s", task_id)
    try:
        result = agent_graph.invoke(initial_state)
        timings = result.get("stage_timings", [])
        timing_str = " ".join(f"{t['name']}:{t['duration_ms']}ms" for t in timings)
        total_ms = sum(t["duration_ms"] for t in timings) if timings else 0
        logger.info(
            "Task %s completed in %.1fs [%s] verdict=%s type=%s",
            task_id, total_ms / 1000, timing_str,
            result.get("audit_verdict", "?"), result.get("task_type", "?"),
        )
        return result
    finally:
        clear_stage(task_id)
