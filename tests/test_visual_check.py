"""Tests for visual verification module (tools/visual_check.py).

Covers:
- Successful page check with mocked Playwright
- Graceful skip when Playwright not installed
- Navigation timeout handling
- Console error capture
- Auditor prompt includes visual context
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch, MagicMock


class TestCheckPageSuccess:
    """Verify check_page returns correct fields on success."""

    def test_check_page_success(self, tmp_path):
        """Mocked Playwright returns 200, screenshot saved, result fields correct."""
        from tools.visual_check import check_page

        mock_response = MagicMock()
        mock_response.status = 200

        mock_page = MagicMock()
        mock_page.goto.return_value = mock_response

        mock_browser = MagicMock()
        mock_browser.new_page.return_value = mock_page

        mock_playwright = MagicMock()
        mock_playwright.chromium.launch.return_value = mock_browser

        mock_cm = MagicMock()
        mock_cm.__enter__ = MagicMock(return_value=mock_playwright)
        mock_cm.__exit__ = MagicMock(return_value=False)

        with patch("playwright.sync_api.sync_playwright", return_value=mock_cm, create=True):
            result = check_page("http://127.0.0.1:8100", tmp_path, timeout=10)

        assert result.checked is True
        assert result.loads is True
        assert result.status_code == 200
        assert result.error == ""
        mock_page.screenshot.assert_called_once()


class TestCheckPageNoPlaywright:
    """Verify graceful skip when Playwright is not installed."""

    def test_check_page_no_playwright(self, tmp_path):
        """ImportError → checked=False, error message set."""
        # Remove playwright from sys.modules to simulate missing install
        import sys
        with patch.dict(sys.modules, {"playwright": None, "playwright.sync_api": None}):
            # Force reimport
            import importlib
            import tools.visual_check as vc_mod
            # Patch the import inside check_page
            with patch("builtins.__import__", side_effect=ImportError("No module named 'playwright'")):
                result = vc_mod.check_page("http://127.0.0.1:8100", tmp_path)

        assert result.checked is False
        assert result.loads is False
        assert "not installed" in result.error.lower() or "playwright" in result.error.lower()


class TestCheckPageTimeout:
    """Verify timeout during navigation is handled gracefully."""

    def test_check_page_timeout(self, tmp_path):
        """Navigation timeout → checked=True, loads=False, error set."""
        from tools.visual_check import check_page

        mock_page = MagicMock()
        mock_page.goto.side_effect = TimeoutError("Navigation timeout")

        mock_browser = MagicMock()
        mock_browser.new_page.return_value = mock_page

        mock_playwright = MagicMock()
        mock_playwright.chromium.launch.return_value = mock_browser

        mock_cm = MagicMock()
        mock_cm.__enter__ = MagicMock(return_value=mock_playwright)
        mock_cm.__exit__ = MagicMock(return_value=False)

        with patch("playwright.sync_api.sync_playwright", return_value=mock_cm, create=True):
            result = check_page("http://127.0.0.1:8100", tmp_path, timeout=1)

        assert result.checked is True
        assert result.loads is False
        assert "timeout" in result.error.lower()


class TestCheckPageConsoleErrors:
    """Verify console errors are captured."""

    def test_check_page_console_errors(self, tmp_path):
        """Console error messages are captured in result."""
        from tools.visual_check import check_page

        mock_response = MagicMock()
        mock_response.status = 200

        mock_page = MagicMock()
        mock_page.goto.return_value = mock_response

        # Simulate console error callback
        console_callbacks = []

        def fake_on(event, callback):
            if event == "console":
                console_callbacks.append(callback)

        mock_page.on = fake_on

        mock_browser = MagicMock()
        mock_browser.new_page.return_value = mock_page

        mock_playwright = MagicMock()
        mock_playwright.chromium.launch.return_value = mock_browser

        mock_cm = MagicMock()
        mock_cm.__enter__ = MagicMock(return_value=mock_playwright)
        mock_cm.__exit__ = MagicMock(return_value=False)

        # Trigger console error after page.goto but before result collection
        def fake_goto(url, **kwargs):
            # Simulate console error firing during page load
            for cb in console_callbacks:
                error_msg = MagicMock()
                error_msg.type = "error"
                error_msg.text = "Uncaught ReferenceError: foo is not defined"
                cb(error_msg)
            return mock_response

        mock_page.goto = fake_goto

        with patch("playwright.sync_api.sync_playwright", return_value=mock_cm, create=True):
            result = check_page("http://127.0.0.1:8100", tmp_path, timeout=10)

        assert result.checked is True
        assert result.loads is True
        assert len(result.console_errors) == 1
        assert "ReferenceError" in result.console_errors[0]


class TestAuditorIncludesVisualContext:
    """Verify auditor includes visual check results in the prompt."""

    def test_auditor_includes_visual_context(self):
        """When visual check is enabled and server_url set, audit prompt includes VISUAL VERIFICATION."""
        from brain.nodes.auditor import audit

        state = {
            "task_id": "test-vc-1",
            "user_id": 1,
            "message": "build a landing page",
            "files": [],
            "task_type": "frontend",
            "project_name": "",
            "project_config": {},
            "plan": "Build a landing page",
            "code": "<html><body>Hello</body></html>",
            "execution_result": "SUCCESS: HTML generated",
            "audit_verdict": "",
            "audit_feedback": "",
            "retry_count": 0,
            "stage": "auditing",
            "extracted_params": {},
            "working_dir": "",
            "conversation_context": "",
            "auto_installed_packages": [],
            "stage_timings": [],
            "server_url": "http://127.0.0.1:8100",
            "deploy_url": "",
            "final_response": "",
            "artifacts": [],
        }

        from tools.visual_check import VisualCheckResult

        mock_vc_result = VisualCheckResult(
            checked=True,
            loads=True,
            status_code=200,
            console_errors=["Uncaught TypeError"],
        )

        captured_prompt = {}

        def mock_claude_call(prompt, **kwargs):
            captured_prompt["value"] = prompt
            return '{"verdict": "pass", "feedback": "Looks good"}'

        import config
        with (
            patch.object(config, "VISUAL_CHECK_ENABLED", True),
            patch("tools.visual_check.check_page", return_value=mock_vc_result),
            patch("tools.claude_client.call", side_effect=mock_claude_call),
        ):
            audit(state)

        assert "VISUAL VERIFICATION" in captured_prompt["value"]
        assert "Page loads: True" in captured_prompt["value"]
        assert "HTTP status: 200" in captured_prompt["value"]
        assert "Uncaught TypeError" in captured_prompt["value"]
