from __future__ import annotations

from typing import TypedDict


class AgentState(TypedDict):
    # Input
    task_id: str
    user_id: int
    message: str
    files: list[str]  # paths to uploaded files

    # Classification
    task_type: str  # "code" | "data" | "file" | "automation" | "project" | "ui_design" | "frontend"

    # Project (populated when task_type == "project")
    project_name: str
    project_config: dict  # full project dict from projects.yaml

    # Planning
    plan: str

    # Execution
    code: str
    execution_result: str

    # Audit
    audit_verdict: str  # "pass" | "fail"
    audit_feedback: str

    # Control
    retry_count: int
    stage: str  # current pipeline stage for streaming status

    # Parameter extraction (populated by executor for project tasks)
    extracted_params: dict  # e.g. {"client": "Light & Wonder", "file": "/path/upload.xlsx"}

    # Working directory override (populated by executor)
    working_dir: str  # path for execution; empty string = use default

    # Conversation memory (injected by handler before pipeline runs)
    conversation_context: str  # recent history formatted for planner

    # Auto-install tracking (populated by executor via run_code_with_auto_install)
    auto_installed_packages: list[str]

    # Per-node timing for debug sidecar
    stage_timings: list[dict]

    # Output
    final_response: str
    artifacts: list[str]  # file paths to send back
