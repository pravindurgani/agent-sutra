"""Tests for tools/claude_client.py — retry logic for empty/thinking-only responses."""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config
from unittest.mock import patch, MagicMock
from tools.claude_client import call


def _make_response(content_blocks, input_tokens=100, output_tokens=50):
    """Build a mock Anthropic response object."""
    resp = MagicMock()
    resp.content = content_blocks
    resp.usage.input_tokens = input_tokens
    resp.usage.output_tokens = output_tokens
    resp.usage.thinking_tokens = 0
    return resp


def _make_text_block(text):
    """Build a mock text content block."""
    block = MagicMock()
    block.type = "text"
    block.text = text
    return block


def _make_thinking_block():
    """Build a mock thinking content block (no text)."""
    block = MagicMock()
    block.type = "thinking"
    block.thinking = "Let me think about this..."
    return block


class TestCallRetryOnEmptyResponse:
    """call() should retry when Claude returns no text content."""

    @patch("tools.claude_client._persist_usage")
    @patch("tools.claude_client._check_budget")
    @patch("tools.claude_client._get_client")
    @patch("tools.claude_client.time.sleep")
    def test_retries_on_no_text_content(self, mock_sleep, mock_client, mock_budget, mock_persist):
        """Thinking-only response on first attempt, normal response on second."""
        thinking_response = _make_response([_make_thinking_block()])
        text_response = _make_response([_make_text_block("Hello, world!")])

        mock_client.return_value.messages.create.side_effect = [thinking_response, text_response]

        result = call("test prompt")
        assert result == "Hello, world!"
        assert mock_client.return_value.messages.create.call_count == 2
        mock_sleep.assert_called_once()  # Slept between retries

    @patch("tools.claude_client._persist_usage")
    @patch("tools.claude_client._check_budget")
    @patch("tools.claude_client._get_client")
    @patch("tools.claude_client.time.sleep")
    def test_retries_on_empty_response(self, mock_sleep, mock_client, mock_budget, mock_persist):
        """Empty content array on first attempt, normal response on second."""
        empty_response = _make_response([])
        # Empty content list triggers "Claude returned empty response"
        empty_response.content = []
        text_response = _make_response([_make_text_block("Success")])

        mock_client.return_value.messages.create.side_effect = [empty_response, text_response]

        result = call("test prompt")
        assert result == "Success"
        assert mock_client.return_value.messages.create.call_count == 2

    @patch("tools.claude_client._persist_usage")
    @patch("tools.claude_client._check_budget")
    @patch("tools.claude_client._get_client")
    @patch("tools.claude_client.time.sleep")
    def test_gives_up_after_max_retries(self, mock_sleep, mock_client, mock_budget, mock_persist):
        """Raises RuntimeError after API_MAX_RETRIES attempts of empty responses."""
        thinking_response = _make_response([_make_thinking_block()])

        mock_client.return_value.messages.create.return_value = thinking_response

        import pytest
        with pytest.raises(RuntimeError, match="no text content"):
            call("test prompt")

        assert mock_client.return_value.messages.create.call_count == config.API_MAX_RETRIES

    @patch("tools.claude_client._persist_usage")
    @patch("tools.claude_client._check_budget")
    @patch("tools.claude_client._get_client")
    def test_does_not_catch_unrelated_runtime_errors(self, mock_client, mock_budget, mock_persist):
        """RuntimeError with different message propagates immediately."""
        mock_client.return_value.messages.create.side_effect = RuntimeError("Something completely different")

        import pytest
        with pytest.raises(RuntimeError, match="Something completely different"):
            call("test prompt")

        # Should fail on first attempt, not retry
        assert mock_client.return_value.messages.create.call_count == 1
