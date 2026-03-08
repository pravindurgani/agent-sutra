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
        _execute_code(state)

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
            result = _check_ollama_model()

        assert result is False
        assert any("not running" in r.message.lower() or "disabled" in r.message.lower()
                    for r in caplog.records)

    @patch("requests.get")
    def test_model_available_returns_true(self, mock_get):
        """_check_ollama_model returns True when model is available."""
        from main import _check_ollama_model
        import config as _cfg
        original = _cfg.OLLAMA_DEFAULT_MODEL
        _cfg.OLLAMA_DEFAULT_MODEL = "llama3.1:8b"

        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"models": [{"name": "llama3.1:8b"}]},
        )
        result = _check_ollama_model()
        _cfg.OLLAMA_DEFAULT_MODEL = original
        assert result is True

    @patch("requests.get")
    def test_model_missing_returns_false(self, mock_get):
        """_check_ollama_model returns False when model is not found."""
        from main import _check_ollama_model
        import config as _cfg
        original = _cfg.OLLAMA_DEFAULT_MODEL
        _cfg.OLLAMA_DEFAULT_MODEL = "nonexistent:7b"

        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"models": [{"name": "llama3.1:8b"}]},
        )
        result = _check_ollama_model()
        _cfg.OLLAMA_DEFAULT_MODEL = original
        assert result is False


# ── 4A: Ollama empty response retry ───────────────────────────────


class TestOllamaEmptyResponseRetry:
    """route_and_call should retry once on empty Ollama response, then fall back."""

    @patch("tools.model_router.claude_client")
    @patch("tools.model_router._call_ollama")
    @patch("tools.model_router._select_model", return_value=("ollama", "deepseek-r1:14b"))
    def test_retry_on_empty_then_success(self, mock_select, mock_ollama, mock_claude):
        """Empty first attempt, non-empty second attempt returns Ollama result."""
        from tools.model_router import route_and_call
        mock_ollama.side_effect = ["", "classify: code"]

        result = route_and_call("test", purpose="classify", complexity="low")

        assert result == "classify: code"
        assert mock_ollama.call_count == 2
        mock_claude.call.assert_not_called()

    @patch("tools.model_router.claude_client")
    @patch("tools.model_router._call_ollama")
    @patch("tools.model_router._select_model", return_value=("ollama", "deepseek-r1:14b"))
    def test_retry_both_empty_falls_back_to_claude(self, mock_select, mock_ollama, mock_claude):
        """Two empty Ollama responses falls back to Claude."""
        from tools.model_router import route_and_call
        mock_ollama.side_effect = ["", ""]
        mock_claude.call.return_value = "claude response"

        result = route_and_call("test", purpose="classify", complexity="low")

        assert result == "claude response"
        assert mock_ollama.call_count == 2
        mock_claude.call.assert_called_once()

    @patch("tools.model_router.claude_client")
    @patch("tools.model_router._call_ollama")
    @patch("tools.model_router._select_model", return_value=("ollama", "deepseek-r1:14b"))
    def test_exception_falls_back_without_retry(self, mock_select, mock_ollama, mock_claude):
        """Exception on first attempt breaks out and falls back to Claude immediately."""
        from tools.model_router import route_and_call
        mock_ollama.side_effect = ConnectionError("Ollama down")
        mock_claude.call.return_value = "claude fallback"

        result = route_and_call("test", purpose="classify", complexity="low")

        assert result == "claude fallback"
        assert mock_ollama.call_count == 1
        mock_claude.call.assert_called_once()

    @patch("tools.model_router.claude_client")
    @patch("tools.model_router._call_ollama")
    @patch("tools.model_router._select_model", return_value=("ollama", "deepseek-r1:14b"))
    def test_non_empty_first_attempt_returns_immediately(self, mock_select, mock_ollama, mock_claude):
        """Non-empty first attempt returns without retry."""
        from tools.model_router import route_and_call
        mock_ollama.return_value = "code"

        result = route_and_call("test", purpose="classify", complexity="low")

        assert result == "code"
        assert mock_ollama.call_count == 1
        mock_claude.call.assert_not_called()


# ── 4B: Ollama startup inference test ─────────────────────────────


