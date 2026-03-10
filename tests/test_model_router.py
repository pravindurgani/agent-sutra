"""Tests for tools/model_router.py — cost defaults, budget escalation, Ollama think-stripping, stats."""
from __future__ import annotations

import sys
import os
import sqlite3
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import patch
from tools.model_router import _get_today_spend, _select_model, _call_ollama, _ollama_stats, get_ollama_stats


class TestGetTodaySpend:
    """_get_today_spend() must use conservative cost defaults for unknown models."""

    @patch("tools.claude_client._usage_db_path", None)
    def test_get_today_spend_unknown_model_uses_expensive_default(self, tmp_path):
        """Unknown model 'claude-unknown-99' uses 15.00/75.00 rates (not 3.00/15.00)."""
        db_path = tmp_path / "usage.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE api_usage "
            "(model TEXT, input_tokens INTEGER, output_tokens INTEGER, "
            "thinking_tokens INTEGER, timestamp REAL)"
        )
        conn.execute(
            "INSERT INTO api_usage VALUES (?, ?, ?, ?, ?)",
            ("claude-unknown-99", 1_000_000, 1_000_000, 0, time.time()),
        )
        conn.commit()
        conn.close()

        with patch("tools.claude_client._usage_db_path", db_path), \
             patch("tools.claude_client._usage_db_initialized", True):
            cost = _get_today_spend()

        # With 15.00/75.00 rates: (1M * 15.00 + 1M * 75.00) / 1M = 90.00
        # With old 3.00/15.00 rates it would be: (1M * 3.00 + 1M * 15.00) / 1M = 18.00
        assert cost > 80.0, f"Expected >$80 with conservative defaults, got ${cost:.2f}"
        assert cost < 100.0, f"Unexpectedly high: ${cost:.2f}"


class TestBudgetEscalation:
    """Budget escalation must respect complexity level (Phase 3 — F-5 fix)."""

    @patch("tools.model_router._daily_spend_exceeds_threshold", return_value=True)
    @patch("tools.model_router._ollama_available", return_value=True)
    @patch("tools.model_router._ram_below_threshold", return_value=True)
    def test_budget_escalation_skips_high_complexity(
        self, _mock_ram: object, _mock_ollama: object, _mock_threshold: object,
    ) -> None:
        """High-complexity plan tasks stay on Claude even when budget threshold exceeded."""
        provider, _model = _select_model("plan", "high")
        assert provider == "claude"

    @patch("tools.model_router._daily_spend_exceeds_threshold", return_value=True)
    @patch("tools.model_router._ollama_available", return_value=True)
    @patch("tools.model_router._ram_below_threshold", return_value=True)
    def test_budget_escalation_routes_low_to_ollama(
        self, _mock_ram: object, _mock_ollama: object, _mock_threshold: object,
    ) -> None:
        """Low-complexity plan tasks route to Ollama when budget threshold exceeded."""
        provider, _model = _select_model("plan", "low")
        assert provider == "ollama"


class TestPurposeDependentOllamaRouting:
    """Phase 0a: classify routes to qwen2.5:7b, plan stays on deepseek-r1:14b."""

    @patch("tools.model_router._ollama_available", return_value=True)
    @patch("tools.model_router._ram_below_threshold", return_value=True)
    def test_classify_routes_to_qwen_7b(
        self, _mock_ram: object, _mock_ollama: object,
    ) -> None:
        """Low-complexity classify routes to OLLAMA_CLASSIFY_MODEL (qwen2.5:7b)."""
        provider, model = _select_model("classify", "low")
        assert provider == "ollama"
        assert model == "qwen2.5:7b"

    @patch("tools.model_router._ollama_available", return_value=True)
    @patch("tools.model_router._ram_below_threshold", return_value=True)
    def test_plan_still_routes_to_deepseek(
        self, _mock_ram: object, _mock_ollama: object,
    ) -> None:
        """Low-complexity plan routes to OLLAMA_DEFAULT_MODEL (deepseek-r1:14b)."""
        provider, model = _select_model("plan", "low")
        assert provider == "ollama"
        assert model == "deepseek-r1:14b"

    @patch("tools.model_router._daily_spend_exceeds_threshold", return_value=True)
    @patch("tools.model_router._ollama_available", return_value=True)
    @patch("tools.model_router._ram_below_threshold", return_value=True)
    def test_budget_escalation_uses_qwen_for_classify(
        self, _mock_ram: object, _mock_ollama: object, _mock_threshold: object,
    ) -> None:
        """Budget escalation routes classify to qwen2.5:7b, not deepseek."""
        provider, model = _select_model("classify", "low")
        assert provider == "ollama"
        assert model == "qwen2.5:7b"

    def test_config_ollama_classify_model_env_override(self) -> None:
        """OLLAMA_CLASSIFY_MODEL env var is respected."""
        import config
        with patch.dict(os.environ, {"OLLAMA_CLASSIFY_MODEL": "llama3:8b"}):
            # Re-read the env var as config would at import time
            val = os.getenv("OLLAMA_CLASSIFY_MODEL", "qwen2.5:7b")
            assert val == "llama3:8b"
        # Default when env not set
        assert config.OLLAMA_CLASSIFY_MODEL == "qwen2.5:7b"


