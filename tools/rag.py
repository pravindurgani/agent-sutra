"""RAG context layer — LanceDB + Ollama embeddings for project file injection."""
from __future__ import annotations

import ast
import logging
import time
from pathlib import Path

import config

logger = logging.getLogger(__name__)


# ── 9B: Python-aware chunking ─────────────────────────────────────


def _chunk_python(content: str, file_path: str) -> list[dict]:
    """Chunk a Python file at function/class boundaries.

    Each chunk includes the full function/class body plus a header
    with the file path and definition name for context.

    Args:
        content: Full file content.
        file_path: Relative path for labelling chunks.

    Returns:
        List of chunk dicts with text, file_path, line_start, line_end, kind.
    """
    chunks: list[dict] = []
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return _chunk_lines(content, file_path)

    lines = content.splitlines(keepends=True)

    # Extract top-level and nested functions/classes
    nodes: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            nodes.append(node)

    if not nodes:
        return _chunk_lines(content, file_path)

    # Sort by line number
    nodes.sort(key=lambda n: n.lineno)

    for node in nodes:
        start = node.lineno - 1  # 0-indexed
        end = node.end_lineno if node.end_lineno else start + 1
        body = "".join(lines[start:end])
        if len(body.strip()) < 20:
            continue
        chunks.append({
            "text": f"# {file_path}:{node.lineno} — {node.name}\n{body}",
            "file_path": file_path,
            "line_start": node.lineno,
            "line_end": end,
            "kind": "function" if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else "class",
        })

    # Module-level code (imports, constants) — first 50 lines or up to first def
    first_def_line = nodes[0].lineno if nodes else len(lines)
    module_header = "".join(lines[:min(first_def_line - 1, 50)])
    if module_header.strip():
        chunks.insert(0, {
            "text": f"# {file_path}:1 — module header\n{module_header}",
            "file_path": file_path,
            "line_start": 1,
            "line_end": min(first_def_line - 1, 50),
            "kind": "module_header",
        })

    return chunks


def _chunk_lines(content: str, file_path: str) -> list[dict]:
    """Line-based chunking with overlap for non-Python files.

    Args:
        content: Full file content.
        file_path: Relative path for labelling chunks.

    Returns:
        List of chunk dicts with text, file_path, line_start, line_end, kind.
    """
    lines = content.splitlines(keepends=True)
    chunks: list[dict] = []
    size = config.RAG_CHUNK_SIZE
    overlap = config.RAG_CHUNK_OVERLAP

    for i in range(0, len(lines), size - overlap):
        block = "".join(lines[i:i + size])
        if len(block.strip()) < 20:
            continue
        chunks.append({
            "text": f"# {file_path}:{i + 1}\n{block}",
            "file_path": file_path,
            "line_start": i + 1,
            "line_end": min(i + size, len(lines)),
            "kind": "block",
        })

    return chunks


def chunk_file(file_path: Path, project_root: Path) -> list[dict]:
    """Chunk a single file using the appropriate strategy.

    Args:
        file_path: Absolute path to the file.
        project_root: Project root for computing relative paths.

    Returns:
        List of chunk dicts. Empty list on failure.
    """
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    # Cap file size to avoid embedding giant files
    if len(content) > 100_000:
        content = content[:100_000]

    rel_path = str(file_path.relative_to(project_root))

    if file_path.suffix == ".py":
        return _chunk_python(content, rel_path)
    return _chunk_lines(content, rel_path)


# ── 9C: Embedding and index management ────────────────────────────


