"""Tests for AgentSutra v8 Phase 4: UX & Orchestration.

Covers:
- Live output registry (sandbox._live_output)
- Hash-gated edit logic
- Debug JSON sidecar (_write_debug_sidecar)
- Stage timing collection (_wrap_node)
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import config


# ── Live output registry tests ───────────────────────────────────────


class TestLiveOutputRegistry:
    """Verify the thread-safe live output registry in sandbox.py."""

    def test_register_returns_empty(self):
        from tools.sandbox import _register_live_output, get_live_output, _clear_live_output

        _register_live_output("test-task-1")
        assert get_live_output("test-task-1") == ""
        _clear_live_output("test-task-1")

    def test_append_and_tail(self):
        from tools.sandbox import (
            _register_live_output, _append_live_output,
            get_live_output, _clear_live_output,
        )

        _register_live_output("test-task-2")
        _append_live_output("test-task-2", "line 1")
        _append_live_output("test-task-2", "line 2")
        _append_live_output("test-task-2", "line 3")

        result = get_live_output("test-task-2", tail=2)
        assert "line 2" in result
        assert "line 3" in result
        assert "line 1" not in result
        _clear_live_output("test-task-2")

    def test_clear_removes_output(self):
        from tools.sandbox import (
            _register_live_output, _append_live_output,
            get_live_output, _clear_live_output,
        )

        _register_live_output("test-task-3")
        _append_live_output("test-task-3", "data")
        _clear_live_output("test-task-3")
        assert get_live_output("test-task-3") == ""

    def test_bounded_memory(self):
        """Appending 100 lines keeps internal list bounded (≤50 after pruning)."""
        from tools.sandbox import (
            _register_live_output, _append_live_output,
            _live_output, _live_output_lock, _clear_live_output,
        )

        _register_live_output("test-task-4")
        for i in range(100):
            _append_live_output("test-task-4", f"line {i}")

        with _live_output_lock:
            lines = _live_output.get("test-task-4", [])
            assert len(lines) <= 50

        _clear_live_output("test-task-4")


# ── Hash-gated edit tests ────────────────────────────────────────────


class TestHashGatedEdits:
    """Verify the hash-gating logic used in Telegram status polling."""

    def test_same_content_same_hash(self):
        """Identical content → identical hash → edit skipped."""
        label1 = "Executing... (task abc12345)"
        label2 = "Executing... (task abc12345)"
        assert hash(label1) == hash(label2)

    def test_different_content_different_hash(self):
        """Different content → different hash → edit triggered."""
        label1 = "Executing... (task abc12345)"
        label2 = "Auditing... (task abc12345)"
        assert hash(label1) != hash(label2)

    def test_live_output_changes_hash(self):
        """Same stage but different stdout → hash changes → edit triggered."""
        base = "Executing...\n\nLatest output:\nProcessing row 100"
        updated = "Executing...\n\nLatest output:\nProcessing row 200"
        assert hash(base) != hash(updated)


# ── Debug sidecar tests ──────────────────────────────────────────────


class TestDebugSidecar:
    """Verify _write_debug_sidecar creates correct JSON files."""

    def test_sidecar_written(self, tmp_path):
        """Debug JSON file exists at expected path after write."""
        from brain.nodes.deliverer import _write_debug_sidecar

        state = {
            "task_id": "test-sidecar-001",
            "message": "Write a hello world",
            "task_type": "code",
            "project_name": "",
            "stage_timings": [
                {"name": "classifying", "duration_ms": 150},
                {"name": "planning", "duration_ms": 800},
                {"name": "executing", "duration_ms": 3200},
                {"name": "auditing", "duration_ms": 400},
                {"name": "delivering", "duration_ms": 200},
            ],
            "audit_verdict": "pass",
            "retry_count": 0,
        }

        with patch.object(config, "OUTPUTS_DIR", tmp_path):
            _write_debug_sidecar(state)

        sidecar_path = tmp_path / "test-sidecar-001.debug.json"
        assert sidecar_path.exists()

        data = json.loads(sidecar_path.read_text())
        assert data["task_id"] == "test-sidecar-001"
        assert data["verdict"] == "pass"
        assert len(data["stages"]) == 5
        assert data["total_duration_ms"] == 4750
        assert "task_type" in data

    def test_sidecar_handles_failure_gracefully(self, tmp_path):
        """Writing to an invalid path doesn't raise."""
        from brain.nodes.deliverer import _write_debug_sidecar

        state = {
            "task_id": "test-sidecar-002",
            "message": "test",
            "task_type": "code",
            "stage_timings": [],
            "audit_verdict": "fail",
            "retry_count": 2,
        }

        # Point to a non-existent directory
        with patch.object(config, "OUTPUTS_DIR", tmp_path / "nonexistent" / "deep"):
            _write_debug_sidecar(state)  # Should not raise


# ── Stage timing collection tests ────────────────────────────────────


class TestStageTimingCollection:
    """Verify _wrap_node records timing in stage_timings."""

    def test_wrap_node_adds_timing(self):
        """Wrapped node should add a timing entry to the result."""
        from brain.graph import _wrap_node

        def mock_node(state):
            time.sleep(0.05)  # Simulate some work
            return {"plan": "do stuff"}

        wrapped = _wrap_node("planning", mock_node)
        state = {
            "task_id": "timing-test",
            "stage_timings": [],
        }

        result = wrapped(state)
        assert "stage_timings" in result
        assert len(result["stage_timings"]) == 1
        assert result["stage_timings"][0]["name"] == "planning"
        assert result["stage_timings"][0]["duration_ms"] >= 40  # At least ~50ms

    def test_wrap_node_accumulates_timings(self):
        """Multiple wrapped nodes should accumulate timing entries."""
        from brain.graph import _wrap_node

        def node_a(state):
            return {"code": "print('hi')"}

        def node_b(state):
            return {"result": "success"}

        wrapped_a = _wrap_node("executing", node_a)
        wrapped_b = _wrap_node("auditing", node_b)

        state = {"task_id": "acc-test", "stage_timings": []}
        result_a = wrapped_a(state)

        # Simulate state merge (LangGraph merges result into state)
        state["stage_timings"] = result_a["stage_timings"]
        result_b = wrapped_b(state)

        assert len(result_b["stage_timings"]) == 2
        assert result_b["stage_timings"][0]["name"] == "executing"
        assert result_b["stage_timings"][1]["name"] == "auditing"
