"""Tests for tools/claude_client.py — budget enforcement and cost tracking."""
from __future__ import annotations

import sys
import os
import sqlite3
import time
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools.claude_client import (
    BudgetExceededError, _check_budget, MODEL_COSTS,
    _init_usage_db, _persist_usage, get_usage_summary, get_cost_summary,
)
import config
from unittest.mock import patch


class TestBudgetEnforcement:
    """Budget limit checks."""

    def test_no_limits_configured(self):
        """When both limits are 0, _check_budget should pass silently."""
        original_daily = config.DAILY_BUDGET_USD
        original_monthly = config.MONTHLY_BUDGET_USD
        try:
            config.DAILY_BUDGET_USD = 0
            config.MONTHLY_BUDGET_USD = 0
            # Should not raise
            _check_budget()
        finally:
            config.DAILY_BUDGET_USD = original_daily
            config.MONTHLY_BUDGET_USD = original_monthly

    def test_budget_exceeded_error_is_runtime_error(self):
        """BudgetExceededError should be a subclass of RuntimeError."""
        assert issubclass(BudgetExceededError, RuntimeError)


class TestModelCosts:
    """Verify model cost table is populated correctly."""

    def test_sonnet_costs(self):
        costs = MODEL_COSTS.get("claude-sonnet-4-6")
        assert costs is not None
        assert costs["input"] == 3.00
        assert costs["output"] == 15.00

    def test_opus_costs(self):
        costs = MODEL_COSTS.get("claude-opus-4-6")
        assert costs is not None
        assert costs["input"] == 15.00
        assert costs["output"] == 75.00

    def test_haiku_costs(self):
        costs = MODEL_COSTS.get("claude-haiku-4-5-20251001")
        assert costs is not None
        assert costs["input"] == 0.80
        assert costs["output"] == 4.00


class TestThinkingTokenTracking:
    """Verify thinking tokens are persisted and included in cost calculations."""

    def _setup_temp_db(self, tmp_path):
        """Set up a temporary usage DB and return cleanup context."""
        import tools.claude_client as cc
        db_path = tmp_path / "test_usage.db"
        original_path = cc._usage_db_path
        original_init = cc._usage_db_initialized
        cc._usage_db_path = db_path
        cc._usage_db_initialized = False
        return cc, original_path, original_init

    def _teardown_temp_db(self, cc, original_path, original_init):
        cc._usage_db_path = original_path
        cc._usage_db_initialized = original_init

    def test_thinking_tokens_persisted(self, tmp_path):
        """thinking_tokens column exists and values are written correctly."""
        cc, orig_path, orig_init = self._setup_temp_db(tmp_path)
        try:
            _persist_usage("claude-sonnet-4-6", 100, 200, time.time(), thinking_tokens=500)
            conn = sqlite3.connect(str(cc._usage_db_path))
            row = conn.execute("SELECT thinking_tokens FROM api_usage LIMIT 1").fetchone()
            conn.close()
            assert row[0] == 500
        finally:
            self._teardown_temp_db(cc, orig_path, orig_init)

    def test_thinking_tokens_default_zero(self, tmp_path):
        """Omitting thinking_tokens defaults to 0."""
        cc, orig_path, orig_init = self._setup_temp_db(tmp_path)
        try:
            _persist_usage("claude-sonnet-4-6", 100, 200, time.time())
            conn = sqlite3.connect(str(cc._usage_db_path))
            row = conn.execute("SELECT thinking_tokens FROM api_usage LIMIT 1").fetchone()
            conn.close()
            assert row[0] == 0
        finally:
            self._teardown_temp_db(cc, orig_path, orig_init)

    def test_cost_includes_thinking(self, tmp_path):
        """get_cost_summary() includes thinking tokens in cost at output rate."""
        cc, orig_path, orig_init = self._setup_temp_db(tmp_path)
        try:
            # 1M input + 1M output + 1M thinking on sonnet
            _persist_usage("claude-sonnet-4-6", 1_000_000, 1_000_000, time.time(), thinking_tokens=1_000_000)
            summary = get_cost_summary()
            # Cost = 1M * $3/M (input) + (1M + 1M) * $15/M (output+thinking) = $3 + $30 = $33
            assert abs(summary["total_cost_usd"] - 33.0) < 0.01
            assert summary["total_thinking_tokens"] == 1_000_000
        finally:
            self._teardown_temp_db(cc, orig_path, orig_init)

    def test_usage_summary_includes_thinking(self, tmp_path):
        """get_usage_summary() returns total_thinking_tokens."""
        cc, orig_path, orig_init = self._setup_temp_db(tmp_path)
        try:
            _persist_usage("claude-sonnet-4-6", 100, 200, time.time(), thinking_tokens=300)
            _persist_usage("claude-opus-4-6", 50, 75, time.time(), thinking_tokens=150)
            summary = get_usage_summary()
            assert summary["total_thinking_tokens"] == 450
        finally:
            self._teardown_temp_db(cc, orig_path, orig_init)

    def test_budget_includes_thinking(self, tmp_path):
        """_check_budget() counts thinking tokens in spend calculation."""
        cc, orig_path, orig_init = self._setup_temp_db(tmp_path)
        try:
            # Insert enough thinking tokens to blow a tiny budget
            # 10M thinking tokens on opus at $75/M = $750
            _persist_usage("claude-opus-4-6", 0, 0, time.time(), thinking_tokens=10_000_000)

            original_daily = config.DAILY_BUDGET_USD
            try:
                config.DAILY_BUDGET_USD = 1.0  # $1 daily limit
                import pytest
                with pytest.raises(BudgetExceededError):
                    _check_budget()
            finally:
                config.DAILY_BUDGET_USD = original_daily
        finally:
            self._teardown_temp_db(cc, orig_path, orig_init)


class TestApiMaxRetries:
    """Verify API_MAX_RETRIES is separate from pipeline MAX_RETRIES."""

    def test_api_max_retries_exists(self):
        """config.API_MAX_RETRIES should be defined and >= MAX_RETRIES."""
        assert hasattr(config, "API_MAX_RETRIES")
        assert config.API_MAX_RETRIES >= config.MAX_RETRIES

    def test_api_max_retries_default(self):
        """Default API_MAX_RETRIES is 5."""
        assert config.API_MAX_RETRIES == 5

    def test_pipeline_retries_unchanged(self):
        """Pipeline MAX_RETRIES remains at 3."""
        assert config.MAX_RETRIES == 3