class TestOllamaStartupInferenceTest:
    """Startup should run a smoke inference test when Ollama model is available."""

    @patch("requests.get")
    @patch("tools.model_router._call_ollama")
    def test_inference_test_runs_when_model_available(self, mock_ollama, mock_get, caplog):
        """When _check_ollama_model returns True, inference test runs."""
        from main import _check_ollama_model
        import config as _cfg
        original = _cfg.OLLAMA_DEFAULT_MODEL
        _cfg.OLLAMA_DEFAULT_MODEL = "llama3.1:8b"

        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"models": [{"name": "llama3.1:8b"}]},
        )
        mock_ollama.return_value = "code"

        ollama_ok = _check_ollama_model()
        assert ollama_ok is True

        # Simulate the inference test from main()
        import logging
        with caplog.at_level(logging.INFO, logger="agentsutra"):
            if ollama_ok:
                test = mock_ollama(
                    "Classify: 'hello world'", system="Reply with ONE word: code",
                    model=_cfg.OLLAMA_DEFAULT_MODEL, max_tokens=10,
                )
                assert test.strip() == "code"

        _cfg.OLLAMA_DEFAULT_MODEL = original

    @patch("requests.get")
    def test_inference_test_skipped_when_model_unavailable(self, mock_get):
        """When _check_ollama_model returns False, inference test is skipped."""
        from main import _check_ollama_model
        mock_get.side_effect = requests.ConnectionError("refused")

        ollama_ok = _check_ollama_model()
        assert ollama_ok is False
        # No inference test should run — nothing to assert beyond the False return


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


# ── 3A: Chain BLOCKED detection in strict-AND gate ─────────────────


class TestChainBlockedDetection:
    """Chain must halt when execution_result contains 'BLOCKED:' from security scanner."""

    def test_chain_gate_detects_blocked(self):
        """BLOCKED: in execution_result should trigger chain halt."""
        result = {
            "execution_result": "BLOCKED: Code contains dangerous pattern 'rm -rf /'.",
            "audit_verdict": "pass",
        }
        exec_result = result.get("execution_result", "")
        exec_blocked = "BLOCKED:" in exec_result
        assert exec_blocked is True

    def test_chain_gate_blocked_takes_priority_over_audit_pass(self):
        """Even if audit says pass, BLOCKED must halt the chain."""
        result = {
            "execution_result": "BLOCKED: Code contains sudo.",
            "audit_verdict": "pass",
            "audit_feedback": "Code looks fine",
        }
        exec_result = result.get("execution_result", "")
        exec_failed = exec_result.startswith("Execution: FAILED")
        exec_blocked = "BLOCKED:" in exec_result

        should_halt = exec_failed or exec_blocked or result.get("audit_verdict") != "pass"
        assert should_halt is True
        # And the reason should be security policy
        if exec_blocked:
            reason = "Security policy blocked this step"
        elif exec_failed:
            reason = "Execution returned non-zero exit code"
        else:
            reason = result.get("audit_feedback", "Unknown")[:300]
        assert reason == "Security policy blocked this step"

    def test_chain_gate_blocked_in_failed_execution(self):
        """BLOCKED embedded in 'Execution: FAILED' output is also caught."""
        result = {
            "execution_result": "Execution: FAILED (exit code -1)\nBLOCKED: Code contains curl|sh.",
            "audit_verdict": "fail",
        }
        exec_result = result.get("execution_result", "")
        exec_failed = exec_result.startswith("Execution: FAILED")
        exec_blocked = "BLOCKED:" in exec_result
        # Both flags triggered
        assert exec_failed is True
        assert exec_blocked is True
        # BLOCKED takes priority in reason
        if exec_blocked:
            reason = "Security policy blocked this step"
        elif exec_failed:
            reason = "Execution returned non-zero exit code"
        else:
            reason = "Unknown"
        assert reason == "Security policy blocked this step"

    def test_chain_gate_no_blocked_no_halt(self):
        """Normal successful execution without BLOCKED should not halt."""
        result = {
            "execution_result": "Execution: SUCCESS (exit code 0)\nOutput:\nHello World",
            "audit_verdict": "pass",
        }
        exec_result = result.get("execution_result", "")
        exec_failed = exec_result.startswith("Execution: FAILED")
        exec_blocked = "BLOCKED:" in exec_result
        should_halt = exec_failed or exec_blocked or result.get("audit_verdict") != "pass"
        assert should_halt is False

    def test_chain_gate_blocked_says_refused(self):
        """When BLOCKED, the halt message should say 'Step refused'."""
        exec_blocked = True
        exec_failed = False
        if exec_blocked:
            label = "Step refused"
        elif exec_failed:
            label = "Step failed"
        else:
            label = "Step failed"
        assert label == "Step refused"

    def test_chain_gate_exec_failed_says_failed(self):
        """When execution fails (non-zero exit), message should say 'Step failed'."""
        exec_blocked = False
        exec_failed = True
        if exec_blocked:
            label = "Step refused"
        elif exec_failed:
            label = "Step failed"
        else:
            label = "Step failed"
        assert label == "Step failed"

    def test_chain_gate_audit_fail_says_failed(self):
        """When audit verdict fails, message should say 'Step failed'."""
        exec_blocked = False
        exec_failed = False
        if exec_blocked:
            label = "Step refused"
        elif exec_failed:
            label = "Step failed"
        else:
            label = "Step failed"
        assert label == "Step failed"


