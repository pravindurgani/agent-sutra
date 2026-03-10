"""Tests for brain/nodes/auditor.py — JSON extraction, environment error detection, data sanity."""
from __future__ import annotations

import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import patch

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

    def test_audit_prompt_includes_fabrication_checks(self):
        """Auditor system prompt must include Phase 2 fabrication guards."""
        from brain.nodes.auditor import SYSTEM_BASE
        assert "fake/mock data" in SYSTEM_BASE.lower() or "sample/fake/mock" in SYSTEM_BASE.lower()
        assert "fabricates" in SYSTEM_BASE.lower() or "fabricate" in SYSTEM_BASE.lower()
        assert "xoxb_" in SYSTEM_BASE or "xoxb-" in SYSTEM_BASE

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


# ── v9.0.0 Phase 2: Data sanity checks in audit prompts ──────────


class TestAuditDataSanity:
    """Auditor prompts must instruct Opus to catch impossible data metrics."""

    def _make_data_state(self, execution_result: str) -> dict:
        return {
            "task_id": "data-sanity-test",
            "message": "Analyse campaign performance data",
            "task_type": "data",
            "plan": "Read CSV, compute CTR, generate report",
            "code": "import pandas as pd\ndf = pd.read_csv('data.csv')\nprint(df)",
            "execution_result": execution_result,
            "retry_count": 0,
            "audit_feedback": "",
        }

    @patch("brain.nodes.auditor.claude_client.call")
    def test_audit_catches_impossible_ctr(self, mock_call):
        """0 impressions + 91499 clicks + 11600% CTR → verdict=fail."""
        mock_call.return_value = json.dumps({
            "verdict": "fail",
            "feedback": "Data anomaly detected: 0 impressions with 91499 clicks and 11600% CTR is mathematically impossible.",
        })
        from brain.nodes.auditor import audit
        state = self._make_data_state(
            "Execution: OK (exit code 0)\nStdout:\n"
            "Campaign: Tugi Tark\n"
            "Impressions: 0\nClicks: 91499\nCTR: 11600%\n"
            "ALL ASSERTIONS PASSED"
        )
        result = audit(state)
        assert result["audit_verdict"] == "fail"

    @patch("brain.nodes.auditor.claude_client.call")
    def test_audit_passes_valid_data(self, mock_call):
        """10000 impressions + 500 clicks + 5.0% CTR → verdict=pass."""
        mock_call.return_value = json.dumps({
            "verdict": "pass",
            "feedback": "Data metrics are consistent. CTR of 5.0% is valid (500/10000).",
        })
        from brain.nodes.auditor import audit
        state = self._make_data_state(
            "Execution: OK (exit code 0)\nStdout:\n"
            "Campaign: Test Campaign\n"
            "Impressions: 10000\nClicks: 500\nCTR: 5.0%\n"
            "ALL ASSERTIONS PASSED"
        )
        result = audit(state)
        assert result["audit_verdict"] == "pass"

    @patch("brain.nodes.auditor.claude_client.call")
    def test_audit_catches_zero_denominator_rate(self, mock_call):
        """0 views + 850% engagement → verdict=fail."""
        mock_call.return_value = json.dumps({
            "verdict": "fail",
            "feedback": "Data anomaly: engagement rate of 850% with 0 views indicates a zero-denominator artifact.",
        })
        from brain.nodes.auditor import audit
        state = self._make_data_state(
            "Execution: OK (exit code 0)\nStdout:\n"
            "Views: 0\nEngagement Rate: 850%\n"
            "ALL ASSERTIONS PASSED"
        )
        result = audit(state)
        assert result["audit_verdict"] == "fail"

    def test_system_prompt_includes_data_sanity_language(self):
        """SYSTEM_BASE must contain data sanity instructions for Opus."""
        from brain.nodes.auditor import SYSTEM_BASE
        assert "DATA SANITY CHECK" in SYSTEM_BASE
        assert "impressions = 0 but clicks > 0" in SYSTEM_BASE
        assert "1000%" in SYSTEM_BASE

    def test_data_criteria_includes_sanity_check(self):
        """AUDIT_CRITERIA["data"] must reference data sanity validation."""
        from brain.nodes.auditor import AUDIT_CRITERIA
        data_criteria = AUDIT_CRITERIA["data"]
        assert "DATA INTEGRITY" in data_criteria
        assert "mathematically impossible" in data_criteria
        assert "sample data substituted" in data_criteria


