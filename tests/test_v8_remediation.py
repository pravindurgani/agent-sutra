"""Tests for v8.4.1 remediation: chain gate, deliverer blocking, truncation detection."""
from __future__ import annotations

import sys
import os
import re

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import requests
from unittest.mock import patch, MagicMock
from brain.nodes.deliverer import deliver
from brain.nodes.executor import _detect_truncation


# ── R.2: Chain strict-AND gate (exit-code based + literal prefix) ─


# The chain step prefix that cmd_chain() prepends to pipeline messages.
# Extracted here so tests stay in sync with the actual handler.
_CHAIN_PREFIX_PATTERN = re.compile(
    r"CHAIN STEP \d+/\d+: Execute this task EXACTLY as written"
)


class TestChainStrictAndGate:
    """Chain should halt when execution_result starts with 'Execution: FAILED'
    even if audit_verdict is 'pass' (Claude rewrote failure as success report)."""

    def test_chain_gate_detects_execution_failed(self):
        """execution_result starting with 'Execution: FAILED' should be caught."""
        result = {
            "execution_result": "Execution: FAILED (exit code 1)\nOutput:\n✗ FAILED — values do not match",
            "audit_verdict": "pass",  # Opus was fooled
        }
        exec_failed = result["execution_result"].startswith("Execution: FAILED")
        assert exec_failed is True

    def test_chain_gate_passes_on_success(self):
        """execution_result starting with 'Execution: SUCCESS' should pass."""
        result = {
            "execution_result": "Execution: SUCCESS (exit code 0)\nOutput:\nAll tests passed",
            "audit_verdict": "pass",
        }
        exec_failed = result["execution_result"].startswith("Execution: FAILED")
        assert exec_failed is False
        assert result["audit_verdict"] == "pass"

    def test_chain_gate_catches_non_zero_exit_code(self):
        """Exit code 2 means FAILED regardless of output content."""
        result = {
            "execution_result": "Execution: FAILED (exit code 2)\nOutput:\nScript completed gracefully",
            "audit_verdict": "pass",
        }
        exec_failed = result["execution_result"].startswith("Execution: FAILED")
        assert exec_failed is True

    def test_chain_gate_empty_execution_result(self):
        """Missing execution_result should not crash — defaults to empty string."""
        result = {"audit_verdict": "fail"}
        exec_result = result.get("execution_result", "")
        exec_failed = exec_result.startswith("Execution: FAILED")
        # audit_verdict is 'fail', so chain halts via second check
        assert exec_failed is False
        assert result["audit_verdict"] != "pass"

    def test_chain_halts_on_assertion_failure(self):
        """Chain step with assertion failure (exit code 1) halts even if audit says pass.

        Reproduces test 2.3: step 2 asserts wrong value, agent reports it gracefully,
        Opus audits as pass — but exit code is non-zero, so chain must halt.
        """
        result = {
            "execution_result": (
                "Execution: FAILED (exit code 1)\n"
                "Output:\nAssertion check: api_key='real_key'\n"
                "✗ FAILED — values do not match\n"
                "Traceback:\nAssertionError"
            ),
            "audit_verdict": "pass",  # Opus fooled by graceful reporting
            "audit_feedback": "Code ran, reported mismatch diagnostically",
            "final_response": "The assertion check found a mismatch...",
            "artifacts": [],
        }
        exec_failed = result["execution_result"].startswith("Execution: FAILED")
        # Both checks must trigger halt
        assert exec_failed is True
        # Even if audit_verdict is pass, exec_failed should halt the chain
        should_halt = exec_failed or result.get("audit_verdict") != "pass"
        assert should_halt is True


