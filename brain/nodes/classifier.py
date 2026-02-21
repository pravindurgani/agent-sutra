from __future__ import annotations

import json
import logging

from brain.state import AgentState
from tools import claude_client
from tools.projects import match_project, get_all_projects_summary

logger = logging.getLogger(__name__)

# Order matters: more specific types first, generic "code" last.
# Tests import this to stay in sync — do not reorder without updating tests.
_FALLBACK_ORDER = ["project", "frontend", "ui_design", "automation", "data", "file", "code"]

SYSTEM = """You are a task classifier for an AI agent system. Given a user message (and optionally attached file info), classify the task into exactly one category.

Categories:
- "project": The task matches an existing registered project (see list below). Use this when the user wants to run, invoke, or interact with a known project.
- "code": Writing NEW code, building apps, scripts, websites, APIs, fixing bugs
- "data": Data analysis, processing CSVs/Excel, generating charts, summarizing data
- "file": File conversion, transformation, reformatting, merging, splitting
- "automation": Web scraping, scheduled reports, monitoring, repetitive workflows
- "ui_design": Visual design tasks — mockups, landing pages, dashboard designs, website layouts, UI/UX prototypes
- "frontend": Full-stack frontend engineering — production React apps, complex interactive dashboards, multi-component web applications, SPA builds

{projects_summary}

Respond with ONLY a JSON object: {{"task_type": "<category>", "reason": "<one sentence>"}}"""


def classify(state: AgentState) -> dict:
    """Classify the incoming task type, checking for project matches first."""
    message = state["message"]

    # Fast path: check for project trigger matches first
    matched_project = match_project(message)
    if matched_project:
        logger.info(
            "Classified task %s as project: %s",
            state["task_id"],
            matched_project["name"],
        )
        return {
            "task_type": "project",
            "project_name": matched_project["name"],
            "project_config": matched_project,
        }

    # Fall back to Claude classification for non-project tasks
    projects_summary = get_all_projects_summary()
    system = SYSTEM.format(projects_summary=projects_summary)

    prompt = f"User message: {message}"
    if state.get("files"):
        prompt += "\n\nAttached files:\n" + "\n".join(f"- {f}" for f in state["files"])

    response = claude_client.call(prompt, system=system, max_tokens=200)

    try:
        parsed = json.loads(response)
        task_type = parsed.get("task_type", "code")
    except json.JSONDecodeError:
        for t in _FALLBACK_ORDER:
            if t in response.lower():
                task_type = t
                break
        else:
            task_type = "code"

    result = {"task_type": task_type}

    # If Claude classified as project, find the matching project
    if task_type == "project" and not matched_project:
        matched_project = match_project(message)
        if matched_project:
            result["project_name"] = matched_project["name"]
            result["project_config"] = matched_project
        else:
            # No trigger match — fall back to "code" to avoid guaranteed failure loop
            logger.warning("Claude classified as project but no trigger match, falling back to code")
            result["task_type"] = "code"

    logger.info("Classified task %s as: %s", state["task_id"], result["task_type"])
    return result
