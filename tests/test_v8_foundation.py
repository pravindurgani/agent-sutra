"""Tests for AgentSutra v8 Phase 1: Foundation & Quick Wins.

Covers:
- Adaptive timeout detection in auditor._detect_environment_error()
- Security blocklist audit (verification that gaps are already covered)
- Coding standards injection into planner system prompts
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# ── Timeout detection tests ──────────────────────────────────────────


class TestAdaptiveTimeoutDetection:
    """Verify _detect_environment_error catches timeout patterns."""

    def test_timed_out_after_caught(self):
        """run_shell format: 'Timed out after 600s' → detected as env error."""
        from brain.nodes.auditor import _detect_environment_error
        result = _detect_environment_error("Timed out after 600s")
        assert result is not None
        assert "timed out" in result.lower() or "timeout" in result.lower()

    def test_execution_timed_out_caught(self):
        """run_code format: 'Execution timed out after 120s' → also caught.
        The substring 'Timed out after' is present in both sandbox formats."""
        from brain.nodes.auditor import _detect_environment_error
        result = _detect_environment_error("Execution timed out after 120s")
        assert result is not None

    def test_killed_process_group_caught(self):
        """Logger warning 'killed process group 12345' → detected."""
        from brain.nodes.auditor import _detect_environment_error
        result = _detect_environment_error("killed process group 12345")
        assert result is not None
        assert "timeout" in result.lower() or "killed" in result.lower()

    def test_import_error_not_caught(self):
        """ImportError IS retryable — auto-install can fix it."""
        from brain.nodes.auditor import _detect_environment_error
        result = _detect_environment_error("ImportError: No module named 'foo'")
        assert result is None

    def test_empty_string_not_caught(self):
        from brain.nodes.auditor import _detect_environment_error
        result = _detect_environment_error("")
        assert result is None

    def test_permission_denied_not_caught(self):
        """Permission denied is intentionally excluded — it's a code-level
        error that the retry loop CAN fix (e.g., wrong path or wrong port)."""
        from brain.nodes.auditor import _detect_environment_error
        result = _detect_environment_error("Permission denied")
        assert result is None

    def test_existing_patterns_still_work(self):
        """Verify the original 4 patterns weren't broken."""
        from brain.nodes.auditor import _detect_environment_error
        assert _detect_environment_error("can't initialize sys standard streams") is not None
        assert _detect_environment_error("Bad file descriptor") is not None
        assert _detect_environment_error("No space left on device") is not None
        assert _detect_environment_error("Name or service not known") is not None


# ── Blocklist audit tests ────────────────────────────────────────────


class TestBlocklistAudit:
    """Verify all 4 roadmap-identified gaps are already covered.

    The roadmap (Section 2.2) identified these potential gaps:
    - perl -E: Covered by character class [eE] in existing pattern (line 60)
    - xargs rm: Added in Round 4 hardening (line 83)
    - truncate: Added in Round 4 hardening (line 88)
    - python3 -c: Deliberately excluded from Tier 1 (see NOTE at line 58-59);
      logged via _LOGGED_PATTERNS instead
    """

    def test_perl_uppercase_E_blocked(self):
        """perl -E covered by [eE] character class in the existing pattern."""
        from tools.sandbox import _check_command_safety
        result = _check_command_safety("perl -E 'say 1'")
        assert result is not None, "perl -E should be blocked by [eE] class"

    def test_perl_lowercase_e_blocked(self):
        from tools.sandbox import _check_command_safety
        result = _check_command_safety("perl -e 'system(\"rm -rf /\")'")
        assert result is not None

    def test_xargs_rm_blocked(self):
        """xargs rm added in Round 4 hardening (commit 17df05f)."""
        from tools.sandbox import _check_command_safety
        result = _check_command_safety("echo foo | xargs rm")
        assert result is not None, "xargs rm should be blocked"

    def test_truncate_blocked(self):
        """truncate added in Round 4 hardening (commit 17df05f)."""
        from tools.sandbox import _check_command_safety
        result = _check_command_safety("truncate --size 0 /etc/passwd")
        assert result is not None, "truncate should be blocked"

    def test_python3_c_not_blocked(self):
        """python3 -c is INTENTIONALLY excluded from Tier 1.

        Rationale (from sandbox.py line 58-59 NOTE): python3 -c is a normal
        scripting pattern used by the agent itself. It remains in
        _LOGGED_PATTERNS (Tier 3) for audit trail, not in _BLOCKED_PATTERNS.
        """
        from tools.sandbox import _check_command_safety
        result = _check_command_safety("python3 -c 'print(1)'")
        assert result is None, "python3 -c should NOT be blocked (deliberate design decision)"

    def test_safe_command_allowed(self):
        from tools.sandbox import _check_command_safety
        result = _check_command_safety("pip3 install pandas")
        assert result is None