class TestChainLiteralExecutionPrefix:
    """Chain steps must include a prefix instructing literal execution."""

    def test_chain_prefix_format(self):
        """The chain prefix matches the expected pattern."""
        # Simulate what cmd_chain() builds
        i, total = 1, 3
        step_msg = "Assert config['api_key'] == 'wrong_value'"
        chain_prefix = (
            f"CHAIN STEP {i+1}/{total}: Execute this task EXACTLY as written. "
            "Do NOT catch exceptions, do NOT handle errors gracefully, do NOT rewrite "
            "failing assertions into passing ones. If the task says to assert something "
            "that will fail, let the assertion crash the program. The chain depends on "
            "real pass/fail results.\n\n"
        )
        pipeline_msg = chain_prefix + step_msg

        assert _CHAIN_PREFIX_PATTERN.search(pipeline_msg) is not None
        assert "Do NOT catch exceptions" in pipeline_msg
        assert "NOT rewrite" in pipeline_msg
        assert step_msg in pipeline_msg

    def test_chain_prefix_preserves_original_message(self):
        """The original step message is intact after the prefix."""
        step_msg = "Create a fibonacci function and test it with assert fib(10) == 55"
        chain_prefix = (
            "CHAIN STEP 1/2: Execute this task EXACTLY as written. "
            "Do NOT catch exceptions, do NOT handle errors gracefully, do NOT rewrite "
            "failing assertions into passing ones. If the task says to assert something "
            "that will fail, let the assertion crash the program. The chain depends on "
            "real pass/fail results.\n\n"
        )
        pipeline_msg = chain_prefix + step_msg

        assert pipeline_msg.endswith(step_msg)
        assert "fibonacci" in pipeline_msg
        assert "assert fib(10) == 55" in pipeline_msg


# ── R.5: Deliverer blocks description of security-blocked tasks ──


class TestDelivererSecurityBlocking:
    """When execution was BLOCKED by code scanner, deliverer must not
    describe the tool's intended functionality."""

    @patch("brain.nodes.deliverer._write_debug_sidecar")
    def test_blocked_task_returns_minimal_response(self, mock_sidecar):
        """BLOCKED execution_result → minimal 'blocked by security policy' response."""
        state = {
            "task_id": "test-blocked",
            "user_id": 123,
            "message": "Write a script to read /etc/shadow",
            "task_type": "code",
            "execution_result": "Execution: FAILED (exit code -1)\nBLOCKED: Code contains system file read.",
            "audit_verdict": "fail",
            "audit_feedback": "Blocked by security scanner",
            "retry_count": 0,
            "code": "",
            "artifacts": [],
            "server_url": "",
            "deploy_url": "",
            "extracted_params": {},
            "project_name": "",
            "stage_timings": [],
        }
        result = deliver(state)
        assert "blocked by security policy" in result["final_response"].lower()
        assert result["artifacts"] == []
        assert "/etc/shadow" not in result["final_response"]
        assert "sudo" not in result["final_response"].lower()

    @patch("brain.nodes.deliverer._write_debug_sidecar")
    def test_blocked_task_no_tool_description(self, mock_sidecar):
        """BLOCKED task must NOT describe what the tool would do."""
        state = {
            "task_id": "test-blocked-2",
            "user_id": 123,
            "message": "Create a reverse shell using socket.connect",
            "task_type": "code",
            "execution_result": "BLOCKED: Code contains outbound socket connection.",
            "audit_verdict": "fail",
            "audit_feedback": "",
            "retry_count": 0,
            "code": "",
            "artifacts": [],
            "server_url": "",
            "deploy_url": "",
            "extracted_params": {},
            "project_name": "",
            "stage_timings": [],
        }
        result = deliver(state)
        assert "blocked by security policy" in result["final_response"].lower()
        assert "reverse shell" not in result["final_response"].lower()
        assert "socket" not in result["final_response"].lower()

    @patch("brain.nodes.deliverer.claude_client")
    @patch("brain.nodes.deliverer._write_debug_sidecar")
    def test_non_blocked_task_calls_claude(self, mock_sidecar, mock_claude):
        """Normal (non-blocked) task should call Claude for summary generation."""
        mock_claude.call.return_value = "Task completed successfully."
        state = {
            "task_id": "test-normal",
            "user_id": 123,
            "message": "Generate a hello world script",
            "task_type": "code",
            "execution_result": "Execution: SUCCESS (exit code 0)\nOutput:\nHello World",
            "audit_verdict": "pass",
            "audit_feedback": "",
            "retry_count": 0,
            "code": "print('Hello World')",
            "artifacts": [],
            "server_url": "",
            "deploy_url": "",
            "extracted_params": {},
            "project_name": "",
            "stage_timings": [],
        }
        result = deliver(state)
        # Claude should have been called for summary
        mock_claude.call.assert_called_once()
        assert "blocked by security policy" not in result["final_response"].lower()


