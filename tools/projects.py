from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import yaml

import config

logger = logging.getLogger(__name__)

_REGISTRY_PATH = config.BASE_DIR / "projects.yaml"
_projects: list[dict] = []


def load_projects() -> list[dict]:
    """Load project registry from projects.yaml."""
    global _projects
    if not _REGISTRY_PATH.exists():
        logger.warning("projects.yaml not found at %s", _REGISTRY_PATH)
        _projects = []
        return _projects

    with open(_REGISTRY_PATH) as f:
        data = yaml.safe_load(f)

    _projects = data.get("projects", []) if data else []
    logger.info("Loaded %d projects from registry", len(_projects))
    return _projects


def get_projects() -> list[dict]:
    """Return cached project list (loads if not loaded yet)."""
    if not _projects:
        load_projects()
    return _projects


def match_project(message: str) -> Optional[dict]:
    """Find a project matching the user's message via trigger keywords.

    Returns the matched project dict or None.
    """
    msg_lower = message.lower()
    projects = get_projects()

    best_match = None
    best_score = 0

    for project in projects:
        triggers = project.get("triggers", [])
        score = 0
        for trigger in triggers:
            if trigger.lower() in msg_lower:
                # Longer trigger matches are more specific = higher score
                score = max(score, len(trigger))

        if score > best_score:
            best_score = score
            best_match = project

    if best_match:
        logger.info("Matched project: %s (score=%d)", best_match["name"], best_score)
    return best_match


def get_project_context(project: dict) -> str:
    """Format a project's info as context for Claude prompts."""
    lines = [
        f"EXISTING PROJECT AVAILABLE: {project['name']}",
        f"Path: {project['path']}",
        f"Description: {project.get('description', 'N/A').strip()}",
    ]

    commands = project.get("commands", {})
    if commands:
        lines.append("Available commands:")
        for name, cmd in commands.items():
            lines.append(f"  - {name}: {cmd}")

    if project.get("requires_file"):
        lines.append("NOTE: This project requires a file upload to work.")

    lines.append(f"Timeout: {project.get('timeout', 60)}s")
    return "\n".join(lines)


def get_all_projects_summary() -> str:
    """Get a brief summary of all registered projects for Claude context."""
    projects = get_projects()
    if not projects:
        return "No existing projects registered."

    lines = ["REGISTERED PROJECTS (invoke these instead of writing new code):"]
    for p in projects:
        triggers = ", ".join(p.get("triggers", [])[:3])
        lines.append(f"  - {p['name']}: {p.get('description', '').strip().split(chr(10))[0]} [triggers: {triggers}]")
    return "\n".join(lines)
