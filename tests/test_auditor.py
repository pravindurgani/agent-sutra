"""Tests for brain/nodes/auditor.py — JSON extraction with edge cases."""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from brain.nodes.auditor import _extract_json


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