def _embed_via_ollama(texts: list[str]) -> list[list[float]]:
    """Get embeddings from Ollama's embedding model.

    Batches requests to avoid overwhelming Ollama on 16GB machines.

    Args:
        texts: List of text strings to embed.

    Returns:
        List of embedding vectors (768-dim for nomic-embed-text).
    """
    import httpx

    base_url = config.OLLAMA_BASE_URL.rstrip("/")
    embeddings: list[list[float]] = []
    batch_size = 16

    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        try:
            resp = httpx.post(
                f"{base_url}/api/embed",
                json={"model": config.RAG_EMBED_MODEL, "input": batch},
                timeout=60.0,
            )
            resp.raise_for_status()
            data = resp.json()
            embeddings.extend(data["embeddings"])
        except Exception as e:
            logger.warning("Embedding batch %d failed: %s", i // batch_size, e)
            # Pad with zero vectors so indices stay aligned
            embeddings.extend([[0.0] * 768] * len(batch))

    return embeddings


def build_index(project_name: str, project_path: Path) -> bool:
    """Build or rebuild the RAG index for a project.

    Args:
        project_name: Name of the project (used as index directory name).
        project_path: Absolute path to the project root.

    Returns:
        True if index was built successfully or is already fresh.
    """
    import lancedb

    index_dir = config.RAG_INDEX_DIR / project_name
    index_dir.mkdir(parents=True, exist_ok=True)

    # Check staleness
    marker = index_dir / ".indexed_at"
    if marker.exists():
        age_hours = (time.time() - marker.stat().st_mtime) / 3600
        if age_hours < config.RAG_STALE_HOURS:
            logger.debug("RAG index for %s is fresh (%.1fh old)", project_name, age_hours)
            return True

    # Collect files (reuse planner's exclude list)
    from brain.nodes.planner import _INJECT_EXTENSIONS, _INJECT_EXCLUDE_DIRS

    source_files: list[Path] = []
    file_count = 0
    try:
        for p in project_path.rglob("*"):
            file_count += 1
            if file_count > config.RAG_MAX_INDEX_FILES * 2:
                break
            if any(excluded in p.parts for excluded in _INJECT_EXCLUDE_DIRS):
                continue
            if p.is_symlink():
                continue
            if p.is_file() and p.suffix in _INJECT_EXTENSIONS:
                source_files.append(p)
    except OSError as e:
        logger.warning("Failed to scan %s for RAG indexing: %s", project_path, e)
        return False

    if not source_files:
        logger.info("No indexable files in %s", project_name)
        return False

    if len(source_files) > config.RAG_MAX_INDEX_FILES:
        logger.warning(
            "Project %s has %d files, exceeds RAG_MAX_INDEX_FILES (%d). Skipping.",
            project_name, len(source_files), config.RAG_MAX_INDEX_FILES,
        )
        return False

    # Chunk all files
    all_chunks: list[dict] = []
    for f in source_files:
        all_chunks.extend(chunk_file(f, project_path))

    if not all_chunks:
        return False

    logger.info("RAG indexing %s: %d chunks from %d files", project_name, len(all_chunks), len(source_files))

    # Embed
    texts = [c["text"] for c in all_chunks]
    vectors = _embed_via_ollama(texts)

    if len(vectors) != len(all_chunks):
        logger.warning("Embedding count mismatch: %d vectors vs %d chunks", len(vectors), len(all_chunks))
        return False

    # 11: Filter out zero-vector chunks (embedding failures padded with zeros).
    # Zero vectors have undefined cosine similarity and unpredictable L2 distance
    # (varies by query magnitude), so we exclude them at index time.
    valid_entries = [
        (chunk, vec) for chunk, vec in zip(all_chunks, vectors)
        if any(v != 0.0 for v in vec)
    ]
    if len(valid_entries) < len(all_chunks):
        dropped = len(all_chunks) - len(valid_entries)
        logger.warning("Dropped %d zero-vector chunks from RAG index (embedding failures)", dropped)

    if not valid_entries:
        logger.warning("All embeddings failed for %s — skipping index build", project_name)
        return False

    # Build LanceDB table
    records = []
    for chunk, vec in valid_entries:
        records.append({
            "text": chunk["text"],
            "file_path": chunk["file_path"],
            "line_start": chunk["line_start"],
            "line_end": chunk["line_end"],
            "kind": chunk["kind"],
            "vector": vec,
        })

    db = lancedb.connect(str(index_dir))
    # Overwrite existing table
    db.create_table("chunks", records, mode="overwrite")

    # Write marker
    marker.write_text(str(time.time()))
    logger.info("RAG index built for %s: %d chunks", project_name, len(records))
    return True


def query_index(project_name: str, query: str, top_k: int | None = None) -> list[dict]:
    """Retrieve the top-k most relevant chunks for a query.

    Args:
        project_name: Name of the project to query.
        query: The task description or search query.
        top_k: Number of results to return (defaults to config.RAG_TOP_K).

    Returns:
        List of dicts with 'text', 'file_path', 'line_start', 'line_end'.
        Returns empty list on any failure (graceful degradation).
    """
    import lancedb

    k = top_k or config.RAG_TOP_K
    index_dir = config.RAG_INDEX_DIR / project_name

    if not (index_dir / ".indexed_at").exists():
        return []

    try:
        query_vec = _embed_via_ollama([query])[0]
    except Exception as e:
        logger.warning("RAG query embedding failed: %s", e)
        return []

    try:
        db = lancedb.connect(str(index_dir))
        table = db.open_table("chunks")
        results = table.search(query_vec).limit(k).to_list()
        return [
            {
                "text": r["text"],
                "file_path": r["file_path"],
                "line_start": r["line_start"],
                "line_end": r["line_end"],
            }
            for r in results
        ]
    except Exception as e:
        logger.warning("RAG query failed for %s: %s", project_name, e)
        return []
