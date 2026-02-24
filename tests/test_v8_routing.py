"""Tests for AgentSutra v8 Phase 3: Intelligence & Routing.

Covers:
- Model router selection logic (all routing rules)
- Budget-based escalation
- Temporal sequence mining (_suggest_next_step)
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

import pytest

import config


# ── Helpers ──────────────────────────────────────────────────────────

@pytest.fixture()
def tasks_db(tmp_path):
    """Create a temp DB with the tasks table for temporal inference tests."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            task_type TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            plan TEXT DEFAULT '',
            result TEXT DEFAULT '',
            error TEXT DEFAULT '',
            token_usage TEXT DEFAULT '{}',
            created_at TEXT NOT NULL,
            completed_at TEXT DEFAULT ''
        )
    """)
    conn.commit()
    conn.close()
    with patch.object(config, "DB_PATH", db_path):
        yield db_path


# ── Model router selection tests ─────────────────────────────────────


class TestModelRouterSelection:
    """Verify _select_model routes correctly based on purpose/complexity/resources."""

    def test_audit_always_opus(self):
        """Rule (a): audit → ALWAYS Claude Opus regardless of anything else."""
        from tools.model_router import _select_model

        with patch("tools.model_router._ollama_available", return_value=True), \
             patch("tools.model_router._ram_below_threshold", return_value=True), \
             patch("tools.model_router._daily_spend_exceeds_threshold", return_value=True):
            provider, model = _select_model("audit", "low")

        assert provider == "claude"
        assert model == config.COMPLEX_MODEL

    def test_code_gen_always_sonnet(self):
        """Rule (b): code_gen → ALWAYS Claude Sonnet."""
        from tools.model_router import _select_model

        provider, model = _select_model("code_gen", "low")
        assert provider == "claude"
        assert model == config.DEFAULT_MODEL

    def test_classify_low_routes_to_ollama(self):
        """Rule (c): classify + low + Ollama available + RAM < 75% → Ollama."""
        from tools.model_router import _select_model

        with patch("tools.model_router._ollama_available", return_value=True), \
             patch("tools.model_router._ram_below_threshold", return_value=True), \
             patch("tools.model_router._daily_spend_exceeds_threshold", return_value=False):
            provider, model = _select_model("classify", "low")

        assert provider == "ollama"
        assert model == config.OLLAMA_DEFAULT_MODEL

    def test_classify_low_no_ollama_falls_back(self):
        """Rule (c): Ollama not available → falls back to Claude Sonnet."""
        from tools.model_router import _select_model

        with patch("tools.model_router._ollama_available", return_value=False), \
             patch("tools.model_router._daily_spend_exceeds_threshold", return_value=False):
            provider, model = _select_model("classify", "low")

        assert provider == "claude"
        assert model == config.DEFAULT_MODEL

    def test_classify_low_high_ram_falls_back(self):
        """Rule (c): RAM at 80% (above 75% threshold) → falls back to Claude."""
        from tools.model_router import _select_model

        with patch("tools.model_router._ollama_available", return_value=True), \
             patch("tools.model_router._ram_below_threshold", return_value=False), \
             patch("tools.model_router._daily_spend_exceeds_threshold", return_value=False):
            provider, model = _select_model("classify", "low")

        assert provider == "claude"
        assert model == config.DEFAULT_MODEL

    def test_plan_high_complexity_uses_sonnet(self):
        """Rule (e): plan + high complexity → Claude Sonnet."""
        from tools.model_router import _select_model

        with patch("tools.model_router._daily_spend_exceeds_threshold", return_value=False):
            provider, model = _select_model("plan", "high")

        assert provider == "claude"
        assert model == config.DEFAULT_MODEL

    def test_budget_escalation_routes_to_ollama(self):
        """Rule (d): daily spend > 70% of budget → classify routes to Ollama."""
        from tools.model_router import _select_model

        with patch("tools.model_router._daily_spend_exceeds_threshold", return_value=True), \
             patch("tools.model_router._ollama_available", return_value=True):
            provider, model = _select_model("classify", "high")

        assert provider == "ollama"
        assert model == config.OLLAMA_DEFAULT_MODEL

    def test_budget_escalation_no_budget_set(self):
        """Rule (d): DAILY_BUDGET_USD=0 (unlimited) → never escalates."""
        from tools.model_router import _daily_spend_exceeds_threshold

        with patch.object(config, "DAILY_BUDGET_USD", 0):
            assert _daily_spend_exceeds_threshold(0.7) is False


# ── Route-and-call integration tests ─────────────────────────────────


class TestRouteAndCall:
    """Verify route_and_call dispatches correctly and handles Ollama failures."""

    def test_ollama_failure_falls_back_to_claude(self):
        """If Ollama call fails, should fall back to Claude transparently."""
        from tools.model_router import route_and_call

        with patch("tools.model_router._select_model", return_value=("ollama", "llama3.1:8b")), \
             patch("tools.model_router._call_ollama", side_effect=Exception("connection refused")), \
             patch("tools.claude_client.call", return_value="claude response") as mock_claude:
            result = route_and_call("hello", purpose="classify")

        assert result == "claude response"
        mock_claude.assert_called_once()


# ── Temporal sequence mining tests ───────────────────────────────────


class TestTemporalSequenceMining:
    """Verify _suggest_next_step infers follow-up tasks from history."""

    def _insert_task(self, db_path, task_id, user_id, message, task_type,
                     status, created_at, completed_at):
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO tasks (id, user_id, message, task_type, status, created_at, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, user_id, message, task_type, status, created_at, completed_at),
        )
        conn.commit()
        conn.close()

    def test_suggests_frequent_follow_up(self, tasks_db):
        """3 sequences of 'scrape jobs' → 'clean jobs' within 30 min → suggestion returned."""
        from brain.nodes.deliverer import _suggest_next_step

        base = datetime(2026, 2, 1, 10, 0, 0, tzinfo=timezone.utc)
        for i in range(3):
            offset = timedelta(hours=i * 2)
            t1_created = (base + offset).isoformat()
            t1_completed = (base + offset + timedelta(minutes=5)).isoformat()
            t2_created = (base + offset + timedelta(minutes=10)).isoformat()
            t2_completed = (base + offset + timedelta(minutes=15)).isoformat()

            self._insert_task(
                tasks_db, f"t1-{i}", 123, "scrape jobs for JobScraper", "project",
                "completed", t1_created, t1_completed,
            )
            self._insert_task(
                tasks_db, f"t2-{i}", 123, "clean jobs data", "project",
                "completed", t2_created, t2_completed,
            )

        result = _suggest_next_step("JobScraper", 123)
        assert result is not None
        assert "clean jobs" in result.lower()
        assert "3 times" in result

    def test_single_occurrence_returns_none(self, tasks_db):
        """Only 1 occurrence → below frequency threshold → returns None."""
        from brain.nodes.deliverer import _suggest_next_step

        base = datetime(2026, 2, 1, 10, 0, 0, tzinfo=timezone.utc)
        self._insert_task(
            tasks_db, "t1", 123, "scrape jobs for JobScraper", "project",
            "completed", base.isoformat(), (base + timedelta(minutes=5)).isoformat(),
        )
        self._insert_task(
            tasks_db, "t2", 123, "clean jobs data", "project",
            "completed", (base + timedelta(minutes=10)).isoformat(),
            (base + timedelta(minutes=15)).isoformat(),
        )

        result = _suggest_next_step("JobScraper", 123)
        assert result is None  # Only 1 occurrence, threshold is 2

    def test_tasks_far_apart_returns_none(self, tasks_db):
        """Follow-up tasks >30 min apart → not a sequence → returns None."""
        from brain.nodes.deliverer import _suggest_next_step

        base = datetime(2026, 2, 1, 10, 0, 0, tzinfo=timezone.utc)
        for i in range(3):
            offset = timedelta(hours=i * 2)
            t1_created = (base + offset).isoformat()
            t1_completed = (base + offset + timedelta(minutes=5)).isoformat()
            # Follow-up is 2 hours later — well beyond 30 min window
            t2_created = (base + offset + timedelta(hours=2)).isoformat()
            t2_completed = (base + offset + timedelta(hours=2, minutes=5)).isoformat()

            self._insert_task(
                tasks_db, f"t1-{i}", 123, "scrape jobs for JobScraper", "project",
                "completed", t1_created, t1_completed,
            )
            self._insert_task(
                tasks_db, f"t2-{i}", 123, "clean jobs data", "project",
                "completed", t2_created, t2_completed,
            )

        result = _suggest_next_step("JobScraper", 123)
        assert result is None  # Tasks too far apart
