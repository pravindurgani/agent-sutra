"""Tests for tools/projects.py — project matching and context-awareness.

Covers:
- Trigger context-exclusion (_MENTION_CONTEXTS)
- Direct and command-position trigger matching
- Edge cases for mention vs. invocation
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


# ── Test fixtures ────────────────────────────────────────────────────

_FAKE_PROJECTS = [
    {
        "name": "iGaming Intelligence Dashboard",
        "path": "/tmp/igaming",
        "triggers": ["igaming intelligence dashboard", "igaming dashboard", "igaming"],
        "commands": {"run": "streamlit run app.py"},
    },
    {
        "name": "Affiliate Job Scraper",
        "path": "/tmp/scraper",
        "triggers": ["affiliate job scraper", "job scraper"],
        "commands": {"scrape": "python3 main.py"},
    },
    {
        "name": "SensiSpend V2",
        "path": "/tmp/sensispend",
        "triggers": ["sensispend"],
        "commands": {"dev": "npm run dev"},
    },
]


@pytest.fixture(autouse=True)
def _mock_projects():
    """Inject fake projects for all tests in this module."""
    with patch("tools.projects.get_projects", return_value=_FAKE_PROJECTS):
        yield


# ── Context-exclusion tests ──────────────────────────────────────────


class TestMatchProjectContextExclusion:
    """Triggers appearing after mention-context words should NOT match."""

    def test_does_not_match_in_description(self) -> None:
        """'about' prefix → mention, not invocation."""
        from tools.projects import match_project
        result = match_project("Design a card about iGaming Intelligence Dashboard")
        assert result is None

    def test_does_not_match_after_for(self) -> None:
        """'for' prefix → mention, not invocation."""
        from tools.projects import match_project
        result = match_project("Create a page for Affiliate Job Scraper")
        assert result is None

    def test_skips_featuring_context(self) -> None:
        """'featuring' prefix → mention, not invocation."""
        from tools.projects import match_project
        result = match_project("Build a dashboard featuring sensispend")
        assert result is None

    def test_skips_including_context(self) -> None:
        """'including' prefix → mention, not invocation."""
        from tools.projects import match_project
        result = match_project("Show a portfolio including iGaming Intelligence Dashboard")
        assert result is None


# ── Positive matching tests ──────────────────────────────────────────


class TestMatchProjectPositiveMatching:
    """Triggers in command position should still match correctly."""

    def test_still_matches_command_position(self) -> None:
        """'Run the affiliate job scraper' → trigger at command position → MATCH."""
        from tools.projects import match_project
        result = match_project("Run the affiliate job scraper")
        assert result is not None
        assert result["name"] == "Affiliate Job Scraper"

    def test_still_matches_direct_trigger(self) -> None:
        """Bare trigger at start of message → MATCH."""
        from tools.projects import match_project
        result = match_project("igaming intelligence dashboard")
        assert result is not None
        assert result["name"] == "iGaming Intelligence Dashboard"

    def test_still_matches_with_prefix(self) -> None:
        """'Launch sensispend' → non-mention prefix → MATCH."""
        from tools.projects import match_project
        result = match_project("Launch sensispend")
        assert result is not None
        assert result["name"] == "SensiSpend V2"


# ── v9.0.0 Phase 6: run_instructions in project context ──────────


class TestProjectContextRunInstructions:
    """get_project_context() includes run_instructions when present."""

    def test_project_context_includes_run_instructions(self) -> None:
        """Project with run_instructions → context contains 'Run Instructions:'."""
        from tools.projects import get_project_context
        project = {
            "name": "iGaming Intelligence Dashboard",
            "path": "/tmp/igaming",
            "commands": {"pipeline": "python3 run_pipeline.py"},
            "run_instructions": "IMPORTANT: run_pipeline.py REQUIRES --full-pipeline flag.\n",
        }
        context = get_project_context(project)
        assert "Run Instructions:" in context
        assert "--full-pipeline" in context

    def test_project_context_without_run_instructions(self) -> None:
        """Project without run_instructions → context does not contain it."""
        from tools.projects import get_project_context
        project = {
            "name": "Affiliate Job Scraper",
            "path": "/tmp/scraper",
            "commands": {"scrape": "python3 main.py"},
        }
        context = get_project_context(project)
        assert "Run Instructions:" not in context
