"""Tests for brain/nodes/auditor.py — JSON extraction and environment error detection."""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from brain.nodes.auditor import _extract_json, _detect_environment_error


class TestExtractJson:
    """Balanced-brace JSON extraction from mixed text."""

    def test_clean_json(self):
        text = '{"verdict": "pass", "feedback": "Looks good"}'
        result = _extract_json(text)
        assert result is not None
        assert result["verdict"] == "pass"

    def test_json_with_surrounding_text(self):
        text = 'Here is my review: {"verdict": "fail", "feedback": "Missing output"} End.'
        result = _extract_json(text)
        assert result is not None
        assert result["verdict"] == "fail"
        assert "Missing output" in result["feedback"]

    def test_nested_braces_in_feedback(self):
        text = '{"verdict": "fail", "feedback": "The dict {key: value} is wrong"}'
        result = _extract_json(text)
        assert result is not None
        assert result["verdict"] == "fail"

    def test_code_block_in_feedback(self):
        text = '{"verdict": "pass", "feedback": "Code uses {x: 1, y: {z: 2}} correctly"}'
        result = _extract_json(text)
        assert result is not None
        assert result["verdict"] == "pass"

    def test_stray_closing_brace(self):
        text = '} some text {"verdict": "pass", "feedback": "OK"}'
        result = _extract_json(text)
        assert result is not None
        assert result["verdict"] == "pass"

    def test_stray_opening_brace(self):
        """A stray opening brace consumes the inner JSON as part of a larger object,
        so the balanced-brace parser won't find a standalone verdict object."""
        text = '{ invalid start {"verdict": "fail", "feedback": "Bad"}'
        result = _extract_json(text)
        # The outer { never closes properly, so no valid JSON with verdict is extracted
        assert result is None

    def test_no_verdict_key(self):
        text = '{"result": "pass", "note": "This has no verdict key"}'
        result = _extract_json(text)
        assert result is None

    def test_no_json_at_all(self):
        text = "This is plain text with no JSON"
        result = _extract_json(text)
        assert result is None

    def test_empty_string(self):
        result = _extract_json("")
        assert result is None

    def test_multiple_json_objects_first_with_verdict(self):
        text = '{"verdict": "pass", "feedback": "Good"} {"other": "data"}'
        result = _extract_json(text)
        assert result is not None
        assert result["verdict"] == "pass"

    def test_deeply_nested_braces(self):
        text = '{"verdict": "fail", "feedback": "Expected {a: {b: {c: 1}}} but got {}"}'
        result = _extract_json(text)
        assert result is not None
        assert result["verdict"] == "fail"

    def test_malformed_json_with_verdict_word(self):
        text = '{verdict: pass}'
        result = _extract_json(text)
        # This contains "verdict" but isn't valid JSON — should return None
        assert result is None


# ── Environment error detection (v6.11) ───────────────────────────


class TestEnvironmentErrorDetection:
    """Auditor should short-circuit retries on environment errors."""

    def test_bad_file_descriptor_detected(self):
        result = (
            "Execution: FAILED (exit code 1)\nStderr:\n"
            "Fatal Python error: init_sys_streams\n"
            "Can't initialize sys standard streams\n"
            "OSError: [Errno 9] Bad file descriptor"
        )
        assert _detect_environment_error(result) is not None

    def test_sys_streams_detected(self):
        result = "Execution: FAILED\nStderr:\ncan't initialize sys standard streams"
        desc = _detect_environment_error(result)
        assert desc is not None
        assert "daemon" in desc.lower() or "stdin" in desc.lower()

    def test_no_space_detected(self):
        result = "Execution: FAILED\nStderr:\nNo space left on device"
        assert _detect_environment_error(result) is not None

    def test_dns_failure_detected(self):
        result = "Execution: FAILED\nStderr:\nName or service not known"
        assert _detect_environment_error(result) is not None

    def test_permission_denied_not_detected(self):
        """Permission denied is a code-level error (wrong path) — retries CAN fix it."""
        result = "Execution: FAILED\nStderr:\nPermission denied: /private/var/output"
        assert _detect_environment_error(result) is None

    def test_connection_refused_not_detected(self):
        """Connection refused is a code-level error (wrong port) — retries CAN fix it."""
        result = "Execution: FAILED\nStderr:\nConnection refused"
        assert _detect_environment_error(result) is None

    def test_code_error_not_detected(self):
        result = "Execution: FAILED (exit code 1)\nTraceback:\nZeroDivisionError: division by zero"
        assert _detect_environment_error(result) is None

    def test_import_error_not_detected(self):
        result = "Execution: FAILED\nTraceback:\nModuleNotFoundError: No module named 'pandas'"
        assert _detect_environment_error(result) is None

    def test_empty_result_not_detected(self):
        assert _detect_environment_error("") is None
        assert _detect_environment_error(None) is None

    def test_env_error_forces_max_retries(self):
        """Environment errors should set retry_count to MAX_RETRIES."""
        from brain.nodes.auditor import audit
        import config
        state = {
            "task_id": "test-env",
            "message": "test",
            "task_type": "code",
            "plan": "test plan",
            "code": "print('hello')",
            "execution_result": "Execution: FAILED\nStderr:\ncan't initialize sys standard streams",
            "retry_count": 0,
        }
        # This should NOT call the Claude API — it should short-circuit
        result = audit(state)
        assert result["retry_count"] >= config.MAX_RETRIES
        assert "ENVIRONMENT ERROR" in result["audit_feedback"]