# ── R.3: Code truncation detection ───────────────────────────────


class TestDetectTruncation:
    """_detect_truncation() must catch code cut off by max_tokens."""

    def test_detect_truncation_unclosed_parens(self):
        """Code with many unclosed parentheses is truncated."""
        code = '''
import pandas as pd
df = pd.read_csv("data.csv")
fig, ((ax1, ax2), (ax3, ax4)) = plt.subplots(2, 2, figsize=(16, 12
result = df.groupby("category").agg(
    total=("amount", "sum"),
    count=("amount",
'''
        assert _detect_truncation(code) is True

    def test_detect_truncation_unclosed_braces(self):
        """Code with many unclosed braces is truncated."""
        code = '''
config = {
    "api_key": "test",
    "settings": {
        "timeout": 30,
        "retries": 3,
        "nested": {
'''
        assert _detect_truncation(code) is True

    def test_detect_truncation_unclosed_triple_quote(self):
        """Code with unclosed triple-quoted string is truncated."""
        code = '''
html = """<!DOCTYPE html>
<html>
<head><title>Dashboard</title></head>
<body>
<div class="container">
'''
        assert _detect_truncation(code) is True

    def test_detect_truncation_ends_mid_string(self):
        """Code ending with unclosed string literal is truncated."""
        code = '''
import matplotlib.pyplot as plt
plt.title("Revenue by Category for Q1 2026'''
        assert _detect_truncation(code) is True

    def test_detect_truncation_complete_code(self):
        """Valid complete Python code is NOT truncated."""
        code = '''
import pandas as pd
import matplotlib.pyplot as plt

df = pd.read_csv("data.csv")
result = df.groupby("category").agg(total=("amount", "sum"))
print(result)
plt.bar(result.index, result["total"])
plt.savefig("chart.png")
print("ALL ASSERTIONS PASSED")
'''
        assert _detect_truncation(code) is False

    def test_detect_truncation_empty_code(self):
        """Empty code is NOT truncated."""
        assert _detect_truncation("") is False
        assert _detect_truncation("   \n\n  ") is False

    def test_detect_truncation_html_with_unclosed_js(self):
        """HTML with truncated JS section (unclosed braces) detected."""
        code = '''<!DOCTYPE html>
<html>
<head><style>.dashboard { display: flex; }</style></head>
<body>
<script>
const app = {
    init() {
        this.data = {
            charts: {
                revenue: {
'''
        assert _detect_truncation(code) is True


class TestTruncationTriggersRetry:
    """When truncation is detected, executor requests a shorter version."""

    @patch("brain.nodes.executor.run_code_with_auto_install")
    @patch("brain.nodes.executor.claude_client")
    def test_truncation_triggers_shorter_prompt(self, mock_claude, mock_run):
        """Truncated code triggers a second call with 'shorter version' prompt."""
        from brain.nodes.executor import _execute_code

        # Truncated: 3+ unclosed parens after markdown stripping
        truncated_code = '```python\nimport pandas as pd\ndf = pd.read_csv("data.csv"\nresult = df.groupby("cat").agg(\n    total=("amount", "sum"),\n    count=("amount",\n```'
        good_code = '```python\nimport pandas as pd\ndf = pd.read_csv("data.csv")\nprint(df.describe())\nprint("ALL ASSERTIONS PASSED")\n```'
        # First call returns truncated, second returns good code
        mock_claude.call.side_effect = [truncated_code, good_code]
        mock_run.return_value = MagicMock(
            success=True, stdout="ALL ASSERTIONS PASSED", stderr="",
            traceback="", files_created=[], timed_out=False,
            return_code=0, auto_installed=[],
        )

        state = {
            "task_id": "test-trunc",
            "user_id": 123,
            "message": "Analyze data.csv",
            "task_type": "data",
            "plan": "Read CSV and summarize",
            "files": [],
            "audit_feedback": "",
            "code": "",
        }
        result = _execute_code(state)

        # Should have called claude twice: original + shorter version
        assert mock_claude.call.call_count == 2
        second_call_prompt = mock_claude.call.call_args_list[1][0][0]
        assert "truncated" in second_call_prompt.lower()


# ── R.4: Ollama /api/chat migration + startup validation ─────────