# ── Coding standards injection tests ─────────────────────────────────


class TestCodingStandardsInjection:
    """Verify standards.md is injected into planner prompts for code tasks."""

    def _make_state(self, task_type: str = "code", message: str = "test task") -> dict:
        return {
            "task_id": "test-123",
            "user_id": 1,
            "message": message,
            "task_type": task_type,
            "files": [],
            "conversation_context": "",
            "audit_feedback": "",
            "execution_result": "",
            "retry_count": 0,
        }

    @patch("brain.nodes.planner.claude_client.call")
    def test_standards_injected_for_code_task(self, mock_call):
        """A 'code' task should include standards in the system prompt."""
        mock_call.return_value = "1. Do the thing"

        # Create a temporary standards file
        import config
        standards_dir = config.BASE_DIR / ".agentsutra"
        standards_dir.mkdir(parents=True, exist_ok=True)
        standards_file = standards_dir / "standards.md"
        standards_file.write_text("Use pathlib, not os.path")

        try:
            from brain.nodes.planner import plan
            plan(self._make_state("code"))
            call_args = mock_call.call_args
            system_prompt = call_args.kwargs.get("system", "") or call_args[1].get("system", "")
            # Try positional if not in kwargs
            if not system_prompt and len(call_args.args) > 1:
                system_prompt = ""
            # Check the system kwarg
            for key, val in call_args.kwargs.items():
                if key == "system":
                    system_prompt = val
            assert "USER'S CODING STANDARDS" in system_prompt
            assert "pathlib" in system_prompt
        finally:
            if standards_file.exists():
                standards_file.unlink()

    @patch("brain.nodes.planner.claude_client.call")
    def test_standards_not_injected_for_project_task(self, mock_call):
        """A 'project' task should NOT include standards (runs existing commands)."""
        mock_call.return_value = "1. Run the command"

        import config
        standards_dir = config.BASE_DIR / ".agentsutra"
        standards_dir.mkdir(parents=True, exist_ok=True)
        standards_file = standards_dir / "standards.md"
        standards_file.write_text("Use pathlib, not os.path")

        try:
            from brain.nodes.planner import plan
            state = self._make_state("project")
            state["project_config"] = {"name": "Test", "path": "/tmp", "commands": {}}
            plan(state)
            call_args = mock_call.call_args
            system_prompt = ""
            for key, val in call_args.kwargs.items():
                if key == "system":
                    system_prompt = val
            assert "USER'S CODING STANDARDS" not in system_prompt
        finally:
            if standards_file.exists():
                standards_file.unlink()

    @patch("brain.nodes.planner.claude_client.call")
    def test_standards_truncated_at_2000_chars(self, mock_call):
        """Standards content longer than 2000 chars should be truncated."""
        mock_call.return_value = "1. Do the thing"

        import config
        standards_dir = config.BASE_DIR / ".agentsutra"
        standards_dir.mkdir(parents=True, exist_ok=True)
        standards_file = standards_dir / "standards.md"
        # Write a 5000-char file
        long_content = "x" * 5000
        standards_file.write_text(long_content)

        try:
            from brain.nodes.planner import plan
            plan(self._make_state("code"))
            call_args = mock_call.call_args
            system_prompt = ""
            for key, val in call_args.kwargs.items():
                if key == "system":
                    system_prompt = val
            # The injected portion should be at most 2000 chars of content
            # Find the injection point and verify length
            marker = "USER'S CODING STANDARDS (follow these strictly):\n"
            idx = system_prompt.find(marker)
            assert idx >= 0, "Standards marker not found"
            injected = system_prompt[idx + len(marker):]
            assert len(injected) <= 2000, f"Injected {len(injected)} chars, expected <= 2000"
        finally:
            if standards_file.exists():
                standards_file.unlink()

    @patch("brain.nodes.planner.claude_client.call")
    def test_standards_skipped_when_file_missing(self, mock_call):
        """No standards file → no injection, no error."""
        mock_call.return_value = "1. Do the thing"

        import config
        standards_file = config.BASE_DIR / ".agentsutra" / "standards.md"
        # Ensure file does NOT exist
        if standards_file.exists():
            standards_file.unlink()

        from brain.nodes.planner import plan
        plan(self._make_state("code"))
        call_args = mock_call.call_args
        system_prompt = ""
        for key, val in call_args.kwargs.items():
            if key == "system":
                system_prompt = val
        assert "USER'S CODING STANDARDS" not in system_prompt
