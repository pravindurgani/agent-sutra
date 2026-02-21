"""Integration tests — full classify→plan→execute→audit→deliver pipeline with mocked Claude.

Chains the 5 node functions directly to avoid requiring langgraph at test time.
Each node reads from and writes to a shared state dict, exactly as the graph does.
"""
from __future__ import annotations

import sys
import os
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config
from brain.nodes.classifier import classify
from brain.nodes.planner import plan
from brain.nodes.executor import execute
from brain.nodes.auditor import audit
from brain.nodes.deliverer import deliver


def _initial_state(task_id="test-001", message="Write a script that prints hello world"):
    """Build a blank AgentState dict."""
    return {
        "task_id": task_id,
        "user_id": 1,
        "message": message,
        "files": [],
        "task_type": "",
        "project_name": "",
        "project_config": {},
        "plan": "",
        "code": "",
        "execution_result": "",
        "audit_verdict": "",
        "audit_feedback": "",
        "retry_count": 0,
        "stage": "",
        "final_response": "",
        "artifacts": [],
        "extracted_params": {},
        "working_dir": "",
        "conversation_context": "",
        "auto_installed_packages": [],
    }


def _run_pipeline(state, mock, max_retries=None):
    """Simulate the graph: classify → plan → execute → audit → (retry or deliver)."""
    retries = max_retries if max_retries is not None else config.MAX_RETRIES

    with patch("tools.claude_client.call", mock):
        state.update(classify(state))
        state.update(plan(state))
        state.update(execute(state))
        state.update(audit(state))

        while state.get("audit_verdict") != "pass" and state.get("retry_count", 0) < retries:
            state.update(plan(state))
            state.update(execute(state))
            state.update(audit(state))

        state.update(deliver(state))

    return state


class MockClaude:
    """Mock for tools.claude_client.call that returns stage-appropriate responses.

    Identifies which pipeline stage is calling based on the call signature:
      - Classifier:  max_tokens=200
      - Planner:     max_tokens=3000
      - Executor:    max_tokens=8192
      - Auditor:     max_tokens=800, temperature=0.0
      - Deliverer:   temperature=0.3
    """

    def __init__(self, audit_responses=None):
        self.calls = []
        self.audit_call_count = 0
        self.audit_responses = audit_responses or [
            '{"verdict": "pass", "feedback": "Code runs correctly and all assertions pass."}'
        ]

    def __call__(self, prompt, system="", model=None, max_tokens=4096,
                 temperature=0.0, thinking=False):
        self.calls.append({
            "max_tokens": max_tokens,
            "temperature": temperature,
            "model": model,
        })

        # Classifier
        if max_tokens == 200:
            return '{"task_type": "code", "reason": "Simple code task"}'

        # Planner
        if max_tokens == 3000:
            return (
                "1. Write a Python script that prints hello world\n"
                "2. Add assertions to verify output\n"
                "3. Print ALL ASSERTIONS PASSED at the end"
            )

        # Executor (code generation)
        if max_tokens == 8192:
            return (
                '```python\n'
                'result = "hello world"\n'
                'print(result)\n'
                'assert len(result) > 0, "Result should not be empty"\n'
                'assert "hello" in result\n'
                'print("ALL ASSERTIONS PASSED")\n'
                '```'
            )

        # Auditor (max_tokens=800 + temperature=0.0)
        if max_tokens == 800 and temperature == 0.0:
            idx = min(self.audit_call_count, len(self.audit_responses) - 1)
            response = self.audit_responses[idx]
            self.audit_call_count += 1
            return response

        # Deliverer (temperature=0.3)
        if temperature == 0.3:
            return "Task completed successfully. The script prints hello world and all assertions passed."

        return "Mock response"


class TestPipelineSuccess:
    """Happy path: all stages succeed, audit passes on first try."""

    def test_full_pipeline_pass(self):
        mock = MockClaude()
        state = _initial_state()
        result = _run_pipeline(state, mock)

        assert result["task_type"] == "code"
        assert result["audit_verdict"] == "pass"
        assert result["retry_count"] == 0
        assert result["final_response"]  # non-empty delivery
        assert result["plan"]  # planner produced a plan
        assert result["code"]  # executor produced code
        assert "hello" in result["code"]

        # 5 Claude calls: classify, plan, execute, audit, deliver
        assert len(mock.calls) == 5

    def test_execution_output_present(self):
        mock = MockClaude()
        state = _initial_state(task_id="test-002", message="Print hello world")
        result = _run_pipeline(state, mock)

        assert "hello world" in result["execution_result"]
        assert "ALL ASSERTIONS PASSED" in result["execution_result"]

    def test_working_dir_populated(self):
        """Bug #3: executor must return working_dir so handler can persist it."""
        mock = MockClaude()
        state = _initial_state(task_id="test-wd-001")
        result = _run_pipeline(state, mock)

        assert result.get("working_dir"), "working_dir should be non-empty after execution"


class TestPipelineRetry:
    """Audit fails on first attempt, succeeds on retry."""

    def test_retry_then_pass(self):
        mock = MockClaude(audit_responses=[
            '{"verdict": "fail", "feedback": "Missing output validation."}',
            '{"verdict": "pass", "feedback": "All checks pass now."}',
        ])
        state = _initial_state(task_id="test-003")
        result = _run_pipeline(state, mock)

        assert result["audit_verdict"] == "pass"
        assert result["retry_count"] >= 1
        assert result["final_response"]

        # 8 calls: classify, plan, execute, audit(fail), plan, execute, audit(pass), deliver
        assert len(mock.calls) == 8
        assert mock.audit_call_count == 2


class TestPipelineMaxRetries:
    """Audit always fails — verify graceful degradation."""

    def test_exhausts_retries_and_still_delivers(self):
        mock = MockClaude(audit_responses=[
            '{"verdict": "fail", "feedback": "Output is wrong."}',
        ])
        state = _initial_state(task_id="test-004")
        result = _run_pipeline(state, mock, max_retries=1)

        # Pipeline should still deliver even after exhausting retries
        assert result["audit_verdict"] == "fail"
        assert result["retry_count"] >= 1
        assert result["final_response"]  # delivery always happens
