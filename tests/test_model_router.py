"""Tests for tools/model_router.py — cost calculation defaults."""
from __future__ import annotations

import sys
import os
import sqlite3
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import patch
from tools.model_router import _get_today_spend


class TestGetTodaySpend:
    """_get_today_spend() must use conservative cost defaults for unknown models."""

    @patch("tools.claude_client._usage_db_path", None)
    def test_get_today_spend_unknown_model_uses_expensive_default(self, tmp_path):
        """Unknown model 'claude-unknown-99' uses 15.00/75.00 rates (not 3.00/15.00)."""
        db_path = tmp_path / "usage.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE api_usage "
            "(model TEXT, input_tokens INTEGER, output_tokens INTEGER, "
            "thinking_tokens INTEGER, timestamp REAL)"
        )
        conn.execute(
            "INSERT INTO api_usage VALUES (?, ?, ?, ?, ?)",
            ("claude-unknown-99", 1_000_000, 1_000_000, 0, time.time()),
        )
        conn.commit()
        conn.close()

        with patch("tools.claude_client._usage_db_path", db_path), \
             patch("tools.claude_client._usage_db_initialized", True):
            cost = _get_today_spend()

        # With 15.00/75.00 rates: (1M * 15.00 + 1M * 75.00) / 1M = 90.00
        # With old 3.00/15.00 rates it would be: (1M * 3.00 + 1M * 15.00) / 1M = 18.00
        assert cost > 80.0, f"Expected >$80 with conservative defaults, got ${cost:.2f}"
        assert cost < 100.0, f"Unexpectedly high: ${cost:.2f}"