# ── v9.0.0 Phase 7: Audit criteria expansion ─────────────────────


class TestAuditCriteriaExpansion:
    """Expanded AUDIT_CRITERIA for frontend, data, and project tasks."""

    @patch("brain.nodes.auditor.claude_client.call")
    def test_frontend_audit_detects_truncated_html(self, mock_call):
        """Frontend HTML missing </html> → verdict=fail."""
        mock_call.return_value = json.dumps({
            "verdict": "fail",
            "feedback": "Code appears truncated: </html> is missing at the end of the file.",
        })
        from brain.nodes.auditor import audit
        state = {
            "task_id": "frontend-trunc-test",
            "message": "Build a React dashboard",
            "task_type": "frontend",
            "plan": "Create single-page React app with Tailwind",
            "code": '<!DOCTYPE html><html><head></head><body><div id="root"></div><script>',
            "execution_result": "Execution: OK (exit code 0)\nStdout:\nServer started",
            "retry_count": 0,
            "audit_feedback": "",
        }
        result = audit(state)
        assert result["audit_verdict"] == "fail"

    @patch("brain.nodes.auditor.claude_client.call")
    def test_data_audit_detects_impossible_rate(self, mock_call):
        """Data with CTR: 11600% → verdict=fail."""
        mock_call.return_value = json.dumps({
            "verdict": "fail",
            "feedback": "Data integrity failure: CTR of 11600% with 0 impressions is mathematically impossible.",
        })
        from brain.nodes.auditor import audit
        state = {
            "task_id": "data-rate-test",
            "message": "Analyse campaign performance",
            "task_type": "data",
            "plan": "Read CSV, compute metrics",
            "code": "import pandas as pd\ndf = pd.read_csv('data.csv')",
            "execution_result": (
                "Execution: OK (exit code 0)\nStdout:\n"
                "Impressions: 0\nClicks: 91499\nCTR: 11600%"
            ),
            "retry_count": 0,
            "audit_feedback": "",
        }
        result = audit(state)
        assert result["audit_verdict"] == "fail"

    @patch("brain.nodes.auditor.claude_client.call")
    def test_project_audit_checks_correct_arguments(self, mock_call):
        """Project with wrong pipeline args → verdict=fail."""
        mock_call.return_value = json.dumps({
            "verdict": "fail",
            "feedback": "Wrong arguments: command used '--client Acme' but task specified 'Light & Wonder'.",
        })
        from brain.nodes.auditor import audit
        state = {
            "task_id": "project-args-test",
            "message": "Run the report for Light & Wonder",
            "task_type": "project",
            "plan": "Execute report pipeline with client=Light & Wonder",
            "code": "python run.py --client Acme",
            "execution_result": (
                "Execution: OK (exit code 0)\nStdout:\n"
                "Report generated for Acme Corp"
            ),
            "extracted_params": {"client": "Light & Wonder"},
            "retry_count": 0,
            "audit_feedback": "",
        }
        result = audit(state)
        assert result["audit_verdict"] == "fail"

    def test_frontend_criteria_includes_truncation_check(self):
        """AUDIT_CRITERIA["frontend"] must check for HTML completeness."""
        from brain.nodes.auditor import AUDIT_CRITERIA
        frontend = AUDIT_CRITERIA["frontend"]
        assert "</html>" in frontend
        assert "truncated" in frontend.lower()

    def test_project_criteria_includes_run_instructions(self):
        """AUDIT_CRITERIA["project"] must check run_instructions compliance."""
        from brain.nodes.auditor import AUDIT_CRITERIA
        project = AUDIT_CRITERIA["project"]
        assert "run_instructions" in project
        assert "Do NOT look for" in project
        assert "ALL ASSERTIONS PASSED" in project
