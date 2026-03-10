"""Tests for brain/nodes/planner.py — ARCHITECTURE.md injection."""
from __future__ import annotations

import sys
import os
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pathlib import Path
from unittest.mock import patch, MagicMock

from brain.nodes.planner import plan


def _make_project_state(project_path: str) -> dict:
    """Create a minimal project state for planner tests."""
    return {
        "task_id": "arch-test-1234",
        "user_id": 1,
        "message": "Run the pipeline",
        "files": [],
        "task_type": "project",
        "project_name": "Test Project",
        "project_config": {
            "name": "Test Project",
            "path": project_path,
            "commands": {"run": "python3 run.py"},
        },
        "plan": "",
        "code": "",
        "execution_result": "",
        "audit_verdict": "",
        "audit_feedback": "",
        "retry_count": 0,
        "conversation_context": "",
    }


# ── v9.0.0 Phase 8: ARCHITECTURE.md injection ────────────────────


class TestArchitectureMdInjection:
    """Planner injects ARCHITECTURE.md into system prompt for project tasks."""

    @patch("brain.nodes.planner.sync_query_project_memories", return_value=[])
    @patch("brain.nodes.planner.route_and_call")
    def test_architecture_md_injected_when_present(self, mock_route, mock_memories):
        """ARCHITECTURE.md present → system prompt contains 'PROJECT ARCHITECTURE'."""
        mock_route.return_value = "Step 1: run the pipeline with --full-pipeline"

        with tempfile.TemporaryDirectory() as tmpdir:
            arch_path = Path(tmpdir) / "ARCHITECTURE.md"
            arch_path.write_text("# Test Project\n\n## Tech Stack\n- Python 3.11\n")

            state = _make_project_state(tmpdir)
            plan(state)

            # Check system kwarg passed to route_and_call
            call_kwargs = mock_route.call_args
            system_prompt = call_kwargs.kwargs.get("system", "") or call_kwargs[1].get("system", "")
            # Handle positional args: route_and_call(prompt, system=...)
            if not system_prompt and len(call_kwargs.args) > 1:
                system_prompt = call_kwargs.args[1]
            assert "PROJECT ARCHITECTURE" in system_prompt
            assert "Python 3.11" in system_prompt

    @patch("brain.nodes.planner.sync_query_project_memories", return_value=[])
    @patch("brain.nodes.planner.route_and_call")
    def test_architecture_md_skipped_when_missing(self, mock_route, mock_memories):
        """No ARCHITECTURE.md → system prompt does not contain it."""
        mock_route.return_value = "Step 1: run the pipeline"

        with tempfile.TemporaryDirectory() as tmpdir:
            state = _make_project_state(tmpdir)
            plan(state)

            call_kwargs = mock_route.call_args
            system_prompt = call_kwargs.kwargs.get("system", "") or call_kwargs[1].get("system", "")
            if not system_prompt and len(call_kwargs.args) > 1:
                system_prompt = call_kwargs.args[1]
            assert "PROJECT ARCHITECTURE" not in system_prompt

    @patch("brain.nodes.planner.sync_query_project_memories", return_value=[])
    @patch("brain.nodes.planner.route_and_call")
    def test_architecture_md_capped_at_5000_chars(self, mock_route, mock_memories):
        """ARCHITECTURE.md >5000 chars → injected content capped at 5000."""
        mock_route.return_value = "Step 1: run the pipeline"

        with tempfile.TemporaryDirectory() as tmpdir:
            arch_path = Path(tmpdir) / "ARCHITECTURE.md"
            arch_path.write_text("X" * 10000)

            state = _make_project_state(tmpdir)
            plan(state)

            call_kwargs = mock_route.call_args
            system_prompt = call_kwargs.kwargs.get("system", "") or call_kwargs[1].get("system", "")
            if not system_prompt and len(call_kwargs.args) > 1:
                system_prompt = call_kwargs.args[1]
            assert "PROJECT ARCHITECTURE" in system_prompt
            # The injected content should be capped — full 10000 X's should not appear
            assert "X" * 10000 not in system_prompt
            assert "X" * 5000 in system_prompt
