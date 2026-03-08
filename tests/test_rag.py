"""Tests for the RAG context layer (tools/rag.py) and planner integration."""
from __future__ import annotations

import sys
import os
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pathlib import Path
from unittest.mock import patch, MagicMock

import config
from tools.rag import _chunk_python, _chunk_lines, chunk_file


# ── 9B: Chunking tests ────────────────────────────────────────────


class TestChunkPython:
    """_chunk_python should extract function/class-level chunks via AST."""

    def test_extracts_functions(self):
        """Functions are extracted as individual chunks."""
        code = '''import os

def foo():
    return 1

def bar(x):
    return x + 1

class Baz:
    def method(self):
        pass
'''
        chunks = _chunk_python(code, "test.py")
        names = [c["text"].split("—")[1].strip().split("\n")[0] for c in chunks if "—" in c["text"]]
        assert "foo" in names
        assert "bar" in names
        assert "Baz" in names
        # Module header should be present
        assert any(c["kind"] == "module_header" for c in chunks)

    def test_handles_syntax_error(self):
        """Invalid Python falls back to line-based chunking."""
        code = "def broken(\n  this is not valid python\n" * 10
        chunks = _chunk_python(code, "broken.py")
        # Should still produce chunks (via line-based fallback)
        assert len(chunks) > 0
        assert all(c["kind"] == "block" for c in chunks)

    def test_empty_file(self):
        """Empty file produces no chunks."""
        chunks = _chunk_python("", "empty.py")
        assert chunks == []

    def test_chunk_metadata(self):
        """Chunks include correct file_path and line numbers."""
        code = '''def hello():
    print("hi")

def world():
    print("world")
'''
        chunks = _chunk_python(code, "greet.py")
        func_chunks = [c for c in chunks if c["kind"] == "function"]
        assert len(func_chunks) == 2
        assert func_chunks[0]["file_path"] == "greet.py"
        assert func_chunks[0]["line_start"] == 1
        assert func_chunks[1]["line_start"] == 4


class TestChunkLines:
    """_chunk_lines should produce overlapping line-based chunks."""

    def test_basic_chunking(self):
        """Produces chunks with correct boundaries."""
        content = "\n".join(f"line {i}" for i in range(200))
        chunks = _chunk_lines(content, "file.yaml")
        assert len(chunks) > 1
        assert all(c["kind"] == "block" for c in chunks)
        assert chunks[0]["line_start"] == 1

    def test_small_file_single_chunk(self):
        """File smaller than chunk size produces one chunk."""
        content = "\n".join(f"line {i}" for i in range(10))
        chunks = _chunk_lines(content, "small.yaml")
        assert len(chunks) == 1

    def test_empty_content(self):
        """Empty content produces no chunks."""
        chunks = _chunk_lines("", "empty.yaml")
        assert chunks == []