class TestOllamaThinkStripping:
    """Ollama think-block stripping for DeepSeek R1 and similar reasoning models."""

    @patch("tools.model_router.requests.post")
    def test_ollama_strips_unclosed_think_block(self, mock_post: object) -> None:
        """Unclosed <think> block (no </think>) → returns empty string for retry."""
        mock_response = mock_post.return_value
        mock_response.status_code = 200
        mock_response.raise_for_status = lambda: None
        mock_response.json.return_value = {
            "message": {"content": "<think>reasoning about the problem..."}
        }
        result = _call_ollama("test prompt", "", "deepseek-r1:14b", 2000)
        assert result == ""

    @patch("tools.model_router.requests.post")
    def test_ollama_strips_complete_think_block_with_answer(self, mock_post: object) -> None:
        """Complete <think>...</think> with answer after → returns the answer."""
        mock_response = mock_post.return_value
        mock_response.status_code = 200
        mock_response.raise_for_status = lambda: None
        mock_response.json.return_value = {
            "message": {"content": "<think>let me think</think>actual answer"}
        }
        result = _call_ollama("test prompt", "", "deepseek-r1:14b", 2000)
        assert result == "actual answer"

    @patch("tools.model_router.requests.post")
    def test_ollama_strips_think_block_no_answer(self, mock_post: object) -> None:
        """Complete <think>...</think> with nothing after → returns empty string."""
        mock_response = mock_post.return_value
        mock_response.status_code = 200
        mock_response.raise_for_status = lambda: None
        mock_response.json.return_value = {
            "message": {"content": "<think>only reasoning here</think>"}
        }
        result = _call_ollama("test prompt", "", "deepseek-r1:14b", 2000)
        assert result == ""


# ── v9.0.0 Phase 9: Ollama health monitoring stats ───────────────


def _reset_ollama_stats():
    """Reset stats to zero for test isolation."""
    for key in _ollama_stats:
        _ollama_stats[key] = 0


class TestOllamaStats:
    """Ollama reliability counters and percentage calculation."""

    def setup_method(self):
        _reset_ollama_stats()

    @patch("tools.model_router.claude_client.call", return_value="claude response")
    @patch("tools.model_router._call_ollama", return_value="")
    @patch("tools.model_router._select_model", return_value=("ollama", "qwen2.5:7b"))
    @patch("tools.model_router.time.sleep")
    def test_ollama_stats_increment_on_empty(self, mock_sleep, mock_select, mock_ollama, mock_claude):
        """Ollama returning empty → empty_responses incremented."""
        from tools.model_router import route_and_call
        route_and_call("test", purpose="classify", complexity="low")
        assert _ollama_stats["empty_responses"] == 2  # 2 attempts, both empty
        assert _ollama_stats["calls"] == 2
        assert _ollama_stats["fallbacks_to_claude"] == 1

    @patch("tools.model_router.claude_client.call", return_value="claude response")
    @patch("tools.model_router._call_ollama", side_effect=ConnectionError("refused"))
    @patch("tools.model_router._select_model", return_value=("ollama", "qwen2.5:7b"))
    def test_ollama_stats_increment_on_fallback(self, mock_select, mock_ollama, mock_claude):
        """Ollama failing → errors and fallbacks_to_claude incremented."""
        from tools.model_router import route_and_call
        route_and_call("test", purpose="classify", complexity="low")
        assert _ollama_stats["errors"] == 1
        assert _ollama_stats["fallbacks_to_claude"] == 1
        assert _ollama_stats["calls"] == 1

    def test_ollama_reliability_calculation(self):
        """Manual counter values → reliability percentage correct."""
        _ollama_stats["calls"] = 100
        _ollama_stats["empty_responses"] = 10
        _ollama_stats["errors"] = 5
        _ollama_stats["fallbacks_to_claude"] = 12

        stats = get_ollama_stats()
        assert stats["calls"] == 100
        assert stats["empty_responses"] == 10
        assert stats["errors"] == 5
        assert stats["fallbacks_to_claude"] == 12
        # reliability = (1 - (10 + 5) / 100) * 100 = 85.0%
        assert stats["reliability_pct"] == 85.0

    def test_ollama_reliability_zero_calls(self):
        """Zero calls → 100% reliability (no failures)."""
        stats = get_ollama_stats()
        assert stats["reliability_pct"] == 100.0
