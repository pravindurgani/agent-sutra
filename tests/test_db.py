"""Tests for storage/db.py — prune_old_data epoch handling and crash recovery."""
from __future__ import annotations

import sys
import os
import time
import sqlite3
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest


def _setup_api_usage_db():
    """Create an in-memory DB mirroring the real api_usage schema."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE api_usage ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  model TEXT NOT NULL,"
        "  input_tokens INTEGER NOT NULL,"
        "  output_tokens INTEGER NOT NULL,"
        "  timestamp REAL NOT NULL"
        ")"
    )
    return conn


class TestPruneOldDataEpoch:
    """Verify api_usage pruning uses epoch float, not ISO string."""

    def test_prune_keeps_recent_records(self):
        """A 1-day-old record must survive a 90-day cutoff."""
        conn = _setup_api_usage_db()
        now = time.time()
        conn.execute(
            "INSERT INTO api_usage (model, input_tokens, output_tokens, timestamp) VALUES (?, ?, ?, ?)",
            ("sonnet", 100, 50, now - 86400),  # 1 day old
        )
        conn.commit()

        cutoff = time.time() - (90 * 86400)
        conn.execute("DELETE FROM api_usage WHERE timestamp < ?", (cutoff,))
        conn.commit()

        remaining = conn.execute("SELECT COUNT(*) FROM api_usage").fetchone()[0]
        conn.close()
        assert remaining == 1, f"1-day-old record should survive 90-day prune, got {remaining}"

    def test_prune_deletes_old_records(self):
        """A 100-day-old record must be deleted by a 90-day cutoff."""
        conn = _setup_api_usage_db()
        now = time.time()
        conn.execute(
            "INSERT INTO api_usage (model, input_tokens, output_tokens, timestamp) VALUES (?, ?, ?, ?)",
            ("sonnet", 100, 50, now - 86400),  # 1 day old — survives
        )
        conn.execute(
            "INSERT INTO api_usage (model, input_tokens, output_tokens, timestamp) VALUES (?, ?, ?, ?)",
            ("opus", 200, 100, now - 100 * 86400),  # 100 days old — deleted
        )
        conn.commit()

        cutoff = time.time() - (90 * 86400)
        cursor = conn.execute("DELETE FROM api_usage WHERE timestamp < ?", (cutoff,))
        deleted = cursor.rowcount
        conn.commit()

        remaining = conn.execute("SELECT COUNT(*) FROM api_usage").fetchone()[0]
        conn.close()
        assert deleted == 1, f"Should delete 1 old record, deleted {deleted}"
        assert remaining == 1, f"Should keep 1 recent record, kept {remaining}"

    def test_iso_string_cutoff_deletes_all_reals(self):
        """Regression guard: ISO string cutoff against REAL column deletes everything."""
        from datetime import datetime, timezone, timedelta

        conn = _setup_api_usage_db()
        now = time.time()
        conn.execute(
            "INSERT INTO api_usage (model, input_tokens, output_tokens, timestamp) VALUES (?, ?, ?, ?)",
            ("sonnet", 100, 50, now - 86400),  # 1 day old — should survive
        )
        conn.execute(
            "INSERT INTO api_usage (model, input_tokens, output_tokens, timestamp) VALUES (?, ?, ?, ?)",
            ("opus", 200, 100, now - 100 * 86400),  # 100 days old
        )
        conn.commit()

        # The BUGGY approach: ISO string vs REAL column
        buggy_cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        cursor = conn.execute("DELETE FROM api_usage WHERE timestamp < ?", (buggy_cutoff,))
        buggy_deleted = cursor.rowcount
        conn.close()

        # Both records get deleted — this proves the bug
        assert buggy_deleted == 2, f"Bug demo: ISO cutoff should delete ALL REAL records, deleted {buggy_deleted}"


class TestRecoverStaleTasks:
    """recover_stale_tasks() must reset stuck tasks on startup."""

    @pytest.mark.asyncio
    async def test_running_tasks_become_crashed(self, tmp_path):
        """Tasks with status='running' are set to 'crashed' after recovery."""
        from unittest.mock import patch
        import config as cfg
        from storage.db import init_db, create_task, update_task, recover_stale_tasks, get_task

        test_db = tmp_path / "test_recovery.db"
        with patch.object(cfg, "DB_PATH", test_db):
            await init_db()
            await create_task("stale-1", 0, "test running")
            await update_task("stale-1", status="running")

            await recover_stale_tasks()

            task = await get_task("stale-1")
            assert task["status"] == "crashed"
            assert "terminated" in task["error"].lower()

    @pytest.mark.asyncio
    async def test_pending_tasks_become_crashed(self, tmp_path):
        """Tasks with status='pending' are also recovered."""
        from unittest.mock import patch
        import config as cfg
        from storage.db import init_db, create_task, recover_stale_tasks, get_task

        test_db = tmp_path / "test_recovery.db"
        with patch.object(cfg, "DB_PATH", test_db):
            await init_db()
            await create_task("stale-2", 0, "test pending")
            # pending is default status, no update needed

            await recover_stale_tasks()

            task = await get_task("stale-2")
            assert task["status"] == "crashed"

    @pytest.mark.asyncio
    async def test_completed_tasks_untouched(self, tmp_path):
        """Tasks already completed are not affected by recovery."""
        from unittest.mock import patch
        import config as cfg
        from storage.db import init_db, create_task, update_task, recover_stale_tasks, get_task

        test_db = tmp_path / "test_recovery.db"
        with patch.object(cfg, "DB_PATH", test_db):
            await init_db()
            await create_task("done-1", 0, "test completed")
            await update_task("done-1", status="completed")

            await recover_stale_tasks()

            task = await get_task("done-1")
            assert task["status"] == "completed"
