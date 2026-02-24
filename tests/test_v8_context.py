"""Tests for AgentSutra v8 Phase 2: Context & Memory Layer.

Covers:
- Project memory DB operations (sync_write / sync_query)
- Deliverer memory extraction (_extract_and_store_memory)
- Planner memory injection (LESSONS LEARNED)
- Dynamic file injection (_inject_project_files)
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import config


# ── Helpers ──────────────────────────────────────────────────────────

@pytest.fixture()
def memory_db(tmp_path):
    """Create an in-memory-style temp DB with the project_memory table."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS project_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_name TEXT NOT NULL,
            memory_type TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            task_id TEXT,
            UNIQUE(project_name, memory_type, content)
        )
    """)
    conn.commit()
    conn.close()
    with patch.object(config, "DB_PATH", db_path):
        yield db_path


# ── Database operation tests ─────────────────────────────────────────


class TestProjectMemoryDB:
    """Verify sync_write_project_memory / sync_query_project_memories."""

    def test_write_and_read_back(self, memory_db):
        from storage.db import sync_write_project_memory, sync_query_project_memories

        sync_write_project_memory("testproj", "success_pattern", "Did X, worked.", "t1")
        rows = sync_query_project_memories("testproj", limit=5)
        assert len(rows) == 1
        assert rows[0] == ("success_pattern", "Did X, worked.")

    def test_unique_constraint_no_error(self, memory_db):
        """INSERT OR IGNORE: writing the same tuple twice must not raise."""
        from storage.db import sync_write_project_memory, sync_query_project_memories

        sync_write_project_memory("testproj", "success_pattern", "same content", "t1")
        sync_write_project_memory("testproj", "success_pattern", "same content", "t2")
        rows = sync_query_project_memories("testproj", limit=10)
        assert len(rows) == 1  # Deduplicated

    def test_query_returns_most_recent_first(self, memory_db):
        from storage.db import sync_write_project_memory, sync_query_project_memories

        sync_write_project_memory("proj", "success_pattern", "old entry", "t1")
        time.sleep(0.05)  # Ensure distinct timestamps
        sync_write_project_memory("proj", "failure_pattern", "new entry", "t2")

        rows = sync_query_project_memories("proj", limit=5)
        assert len(rows) == 2
        # Most recent first (ORDER BY created_at DESC)
        assert rows[0][1] == "new entry"
        assert rows[1][1] == "old entry"

    def test_limit_respected(self, memory_db):
        from storage.db import sync_write_project_memory, sync_query_project_memories

        for i in range(10):
            sync_write_project_memory("proj", "success_pattern", f"entry {i}", f"t{i}")
            time.sleep(0.01)

        rows = sync_query_project_memories("proj", limit=5)
        assert len(rows) == 5


# ── Deliverer memory extraction tests ────────────────────────────────


class TestDelivererMemoryExtraction:
    """Verify _extract_and_store_memory in deliverer.py."""

    def test_success_pattern_stored(self, memory_db):
        from brain.nodes.deliverer import _extract_and_store_memory
        from storage.db import sync_query_project_memories

        state = {
            "project_name": "iGBreport",
            "audit_verdict": "pass",
            "task_id": "task-abc",
            "message": "Generate iGB report for Light & Wonder",
            "code": "python run_report.py --client 'L&W'",
            "extracted_params": {"client": "Light & Wonder"},
        }
        _extract_and_store_memory(state)

        rows = sync_query_project_memories("iGBreport", limit=5)
        assert len(rows) == 1
        assert rows[0][0] == "success_pattern"
        assert "Light & Wonder" in rows[0][1]

    def test_failure_pattern_stored(self, memory_db):
        from brain.nodes.deliverer import _extract_and_store_memory
        from storage.db import sync_query_project_memories

        state = {
            "project_name": "iGBreport",
            "audit_verdict": "fail",
            "task_id": "task-def",
            "message": "Generate report for Kambi",
            "audit_feedback": "Exit code 1: FileNotFoundError",
        }
        _extract_and_store_memory(state)

        rows = sync_query_project_memories("iGBreport", limit=5)
        assert len(rows) == 1
        assert rows[0][0] == "failure_pattern"
        assert "FileNotFoundError" in rows[0][1]

    def test_no_project_name_writes_nothing(self, memory_db):
        from brain.nodes.deliverer import _extract_and_store_memory
        from storage.db import sync_query_project_memories

        state = {
            "audit_verdict": "pass",
            "task_id": "task-xyz",
            "message": "Write a hello world script",
        }
        _extract_and_store_memory(state)

        # No project → nothing stored for any project
        rows = sync_query_project_memories("", limit=10)
        assert len(rows) == 0


# ── Planner memory injection tests ───────────────────────────────────


class TestPlannerMemoryInjection:
    """Verify that project memories appear in the planner system prompt."""

    def test_lessons_injected_for_project_task(self, memory_db, tmp_path):
        from storage.db import sync_write_project_memory

        # Seed memories
        sync_write_project_memory("iGBreport", "success_pattern", "Used --client flag correctly", "t1")
        sync_write_project_memory("iGBreport", "failure_pattern", "Missing venv activation", "t2")

        state = {
            "task_id": "test-plan-1",
            "task_type": "project",
            "project_name": "iGBreport",
            "project_config": {
                "name": "iGBreport",
                "path": str(tmp_path),
                "description": "iGB report generator",
                "commands": {"run": "echo hello"},
            },
            "message": "Generate report for Kambi",
        }

        with patch("tools.claude_client.call", return_value="1. Run report") as mock_call:
            from brain.nodes.planner import plan
            plan(state)
            _, kwargs = mock_call.call_args
            assert "LESSONS LEARNED" in kwargs.get("system", "")
            assert "IGBREPORT" in kwargs.get("system", "")  # Uppercased project name

    def test_no_lessons_for_non_project_task(self, memory_db):
        from storage.db import sync_write_project_memory

        sync_write_project_memory("iGBreport", "success_pattern", "something", "t1")

        state = {
            "task_id": "test-plan-2",
            "task_type": "code",
            "message": "Write a hello world script",
        }

        with patch("tools.claude_client.call", return_value="1. Write script") as mock_call:
            from brain.nodes.planner import plan
            plan(state)
            _, kwargs = mock_call.call_args
            assert "LESSONS LEARNED" not in kwargs.get("system", "")


# ── Dynamic file injection tests ─────────────────────────────────────


class TestDynamicFileInjection:
    """Verify _inject_project_files selects and injects relevant files."""

    def test_injects_files_for_small_project(self, tmp_path):
        """Project with 3 .py files → system prompt gets RELEVANT CODE FROM."""
        # Create a small project directory
        (tmp_path / "main.py").write_text("print('hello')")
        (tmp_path / "utils.py").write_text("def helper(): pass")
        (tmp_path / "config.py").write_text("DB_URL = 'sqlite:///test.db'")

        state = {
            "task_type": "project",
            "project_name": "testproj",
            "project_config": {"path": str(tmp_path)},
            "message": "Run the main script",
        }

        # Mock Claude to return a selection
        mock_selection = json.dumps(["main.py", "config.py"])
        with patch("tools.claude_client.call", return_value=mock_selection):
            from brain.nodes.planner import _inject_project_files
            result = _inject_project_files(state, "BASE SYSTEM")

        assert "RELEVANT CODE FROM TESTPROJ" in result
        assert "print('hello')" in result
        assert "DB_URL" in result

    def test_skips_large_project(self, tmp_path):
        """Project with >50 files → injection skipped, system returned unmodified."""
        for i in range(55):
            (tmp_path / f"module_{i}.py").write_text(f"# module {i}")

        state = {
            "task_type": "project",
            "project_name": "bigproj",
            "project_config": {"path": str(tmp_path)},
            "message": "Run tests",
        }

        from brain.nodes.planner import _inject_project_files
        result = _inject_project_files(state, "BASE SYSTEM")
        assert result == "BASE SYSTEM"  # Unmodified

    def test_invalid_json_skips_gracefully(self, tmp_path):
        """Claude returns garbage → injection skips, no crash."""
        (tmp_path / "app.py").write_text("import flask")

        state = {
            "task_type": "project",
            "project_name": "testproj",
            "project_config": {"path": str(tmp_path)},
            "message": "Run the app",
        }

        with patch("tools.claude_client.call", return_value="Sorry, I can't do that"):
            from brain.nodes.planner import _inject_project_files
            result = _inject_project_files(state, "BASE SYSTEM")

        assert result == "BASE SYSTEM"  # Unmodified, no crash

    def test_excludes_pycache_dirs(self, tmp_path):
        """Files inside __pycache__ should not appear in the file tree."""
        (tmp_path / "app.py").write_text("main code")
        pycache = tmp_path / "__pycache__"
        pycache.mkdir()
        (pycache / "app.cpython-311.pyc").write_text("bytecode")

        state = {
            "task_type": "project",
            "project_name": "testproj",
            "project_config": {"path": str(tmp_path)},
            "message": "Run app",
        }

        mock_selection = json.dumps(["app.py"])
        with patch("tools.claude_client.call", return_value=mock_selection) as mock_call:
            from brain.nodes.planner import _inject_project_files
            _inject_project_files(state, "BASE SYSTEM")

        # The file tree sent to Claude should NOT contain __pycache__
        selector_prompt = mock_call.call_args[0][0]
        assert "__pycache__" not in selector_prompt