class TestChunkFile:
    """chunk_file dispatches to the correct chunking strategy."""

    def test_python_file_uses_ast(self):
        """Python files are chunked via AST."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "module.py"
            f.write_text("def func():\n    return 42\n")
            chunks = chunk_file(f, root)
            assert any(c["kind"] == "function" for c in chunks)

    def test_non_python_uses_lines(self):
        """Non-Python files use line-based chunking."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "config.yaml"
            f.write_text("\n".join(f"key_{i}: value_{i}" for i in range(200)))
            chunks = chunk_file(f, root)
            assert all(c["kind"] == "block" for c in chunks)

    def test_caps_large_files(self):
        """Files >100KB are truncated before chunking."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "big.py"
            # 200KB file — should be capped
            f.write_text("x = 1\n" * 40_000)
            chunks = chunk_file(f, root)
            # Should produce chunks but not from the full 200KB
            assert len(chunks) > 0
            total_text = sum(len(c["text"]) for c in chunks)
            assert total_text < 200_000

    def test_missing_file(self):
        """Missing file returns empty list."""
        chunks = chunk_file(Path("/nonexistent/file.py"), Path("/nonexistent"))
        assert chunks == []


# ── 9C: Embedding and index management ────────────────────────────


class TestEmbedViaOllama:
    """_embed_via_ollama should batch requests and handle failures."""

    @patch("httpx.post")
    def test_batching(self, mock_post):
        """Texts are batched in groups of 16."""
        from tools.rag import _embed_via_ollama

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"embeddings": [[0.1] * 768] * 16}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        texts = [f"text {i}" for i in range(32)]
        result = _embed_via_ollama(texts)

        assert mock_post.call_count == 2
        assert len(result) == 32

    @patch("httpx.post")
    def test_failure_pads_zeros(self, mock_post):
        """Failed batch returns zero vectors to maintain alignment."""
        from tools.rag import _embed_via_ollama

        mock_post.side_effect = Exception("Connection refused")

        texts = ["text 1", "text 2"]
        result = _embed_via_ollama(texts)

        assert len(result) == 2
        assert result[0] == [0.0] * 768


class TestBuildIndex:
    """build_index should create a LanceDB table from project files."""

    @patch("tools.rag._embed_via_ollama")
    @patch("lancedb.connect")
    def test_creates_table(self, mock_connect, mock_embed):
        """Builds index with correct number of records."""
        # Return matching number of vectors for however many chunks are generated
        mock_embed.side_effect = lambda texts: [[0.1] * 768] * len(texts)

        mock_db = MagicMock()
        mock_connect.return_value = mock_db

        with tempfile.TemporaryDirectory() as td:
            project_path = Path(td)
            (project_path / "main.py").write_text("def hello():\n    return 1\n")
            (project_path / "utils.py").write_text("def helper():\n    return 2\n")

            with tempfile.TemporaryDirectory() as idx_dir:
                with patch.object(config, "RAG_INDEX_DIR", Path(idx_dir)):
                    from tools.rag import build_index
                    result = build_index("test-proj", project_path)

                    assert result is True
                    mock_db.create_table.assert_called_once()
                    call_args = mock_db.create_table.call_args
                    assert call_args[0][0] == "chunks"
                    assert call_args[1]["mode"] == "overwrite"

    def test_skips_fresh_index(self):
        """Returns True without re-indexing if marker is fresh."""
        with tempfile.TemporaryDirectory() as idx_dir:
            with patch.object(config, "RAG_INDEX_DIR", Path(idx_dir)):
                marker_dir = Path(idx_dir) / "test-proj"
                marker_dir.mkdir()
                marker = marker_dir / ".indexed_at"
                marker.write_text(str(time.time()))

                from tools.rag import build_index
                # Should return True without trying to import lancedb
                result = build_index("test-proj", Path("/nonexistent"))
                assert result is True

    def test_skips_large_projects(self):
        """Projects exceeding RAG_MAX_INDEX_FILES are skipped."""
        with tempfile.TemporaryDirectory() as td:
            project_path = Path(td)
            # Create more files than the limit
            for i in range(config.RAG_MAX_INDEX_FILES + 10):
                (project_path / f"file_{i}.py").write_text(f"x = {i}\n")

            with tempfile.TemporaryDirectory() as idx_dir:
                with patch.object(config, "RAG_INDEX_DIR", Path(idx_dir)):
                    from tools.rag import build_index
                    result = build_index("big-proj", project_path)
                    assert result is False


class TestQueryIndex:
    """query_index should return relevant chunks or empty list on failure."""

    @patch("tools.rag._embed_via_ollama")
    @patch("lancedb.connect")
    def test_returns_results(self, mock_connect, mock_embed):
        """Successful query returns formatted results."""
        mock_embed.return_value = [[0.1] * 768]

        mock_table = MagicMock()
        mock_table.search.return_value.limit.return_value.to_list.return_value = [
            {"text": "def foo():", "file_path": "main.py", "line_start": 1, "line_end": 5, "_distance": 0.1},
        ]
        mock_db = MagicMock()
        mock_db.open_table.return_value = mock_table
        mock_connect.return_value = mock_db

        with tempfile.TemporaryDirectory() as idx_dir:
            with patch.object(config, "RAG_INDEX_DIR", Path(idx_dir)):
                marker_dir = Path(idx_dir) / "test-proj"
                marker_dir.mkdir()
                (marker_dir / ".indexed_at").write_text(str(time.time()))

                from tools.rag import query_index
                results = query_index("test-proj", "find the foo function")

                assert len(results) == 1
                assert results[0]["file_path"] == "main.py"
                assert results[0]["text"] == "def foo():"

    def test_missing_index_returns_empty(self):
        """No index marker → empty list."""
        with tempfile.TemporaryDirectory() as idx_dir:
            with patch.object(config, "RAG_INDEX_DIR", Path(idx_dir)):
                from tools.rag import query_index
                results = query_index("nonexistent-proj", "anything")
                assert results == []


# ── 9D: Planner integration ───────────────────────────────────────


class TestInjectProjectFilesRAG:
    """_inject_project_files should try RAG first, then fall back to legacy."""

    @patch("tools.rag.query_index")
    @patch("tools.rag.build_index")
    def test_uses_rag_when_enabled(self, mock_build, mock_query):
        """RAG_ENABLED=true: uses RAG path."""
        mock_build.return_value = True
        mock_query.return_value = [
            {"text": "# main.py:1 — foo\ndef foo(): pass", "file_path": "main.py",
             "line_start": 1, "line_end": 2},
        ]

        with tempfile.TemporaryDirectory() as td:
            project_path = Path(td)
            (project_path / "main.py").write_text("def foo(): pass\n")

            state = {
                "task_id": "test-rag",
                "message": "Fix the foo function",
                "project_config": {"path": str(project_path)},
                "project_name": "test-proj",
            }

            with patch.object(config, "RAG_ENABLED", True):
                from brain.nodes.planner import _inject_project_files
                result = _inject_project_files(state, "base prompt")

                assert "(via RAG)" in result
                assert "foo" in result
                mock_build.assert_called_once()
                mock_query.assert_called_once()

    @patch("brain.nodes.planner.claude_client")
    def test_fallback_when_rag_fails(self, mock_claude):
        """RAG failure falls back to legacy Claude-based selector."""
        mock_claude.call.return_value = '["main.py"]'

        with tempfile.TemporaryDirectory() as td:
            project_path = Path(td)
            (project_path / "main.py").write_text("def foo(): pass\n")

            state = {
                "task_id": "test-fallback",
                "message": "Fix it",
                "project_config": {"path": str(project_path)},
                "project_name": "test-proj",
            }

            with patch.object(config, "RAG_ENABLED", True):
                with patch("tools.rag.build_index", side_effect=Exception("Ollama down")):
                    from brain.nodes.planner import _inject_project_files
                    _inject_project_files(state, "base prompt")

                    # Should have fallen back to legacy selector
                    mock_claude.call.assert_called_once()

    @patch("brain.nodes.planner.claude_client")
    def test_legacy_when_rag_disabled(self, mock_claude):
        """RAG_ENABLED=false: skips RAG entirely, uses legacy."""
        mock_claude.call.return_value = '["main.py"]'

        with tempfile.TemporaryDirectory() as td:
            project_path = Path(td)
            (project_path / "main.py").write_text("def foo(): pass\n")

            state = {
                "task_id": "test-legacy",
                "message": "Fix it",
                "project_config": {"path": str(project_path)},
                "project_name": "test-proj",
            }

            with patch.object(config, "RAG_ENABLED", False):
                from brain.nodes.planner import _inject_project_files
                _inject_project_files(state, "base prompt")

                # RAG should not have been attempted
                mock_claude.call.assert_called_once()
