"""Tests for brain/graph.py — pipeline wiring, stage tracking, completion summary."""
from __future__ import annotations

import sys
import os
import logging

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import patch


class TestRunTaskCompletionSummary:
    """run_task() must log a structured summary line on completion."""

    @patch("brain.graph.agent_graph")
    def test_run_task_logs_completion_summary(self, mock_graph, caplog):
        """Completed task produces INFO line with timing, verdict, and task type."""
        mock_graph.invoke.return_value = {
            "task_id": "abc12345-dead-beef",
            "audit_verdict": "pass",
            "task_type": "code",
            "stage_timings": [
                {"name": "classifying", "duration_ms": 150},
                {"name": "planning", "duration_ms": 2000},
                {"name": "executing", "duration_ms": 5000},
                {"name": "auditing", "duration_ms": 1200},
                {"name": "delivering", "duration_ms": 300},
            ],
        }

        from brain.graph import run_task

        with caplog.at_level(logging.INFO, logger="brain.graph"):
            run_task("abc12345-dead-beef", user_id=1, message="test task")

        # Find the completion summary line
        summary_records = [r for r in caplog.records if "completed in" in r.message]
        assert len(summary_records) == 1

        msg = summary_records[0].message
        assert "abc12345-dead-beef" in msg
        assert "8.7s" in msg  # 150+2000+5000+1200+300 = 8650ms = 8.7s
        assert "verdict=pass" in msg
        assert "type=code" in msg
        assert "classifying:0.1s" in msg  # 150ms rounds to 0.1s (not 0.2)
        assert "executing:5.0s" in msg

    @patch("brain.graph.agent_graph")
    def test_run_task_logs_summary_on_failure(self, mock_graph, caplog):
        """Failed task (verdict=fail) still gets a summary line."""
        mock_graph.invoke.return_value = {
            "task_id": "fail1234-dead-beef",
            "audit_verdict": "fail",
            "task_type": "data",
            "stage_timings": [
                {"name": "classifying", "duration_ms": 100},
                {"name": "planning", "duration_ms": 500},
                {"name": "executing", "duration_ms": 3000},
                {"name": "auditing", "duration_ms": 800},
                {"name": "delivering", "duration_ms": 200},
            ],
        }

        from brain.graph import run_task

        with caplog.at_level(logging.INFO, logger="brain.graph"):
            run_task("fail1234-dead-beef", user_id=1, message="failing task")

        summary_records = [r for r in caplog.records if "completed in" in r.message]
        assert len(summary_records) == 1
        assert "verdict=fail" in summary_records[0].message