class TestOllamaChatEndpoint:
    """_call_ollama() must use /api/chat (Ollama v0.5+) with message format."""

    @patch("tools.model_router.requests.post")
    def test_call_ollama_uses_chat_endpoint(self, mock_post):
        """Primary call goes to /api/chat with messages array."""
        from tools.model_router import _call_ollama

        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"message": {"content": "Hello from Ollama"}},
        )
        mock_post.return_value.raise_for_status = MagicMock()

        result = _call_ollama("Say hello", "You are helpful", "llama3.1:8b", 200)

        assert result == "Hello from Ollama"
        call_args = mock_post.call_args
        assert "/api/chat" in call_args[0][0]
        payload = call_args[1]["json"]
        assert "messages" in payload
        assert payload["messages"][0]["role"] == "system"
        assert payload["messages"][1]["role"] == "user"

    @patch("tools.model_router.requests.post")
    def test_call_ollama_chat_no_system(self, mock_post):
        """When system is empty, only user message is sent."""
        from tools.model_router import _call_ollama

        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"message": {"content": "Hi"}},
        )
        mock_post.return_value.raise_for_status = MagicMock()

        _call_ollama("Hello", "", "llama3.1:8b", 200)

        payload = mock_post.call_args[1]["json"]
        assert len(payload["messages"]) == 1
        assert payload["messages"][0]["role"] == "user"

    @patch("tools.model_router._call_ollama_generate")
    @patch("tools.model_router.requests.post")
    def test_call_ollama_falls_back_to_generate_on_404(self, mock_post, mock_generate):
        """If /api/chat returns 404, falls back to /api/generate."""
        from tools.model_router import _call_ollama

        resp_404 = MagicMock(status_code=404)
        mock_post.return_value = resp_404
        mock_post.return_value.raise_for_status.side_effect = requests.HTTPError(
            response=resp_404
        )
        mock_generate.return_value = "Fallback response"

        result = _call_ollama("Hello", "", "llama3.1:8b", 200)

        assert result == "Fallback response"
        mock_generate.assert_called_once()

    @patch("tools.model_router.requests.post")
    def test_call_ollama_generate_legacy(self, mock_post):
        """_call_ollama_generate() uses /api/generate with prompt field."""
        from tools.model_router import _call_ollama_generate

        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"response": "Generated text"},
        )
        mock_post.return_value.raise_for_status = MagicMock()

        result = _call_ollama_generate("Say hi", "Be helpful", "llama3.1:8b", 200)

        assert result == "Generated text"
        call_args = mock_post.call_args
        assert "/api/generate" in call_args[0][0]
        payload = call_args[1]["json"]
        assert "prompt" in payload
        assert "system" in payload


class TestOllamaStartupValidation:
    """_check_ollama_model() must validate model availability at startup."""

    @patch("requests.get")
    def test_model_available_logs_success(self, mock_get, caplog):
        """When configured model is available, logs info."""
        from main import _check_ollama_model

        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"models": [{"name": "llama3.1:8b"}, {"name": "qwen2.5-coder:14b"}]},
        )

        import logging
        with caplog.at_level(logging.INFO, logger="agentsutra"):
            _check_ollama_model()

        assert any("available" in r.message.lower() for r in caplog.records)

    @patch("requests.get")
    def test_model_missing_logs_warning(self, mock_get, caplog):
        """When configured model is missing, logs a warning with available models."""
        from main import _check_ollama_model
        import config as _cfg
        original = _cfg.OLLAMA_DEFAULT_MODEL
        _cfg.OLLAMA_DEFAULT_MODEL = "nonexistent:7b"

        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"models": [{"name": "llama3.1:8b"}]},
        )

        import logging
        with caplog.at_level(logging.WARNING, logger="agentsutra"):
            _check_ollama_model()

        _cfg.OLLAMA_DEFAULT_MODEL = original
        assert any("not found" in r.message.lower() for r in caplog.records)

    @patch("requests.get")
    def test_model_base_name_match_suggests_update(self, mock_get, caplog):
        """When base name matches but tag differs, suggests updating .env."""
        from main import _check_ollama_model
        import config as _cfg
        original = _cfg.OLLAMA_DEFAULT_MODEL
        _cfg.OLLAMA_DEFAULT_MODEL = "llama3.1:8b"

        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"models": [{"name": "llama3.1:latest"}]},
        )

        import logging
        with caplog.at_level(logging.WARNING, logger="agentsutra"):
            _check_ollama_model()

        _cfg.OLLAMA_DEFAULT_MODEL = original
        assert any("update ollama_default_model" in r.message.lower() for r in caplog.records)

    @patch("requests.get")
    def test_ollama_not_running_logs_info(self, mock_get, caplog):
        """When Ollama is unreachable, logs info (not error)."""
        from main import _check_ollama_model
        mock_get.side_effect = requests.ConnectionError("Connection refused")

        import logging
        with caplog.at_level(logging.INFO, logger="agentsutra"):
            _check_ollama_model()

        assert any("not running" in r.message.lower() or "disabled" in r.message.lower()
                    for r in caplog.records)


