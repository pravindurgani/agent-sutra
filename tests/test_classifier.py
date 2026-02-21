"""Tests for brain/nodes/classifier.py — fallback ordering."""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from brain.nodes.classifier import _FALLBACK_ORDER


class TestFallbackOrdering:
    """Verify classifier fallback scans specific types before generic 'code'."""

    def test_fallback_order_code_is_last(self):
        """The fallback list must check 'code' LAST since it's the most generic."""
        assert _FALLBACK_ORDER[-1] == "code"
        assert _FALLBACK_ORDER[0] == "project"

    def test_frontend_before_code(self):
        """'frontend' must be checked before 'code' to avoid misclassification."""
        assert _FALLBACK_ORDER.index("frontend") < _FALLBACK_ORDER.index("code")

    def test_ui_design_before_code(self):
        assert _FALLBACK_ORDER.index("ui_design") < _FALLBACK_ORDER.index("code")

    def test_data_before_code(self):
        assert _FALLBACK_ORDER.index("data") < _FALLBACK_ORDER.index("code")

    def test_all_types_present(self):
        """Verify all 7 task types are in the fallback list."""
        assert len(_FALLBACK_ORDER) == 7
        assert set(_FALLBACK_ORDER) == {"project", "frontend", "ui_design", "automation", "data", "file", "code"}
