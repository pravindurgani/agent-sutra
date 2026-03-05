"""Integration tests for server management wiring in executor and deliverer.

Covers:
- Server started for frontend/ui_design tasks after HTML generation
- Server NOT started for non-frontend tasks
- Server failure doesn't crash execution
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch, MagicMock

import config


def _make_state(**overrides) -> dict:
    """Build a minimal AgentState dict with sensible defaults."""
    base = {
        "task_id": "test-srv-int-1",
        "user_id": 1,
        "message": "build a landing page",
        "files": [],
        "task_type": "frontend",
        "project_name": "",
        "project_config": {},
        "plan": "Build a landing page with a hero section",
        "code": "",
        "execution_result": "",
        "audit_verdict": "",
        "audit_feedback": "",
        "retry_count": 0,
        "stage": "executing",
        "extracted_params": {},
        "working_dir": "",
        "conversation_context": "",
        "auto_installed_packages": [],
        "stage_timings": [],
        "server_url": "",
        "deploy_url": "",
        "final_response": "",
        "artifacts": [],
    }
    base.update(overrides)
    return base


class TestServerStartedForFrontend:
    """Verify server is started for frontend tasks after HTML generation."""

    def test_server_started_for_frontend_task(self, tmp_path):
        """start_server is called after HTML generation for frontend tasks."""
        from brain.nodes.executor import _execute_html_generation

        state = _make_state(task_type="frontend")

        mock_start = MagicMock(return_value=("http://127.0.0.1:8100", 8100))

        with (
            patch("tools.claude_client.call", return_value="<html><body>Hello</body></html>"),
            patch.object(config, "OUTPUTS_DIR", tmp_path),
            patch("tools.sandbox.start_server", mock_start),
        ):
            result = _execute_html_generation(
                state,
                system="test",
                max_tokens=8192,
                log_label="Frontend app",
                filename_base="app",
                preview_extensions=(".html",),
            )

        mock_start.assert_called_once()
        assert result.get("server_url") == "http://127.0.0.1:8100"

    def test_server_started_for_ui_design_task(self, tmp_path):
        """start_server is called after HTML generation for ui_design tasks."""
        from brain.nodes.executor import _execute_html_generation

        state = _make_state(task_type="ui_design")

        mock_start = MagicMock(return_value=("http://127.0.0.1:8101", 8101))

        with (
            patch("tools.claude_client.call", return_value="<html><body>Design</body></html>"),
            patch.object(config, "OUTPUTS_DIR", tmp_path),
            patch("tools.sandbox.start_server", mock_start),
        ):
            result = _execute_html_generation(
                state,
                system="test",
                max_tokens=8192,
                log_label="UI design",
                filename_base="design",
                preview_extensions=(".html",),
            )

        mock_start.assert_called_once()
        assert result.get("server_url") == "http://127.0.0.1:8101"


class TestServerNotStartedForCode:
    """Verify server is NOT started for non-frontend tasks."""

    def test_server_not_started_for_code_task(self, tmp_path):
        """execute() for code tasks does NOT call start_server."""
        from brain.nodes.executor import execute

        state = _make_state(task_type="code", plan="print hello world")

        mock_start = MagicMock()

        with (
            patch("tools.claude_client.call", return_value="print('hello')"),
            patch.object(config, "OUTPUTS_DIR", tmp_path),
            patch("tools.sandbox.start_server", mock_start),
            patch("brain.nodes.executor.run_code_with_auto_install") as mock_run,
        ):
            mock_run.return_value = MagicMock(
                success=True, stdout="hello", stderr="",
                files_created=[], auto_installed=[],
            )
            result = execute(state)

        mock_start.assert_not_called()
        assert result.get("server_url", "") == ""


class TestServerFailureGraceful:
    """Verify server failure doesn't crash execution."""

    def test_server_failure_doesnt_crash_execution(self, tmp_path):
        """When start_server raises, HTML generation still returns successfully."""
        from brain.nodes.executor import _execute_html_generation

        state = _make_state(task_type="frontend")

        mock_start = MagicMock(side_effect=RuntimeError("port exhausted"))

        with (
            patch("tools.claude_client.call", return_value="<html><body>Hello</body></html>"),
            patch.object(config, "OUTPUTS_DIR", tmp_path),
            patch("tools.sandbox.start_server", mock_start),
        ):
            result = _execute_html_generation(
                state,
                system="test",
                max_tokens=8192,
                log_label="Frontend app",
                filename_base="app",
                preview_extensions=(".html",),
            )

        # HTML generation still succeeded
        assert "SUCCESS" in result["execution_result"]
        assert len(result["artifacts"]) == 1
        # But no server_url
        assert result.get("server_url", "") == ""
