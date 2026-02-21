"""Tests for tools/file_manager.py — metadata extraction, uploads, edge cases."""
from __future__ import annotations

import sys
import os
import re
import tempfile
import csv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools.file_manager import get_file_content, get_file_metadata, save_upload
from pathlib import Path
from unittest.mock import patch
import config


class TestGetFileContent:
    """File content reading with encoding and size limits."""

    def test_read_text_file(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello world")
        content = get_file_content(f)
        assert "hello world" in content

    def test_max_chars_truncation(self, tmp_path):
        f = tmp_path / "big.txt"
        f.write_text("x" * 10000)
        content = get_file_content(f, max_chars=100)
        assert len(content) <= 200  # content + truncation message

    def test_nonexistent_file(self):
        content = get_file_content(Path("/nonexistent/file.txt"))
        # Should return error message, not crash
        assert content is not None


class TestGetFileMetadata:
    """CSV/Excel metadata extraction."""

    def test_csv_basic(self, tmp_path):
        f = tmp_path / "data.csv"
        with open(f, "w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["name", "age", "city"])
            writer.writerow(["Alice", "30", "London"])
            writer.writerow(["Bob", "25", "Paris"])
        meta = get_file_metadata(f)
        assert meta["row_count"] == 2  # 2 data rows (header excluded)
        assert "name" in meta.get("columns", []) or "name" in str(meta.get("header", []))

    def test_csv_empty_file(self, tmp_path):
        f = tmp_path / "empty.csv"
        f.write_text("")
        meta = get_file_metadata(f)
        # Should not crash; row_count should be 0 or similar
        assert meta.get("row_count", 0) == 0

    def test_csv_header_only(self, tmp_path):
        f = tmp_path / "header_only.csv"
        f.write_text("col_a,col_b,col_c\n")
        meta = get_file_metadata(f)
        assert meta.get("row_count") == 0  # 0 data rows (header-only file)

    def test_csv_large_sample(self, tmp_path):
        f = tmp_path / "large.csv"
        with open(f, "w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["id", "value"])
            for i in range(1000):
                writer.writerow([i, f"val_{i}"])
        meta = get_file_metadata(f)
        assert meta["row_count"] == 1000  # 1000 data rows (header excluded)

    def test_non_csv_file(self, tmp_path):
        f = tmp_path / "script.py"
        f.write_text("print('hello')")
        meta = get_file_metadata(f)
        assert "size_bytes" in meta or "size_human" in meta


class TestSaveUpload:
    """Upload saving with UUID-based unique filenames."""

    def test_uuid_in_filename(self, tmp_path):
        """Saved filename contains UUID suffix for uniqueness."""
        with patch.object(config, "UPLOADS_DIR", tmp_path):
            path = save_upload(b"test data", "report.csv")
            # Filename should match pattern: report_<8hex>.csv
            assert re.match(r"report_[0-9a-f]{8}\.csv", path.name)

    def test_two_uploads_never_collide(self, tmp_path):
        """Two uploads of same filename produce different paths."""
        with patch.object(config, "UPLOADS_DIR", tmp_path):
            p1 = save_upload(b"data1", "data.csv")
            p2 = save_upload(b"data2", "data.csv")
            assert p1 != p2
            assert p1.read_bytes() == b"data1"
            assert p2.read_bytes() == b"data2"

    def test_path_traversal_sanitized(self, tmp_path):
        """Path components are stripped from filename."""
        with patch.object(config, "UPLOADS_DIR", tmp_path):
            path = save_upload(b"data", "../../etc/passwd")
            # Must be inside uploads dir, not traversed
            assert path.parent == tmp_path
            assert "etc" not in str(path)
            assert "passwd" in path.name

    def test_dotfile_gets_prefix(self, tmp_path):
        """Hidden files get 'upload_' prefix."""
        with patch.object(config, "UPLOADS_DIR", tmp_path):
            path = save_upload(b"data", ".hidden")
            assert path.name.startswith("upload_")