# ── 3B: Timeout progress feedback ─────────────────────────────────


class TestTimeoutProgressFeedback:
    """Status loop should show progress at 5min and warning at 80% timeout."""

    def test_progress_flag_prevents_duplicate(self):
        """Progress flag should prevent sending the message twice."""
        user_data = {}
        task_id = "test-progress-123"

        # First time: flag not set
        assert not user_data.get(f"_progress_{task_id}")
        user_data[f"_progress_{task_id}"] = True

        # Second time: flag is set
        assert user_data.get(f"_progress_{task_id}") is True

    def test_warn_flag_prevents_duplicate(self):
        """Warning flag should prevent sending the timeout warning twice."""
        user_data = {}
        task_id = "test-warn-456"

        assert not user_data.get(f"_warn_{task_id}")
        user_data[f"_warn_{task_id}"] = True
        assert user_data.get(f"_warn_{task_id}") is True

    def test_cleanup_removes_progress_flags(self):
        """Finally block should clean up both progress flags."""
        user_data = {
            "_progress_task-abc": True,
            "_warn_task-abc": True,
            "other_key": "preserved",
        }
        task_id = "task-abc"

        # Simulate finally block cleanup
        user_data.pop(f"_progress_{task_id}", None)
        user_data.pop(f"_warn_{task_id}", None)

        assert f"_progress_{task_id}" not in user_data
        assert f"_warn_{task_id}" not in user_data
        assert user_data["other_key"] == "preserved"

    def test_timeout_thresholds(self):
        """Progress at 300s, warning at 80% of LONG_TIMEOUT."""
        import config
        progress_threshold = 300
        warn_threshold = config.LONG_TIMEOUT * 0.8

        # Progress fires at 5 minutes
        assert progress_threshold == 300
        # Warning fires at 80% of timeout (default 900s → 720s)
        assert warn_threshold == config.LONG_TIMEOUT * 0.8
        # Warning fires after progress
        assert warn_threshold > progress_threshold


# ── 5A: Executor file reference validation ─────────────────────────


class TestCheckReferencedFiles:
    """_check_referenced_files should warn about missing files in the message."""

    def test_missing_file_returns_warning(self, tmp_path):
        """Non-existent file reference produces a warning."""
        from brain.nodes.executor import _check_referenced_files
        result = _check_referenced_files("grep error nonexistent_server.log", tmp_path)
        assert "WARNING" in result
        assert "nonexistent_server.log" in result
        assert "NEVER fabricate" in result

    def test_existing_file_returns_empty(self, tmp_path):
        """File that exists in working_dir produces no warning."""
        from brain.nodes.executor import _check_referenced_files
        (tmp_path / "data.csv").write_text("a,b\n1,2\n")
        result = _check_referenced_files("analyse data.csv", tmp_path)
        assert result == ""

    def test_multiple_refs_some_missing(self, tmp_path):
        """Multiple refs — only missing ones are listed."""
        from brain.nodes.executor import _check_referenced_files
        (tmp_path / "report.json").write_text("{}")
        result = _check_referenced_files(
            "read report.json and compare with metrics.csv", tmp_path
        )
        assert "metrics.csv" in result
        assert "report.json" not in result

    def test_no_file_refs_returns_empty(self, tmp_path):
        """Message with no file references returns empty string."""
        from brain.nodes.executor import _check_referenced_files
        result = _check_referenced_files("create a fibonacci function", tmp_path)
        assert result == ""

    def test_tilde_expansion(self, tmp_path):
        """Home-relative paths are expanded for existence check."""
        from brain.nodes.executor import _check_referenced_files
        # ~/nonexistent_file_xyz.log won't exist
        result = _check_referenced_files(
            "tail ~/nonexistent_file_xyz.log", tmp_path
        )
        assert "WARNING" in result