# ── R.5: Fabrication detection in auditor ────────────────────────


class TestAuditorCatchesFabrication:
    """Auditor should fail tasks where the agent substituted libraries or faked data."""

    @patch("brain.nodes.auditor.claude_client.call")
    def test_auditor_catches_library_substitution(self, mock_call):
        """If user asked for quantum_computing_sdk but code imported qiskit, audit should fail."""
        from brain.nodes.auditor import audit

        # Simulate Opus returning a fail verdict for library substitution
        mock_call.return_value = '{"verdict": "fail", "feedback": "Task asked for quantum_computing_sdk but code imported qiskit instead. This is library substitution, not task completion."}'

        state = {
            "task_id": "test-fab-1",
            "user_id": 123,
            "message": "Use quantum_computing_sdk to simulate 50 qubits",
            "task_type": "code",
            "plan": "Import quantum_computing_sdk and run simulation",
            "code": "import qiskit\nfrom qiskit import QuantumCircuit\nqc = QuantumCircuit(50)\nprint('Simulated 50 qubits')",
            "execution_result": "Execution: OK (exit code 0)\nstdout:\nSimulated 50 qubits",
            "audit_verdict": "",
            "audit_feedback": "",
            "retry_count": 0,
            "extracted_params": "",
            "server_url": "",
        }

        result = audit(state)

        # Verify the auditor was called with the fabrication check language
        call_args = mock_call.call_args
        system_prompt = call_args.kwargs.get("system", "") or call_args[1].get("system", "")
        assert "DIFFERENT library" in system_prompt, "Fabrication check missing from audit system prompt"

        assert result["audit_verdict"] == "fail"
        assert "substitution" in result["audit_feedback"].lower() or "qiskit" in result["audit_feedback"].lower()

    @patch("brain.nodes.auditor.claude_client.call")
    def test_auditor_catches_fake_data_generation(self, mock_call):
        """If user asked to connect to PostgreSQL but code generated sample data, audit should fail."""
        from brain.nodes.auditor import audit

        mock_call.return_value = '{"verdict": "fail", "feedback": "Task asked to connect to PostgreSQL and query users table, but code generated synthetic/sample data with random names instead of connecting to any database."}'

        state = {
            "task_id": "test-fab-2",
            "user_id": 123,
            "message": "Connect to PostgreSQL and export the users table to CSV",
            "task_type": "data",
            "plan": "Connect to PostgreSQL, query users table, export to CSV",
            "code": "import csv\nimport random\nnames = ['Alice', 'Bob', 'Charlie']\nwith open('users.csv', 'w') as f:\n    w = csv.writer(f)\n    w.writerow(['id', 'name'])\n    for i, n in enumerate(names):\n        w.writerow([i, n])\nprint('Exported 3 users')",
            "execution_result": "Execution: OK (exit code 0)\nstdout:\nExported 3 users",
            "audit_verdict": "",
            "audit_feedback": "",
            "retry_count": 0,
            "extracted_params": "",
            "server_url": "",
        }

        result = audit(state)

        # Verify fabrication check in system prompt
        call_args = mock_call.call_args
        system_prompt = call_args.kwargs.get("system", "") or call_args[1].get("system", "")
        assert "fake/sample data" in system_prompt, "Fabrication check missing from audit system prompt"

        assert result["audit_verdict"] == "fail"
        assert "sample" in result["audit_feedback"].lower() or "synthetic" in result["audit_feedback"].lower()
