"""Integration tests for deployer wiring in the deliverer.

Covers:
- Deploy triggered for frontend/ui_design tasks with pass verdict
- Deploy NOT triggered for non-frontend tasks or failed tasks
- Deploy failure doesn't crash delivery
- deploy_url appears in debug sidecar
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import config
from brain.nodes.deliverer import deliver, _write_debug_sidecar


def _make_state(**overrides) -> dict:
    """Build a minimal AgentState dict with sensible defaults."""
    base = {
        "task_id": "test-1234",
        "user_id": 1,
        "message": "build a landing page",
        "files": [],
        "task_type": "frontend",
        "project_name": "",
        "project_config": {},
        "plan": "build it",
        "code": "<html><body>Hello</body></html>",
        "execution_result": "Execution: SUCCESS (exit code 0)\nOutput:\nFrontend generated",
        "audit_verdict": "pass",
        "audit_feedback": "",
        "retry_count": 0,
        "stage": "delivering",
        "extracted_params": {},
        "working_dir": "",
        "conversation_context": "",
        "auto_installed_packages": [],
        "stage_timings": [],
        "final_response": "",
        "artifacts": [],
        "deploy_url": "",
    }
    base.update(overrides)
    return base


class TestDeployTriggered:
    """Verify deployer is called at the right time in the deliverer."""

    def test_deploy_triggered_on_frontend_pass(self, tmp_path):
        """Deploy is called when task_type='frontend' and verdict='pass'."""
        # Create a fake artifact
        artifact = tmp_path / "page.html"
        artifact.write_text("<h1>Hello</h1>")

        state = _make_state(
            task_type="frontend",
            audit_verdict="pass",
            artifacts=[str(artifact)],
        )

        mock_deploy = MagicMock(return_value="https://example.github.io/test/")

        with (
            patch("brain.nodes.deliverer.deploy", mock_deploy, create=True),
            patch("tools.deployer.deploy", mock_deploy),
            patch("tools.claude_client.call", return_value="Task completed."),
        ):
            result = deliver(state)

        mock_deploy.assert_called_once()
        assert result["deploy_url"] == "https://example.github.io/test/"

    def test_deploy_triggered_on_ui_design_pass(self, tmp_path):
        """Deploy is also called for ui_design tasks."""
        artifact = tmp_path / "design.html"
        artifact.write_text("<h1>Design</h1>")

        state = _make_state(
            task_type="ui_design",
            audit_verdict="pass",
            artifacts=[str(artifact)],
        )

        mock_deploy = MagicMock(return_value="https://example.github.io/design/")

        with (
            patch("tools.deployer.deploy", mock_deploy),
            patch("tools.claude_client.call", return_value="Task completed."),
        ):
            result = deliver(state)

        mock_deploy.assert_called_once()
        assert result["deploy_url"] == "https://example.github.io/design/"


class TestDeployNotTriggered:
    """Verify deployer is NOT called in the wrong situations."""

    def test_deploy_not_triggered_on_code_task(self, tmp_path):
        """Deploy is NOT called for task_type='code'."""
        artifact = tmp_path / "script.py"
        artifact.write_text("print('hello')")

        state = _make_state(
            task_type="code",
            audit_verdict="pass",
            artifacts=[str(artifact)],
        )

        mock_deploy = MagicMock()

        with (
            patch("tools.deployer.deploy", mock_deploy),
            patch("tools.claude_client.call", return_value="Task completed."),
        ):
            result = deliver(state)

        mock_deploy.assert_not_called()
        assert result["deploy_url"] == ""

    def test_deploy_not_triggered_on_fail(self, tmp_path):
        """Deploy is NOT called when verdict='fail'."""
        state = _make_state(
            task_type="frontend",
            audit_verdict="fail",
            audit_feedback="CSS is broken",
            artifacts=[],
        )

        mock_deploy = MagicMock()

        with (
            patch("tools.deployer.deploy", mock_deploy),
            patch("tools.claude_client.call", return_value="Task failed."),
        ):
            result = deliver(state)

        mock_deploy.assert_not_called()
        assert result["deploy_url"] == ""

    def test_deploy_not_triggered_without_artifacts(self):
        """Deploy is NOT called when there are no artifacts."""
        state = _make_state(
            task_type="frontend",
            audit_verdict="pass",
            artifacts=[],
        )

        mock_deploy = MagicMock()

        with (
            patch("tools.deployer.deploy", mock_deploy),
            patch("tools.claude_client.call", return_value="Task completed."),
        ):
            result = deliver(state)

        mock_deploy.assert_not_called()
        assert result["deploy_url"] == ""


class TestDeployGracefulDegradation:
    """Verify deploy failure doesn't crash delivery."""

    def test_deploy_failure_doesnt_crash_delivery(self, tmp_path):
        """When deployer raises, delivery still completes with empty deploy_url."""
        artifact = tmp_path / "page.html"
        artifact.write_text("<h1>Hello</h1>")

        state = _make_state(
            task_type="frontend",
            audit_verdict="pass",
            artifacts=[str(artifact)],
        )

        mock_deploy = MagicMock(side_effect=RuntimeError("deploy exploded"))

        with (
            patch("tools.deployer.deploy", mock_deploy),
            patch("tools.claude_client.call", return_value="Task completed."),
        ):
            result = deliver(state)

        # Delivery still completes
        assert "Task completed." in result["final_response"]
        assert result["deploy_url"] == ""


class TestDeployUrlInSidecar:
    """Verify deploy_url appears in debug sidecar JSON."""

    def test_deploy_url_in_debug_sidecar(self, tmp_path):
        """deploy_url appears in the sidecar JSON output."""
        state = _make_state(deploy_url="https://example.github.io/test/")

        with patch.object(config, "OUTPUTS_DIR", tmp_path):
            _write_debug_sidecar(state)

        sidecar_path = tmp_path / "test-1234.debug.json"
        assert sidecar_path.exists()
        data = json.loads(sidecar_path.read_text())
        assert data["deploy_url"] == "https://example.github.io/test/"

    def test_empty_deploy_url_in_sidecar(self, tmp_path):
        """Empty deploy_url is still present in sidecar."""
        state = _make_state(deploy_url="")

        with patch.object(config, "OUTPUTS_DIR", tmp_path):
            _write_debug_sidecar(state)

        sidecar_path = tmp_path / "test-1234.debug.json"
        data = json.loads(sidecar_path.read_text())
        assert data["deploy_url"] == ""