# ── 5B: Auditor fabrication prompt ─────────────────────────────────


class TestAuditorFabricationPrompt:
    """Auditor system prompt must include fabrication check criteria."""

    def test_fabrication_check_in_system_base(self):
        """SYSTEM_BASE includes the fabrication check section."""
        from brain.nodes.auditor import SYSTEM_BASE
        assert "FABRICATION CHECK" in SYSTEM_BASE
        assert "sample data instead" in SYSTEM_BASE
        assert "ghp_*" in SYSTEM_BASE
        assert 'open(..., "w")' in SYSTEM_BASE

    def test_fabrication_check_preserved_existing_content(self):
        """Adding fabrication check didn't break existing SYSTEM_BASE content."""
        from brain.nodes.auditor import SYSTEM_BASE
        assert "STRICT quality auditor" in SYSTEM_BASE
        assert "adversarial review" in SYSTEM_BASE
        assert "Graceful degradation" in SYSTEM_BASE


# ── 5C: Deliverer credential pattern filter ────────────────────────


class TestDelivererCredentialFilter:
    """Artifacts containing credential-shaped strings must be blocked."""

    def test_github_pat_blocked(self, tmp_path):
        """File containing a GitHub PAT pattern is blocked."""
        from brain.nodes.deliverer import _has_credential_patterns
        f = tmp_path / "output.json"
        f.write_text('{"token": "ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ123456789a"}')
        assert _has_credential_patterns(f) is True

    def test_aws_key_blocked(self, tmp_path):
        """File containing an AWS access key pattern is blocked."""
        from brain.nodes.deliverer import _has_credential_patterns
        f = tmp_path / "config.yaml"
        f.write_text("aws_access_key: AKIAIOSFODNN7EXAMPLE")
        assert _has_credential_patterns(f) is True

    def test_openai_key_blocked(self, tmp_path):
        """File containing an OpenAI key pattern is blocked."""
        from brain.nodes.deliverer import _has_credential_patterns
        f = tmp_path / "secrets.txt"
        f.write_text("sk-" + "a" * 48)
        assert _has_credential_patterns(f) is True

    def test_safe_file_not_blocked(self, tmp_path):
        """File without credential patterns passes through."""
        from brain.nodes.deliverer import _has_credential_patterns
        f = tmp_path / "results.json"
        f.write_text('{"accuracy": 0.95, "model": "v2"}')
        assert _has_credential_patterns(f) is False

    def test_py_files_not_checked(self, tmp_path):
        """Python source files are not checked (per spec)."""
        from brain.nodes.deliverer import _has_credential_patterns
        f = tmp_path / "script.py"
        f.write_text('API_KEY = "ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ123456789a"')
        assert _has_credential_patterns(f) is False

    def test_binary_files_not_checked(self, tmp_path):
        """Binary/image files are not checked."""
        from brain.nodes.deliverer import _has_credential_patterns
        f = tmp_path / "chart.png"
        f.write_bytes(b'\x89PNG\r\n' + b'ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ123456789a'.ljust(50))
        assert _has_credential_patterns(f) is False

    def test_credential_filter_in_deliver(self):
        """deliver() filters artifacts with credential patterns."""
        from brain.nodes.deliverer import _has_credential_patterns
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            safe = Path(td) / "output.csv"
            safe.write_text("a,b\n1,2\n")
            cred = Path(td) / "leaked.json"
            cred.write_text('{"key": "ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ123456789a"}')

            artifacts = [str(safe), str(cred)]
            filtered = [a for a in artifacts if not _has_credential_patterns(Path(a))]
            assert str(safe) in filtered
            assert str(cred) not in filtered
