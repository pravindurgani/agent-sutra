# AgentSutra v8.7.0 — Implementation Summary

> **Generated:** 2026-03-08
> **Scope:** Phases 0–9 from `IMPLEMENTATION_PLAN.md`
> **Commits:** 10 (7f1c921 through HEAD, Phase 9 unstaged)

---

## Phase 0 — Quick Wins

| Sub-phase | What | Why | File |
|-----------|------|-----|------|
| 0A | Fixed `/status` showing raw task_type instead of human-friendly label | UX polish — users see "Code Generation" not "code" | `bot/handlers.py` |
| 0B | Added try/except around deploy URL in deliverer | Deploy failures crashed the delivery stage | `brain/nodes/deliverer.py` |
| 0C | Fixed `/stopserver` hook targeting wrong process | Server cleanup killed parent instead of child | `bot/handlers.py` |

**Could break:** Minimal risk. All changes are isolated error handling or string formatting.

---

## Phase 1 — Preview Server Hardening

| Sub-phase | What | Why | File |
|-----------|------|-----|------|
| 1A | Bound preview servers to `127.0.0.1` only | Servers were binding to `0.0.0.0`, exposed on LAN | `tools/sandbox.py` |
| 1B | Added free-port allocation via socket binding | Hardcoded port assumption failed when port was in use | `tools/sandbox.py` |
| 1C | Added orphan process cleanup on bot shutdown | Leaked server processes accumulated after restarts | `tools/sandbox.py`, `bot/handlers.py` |

**Could break:**
- **1A:** If any test or workflow expects external access to preview servers, it will fail. All access must go through `127.0.0.1`.
- **1B:** Free-port allocation binds a socket to find an available port. If the port is taken between allocation and server start (race condition), the server fails to bind. This is unlikely but possible under heavy concurrent use.

---

## Phase 2 — AST Code Scanner Hardening

| Sub-phase | What | Why | File |
|-----------|------|-----|------|
| 2A | Added AST-based scanner for dynamic code evaluation patterns | Static regex missed dynamic code construction hidden in variables | `tools/sandbox.py` |
| 2B | Added file-write content scanning | Code could write malicious content to a `.py` file then import it, bypassing the scanner | `tools/sandbox.py` |

**Could break:**
- **2A:** False positives on legitimate uses of `compile()` in data-science code (e.g., pandas). The scanner checks for `builtins` attribute access, which should avoid pandas false positives, but edge cases exist.
- **2B:** File-write scanning adds ~5ms per code run. If a task writes many files in a loop, this could accumulate. Graceful degradation: scan failure logs a warning and allows the run to proceed.

---

## Phase 3 — Chain BLOCKED Detection

| Sub-phase | What | Why | File |
|-----------|------|-----|------|
| 3A | Added BLOCKED status detection in chain gate | Chain continued to next step even when a step was security-blocked | `bot/handlers.py` |
| 3B | Added timeout progress feedback to task execution | Users saw no output during long-running tasks | `bot/handlers.py` |

**Could break:**
- **3A:** The BLOCKED detection checks for "BLOCKED" in the response text. If a legitimate response contains this word (e.g., "I blocked the request"), it could be falsely halted. Mitigated by checking the task status field, not just text.

---

## Phase 4 — Ollama Stabilisation

| Sub-phase | What | Why | File |
|-----------|------|-----|------|
| 4A | Added empty response retry for Ollama | Ollama occasionally returns empty strings, causing downstream NoneType errors | `tools/model_router.py` |
| 4B | Added startup inference test | Bot started successfully even when Ollama was down, then failed on first task | `main.py` |

**Could break:**
- **4A:** The retry adds up to 2 extra Ollama calls on empty response. If Ollama is consistently returning empty (e.g., model not loaded), this wastes ~6 seconds before falling back.
- **4B:** Startup test blocks bot launch until Ollama responds or times out (10s). On a slow Mac Mini cold start, this could delay bot availability.

---

## Phase 5 — Anti-Fabrication

| Sub-phase | What | Why | File |
|-----------|------|-----|------|
| 5A | Added file reference validation in executor | Agent claimed to create files that didn't exist | `brain/nodes/executor.py` |
| 5B | Enhanced auditor prompt with fabrication detection | Agent substituted libraries (asked for pandas, used polars) | `brain/nodes/auditor.py` |
| 5C | Hardened credential filter with substring matching | `_filter_env()` missed partial key names like `MY_API_KEY` | `tools/sandbox.py` |

**Could break:**
- **5A:** File validation checks the workspace directory. If a task legitimately creates files in a subdirectory with unusual naming, the validator may miss them (it checks common extensions only).
- **5B:** The fabrication detection prompt adds ~200 tokens to every audit call. At Opus pricing, this is ~$0.003/task — negligible individually but cumulative.

---

## Phase 6 — Smart Subprocess Scanning

| Sub-phase | What | Why | File |
|-----------|------|-----|------|
| 6A | Replaced blanket `subprocess.*` block with AST-based safe command allowlist | Blocking all subprocess broke legitimate tasks (e.g., `subprocess.run(["python3", "test.py"])`) | `tools/sandbox.py` |

**Could break:**
- **Critical cross-phase interaction:** The Ultimate Test Suite Test 3.9 expects `subprocess.run(["ls", "-la"])` to be **fully blocked**. Phase 6A changed this — safe commands like `ls` now pass the scanner. The test expectation document needs updating, or the test will report a "failure" that's actually correct new behaviour.
- **Allowlist gaps:** The safe command list (`python3`, `pip`, `git`, `ls`, `cat`, `head`, `tail`, `wc`, `sort`, `uniq`, `diff`, `echo`, `mkdir`, `cp`, `mv`, `touch`) may be too permissive. `mv` could move sensitive files; `cp` could duplicate them. These are audit-logged (Tier 3) but not blocked.

---

## Phase 7 — Log Quality

| Sub-phase | What | Why | File |
|-----------|------|-----|------|
| 7A | Added per-stage timing to completion log | Pipeline performance was invisible without `/debug` | `brain/graph.py` |
| 7B | Added task completion summary log line | No single log line confirmed successful completion | `brain/graph.py` |
| 7C | Simplified file selector to single-attempt (prep for RAG) | Multi-attempt Claude file selection was expensive and often guessed wrong | `brain/nodes/planner.py` |
| 7D | Added shell script truncation detection | `max_tokens` cutoff left unclosed `if/fi` and `do/done` blocks | `brain/nodes/executor.py` |

**Could break:**
- **7C:** Single-attempt file selection is strictly worse than retry for non-RAG mode. If RAG is disabled (config or missing lancedb), the legacy path now gets one shot instead of two. Acceptable tradeoff since RAG is the primary path.
- **7D:** Shell truncation threshold uses `+2` tolerance (`if_count > fi_count + 2`). A shell script with exactly 2 unmatched `if` keywords won't trigger truncation detection. This is intentional to avoid Python false positives (`if` is common in Python, `fi` is not).

---

## Phase 8 — Path Sanitisation

| Sub-phase | What | Why | File |
|-----------|------|-----|------|
| 8A | Added `_sanitize_paths()` to deliverer | Production paths like `/Users/agentruntime1/Desktop/project/` leaked in responses | `brain/nodes/deliverer.py` |

**Could break:**
- **Regex over-matching:** The pattern `/Users/\w+/` replaces any macOS user path. If a task legitimately discusses user paths (e.g., "save to /Users/john/Desktop"), the path gets sanitised to `~/Desktop`. This is the correct behaviour for security but could confuse users who expect literal paths.
- **Hostname replacement:** `Admin.local` is replaced with `<hostname>` — only matches that exact string. Other hostnames in output won't be sanitised.

---

## Phase 9 — RAG Context Layer

| Sub-phase | What | Why | File |
|-----------|------|-----|------|
| 9A | Added 8 RAG config constants | Centralised RAG configuration | `config.py` |
| 9B | AST-based Python chunking | Function/class-level chunks for semantic search | `tools/rag.py` (NEW) |
| 9C | Line-based chunking with overlap | Non-Python files chunked in 120-line blocks with 20-line overlap | `tools/rag.py` |
| 9D | Ollama embedding via `nomic-embed-text` | Local embeddings, zero API cost | `tools/rag.py` |
| 9E | LanceDB index management with staleness | 24h TTL via `.indexed_at` marker file | `tools/rag.py` |
| 9F | Planner integration: RAG-first with legacy fallback | Semantic file injection replaces random sampling | `brain/nodes/planner.py` |
| 9G | `/reindex` command | Force re-index when project files change | `bot/handlers.py`, `bot/telegram_bot.py` |

**Could break:**
- **LanceDB dependency:** `lancedb>=0.6.0` is a new dependency (~50MB). If not installed, all RAG paths gracefully fall back to legacy. But the fallback is now single-attempt (Phase 7C), so file injection quality degrades compared to pre-Phase-7 retry behaviour.
- **Ollama `nomic-embed-text` model required:** RAG calls `ollama embed` with this model. If not pulled on the Mac Mini, embedding returns zero vectors, search returns garbage results, and the planner falls back to legacy. **Action required: `ollama pull nomic-embed-text` before production use.**
- **Index directory:** `~/.agentsutra/rag_indexes/` is created automatically. If the home directory has restricted permissions (unlikely on Mac Mini), index creation fails silently.
- **Circular import risk:** `tools/rag.py` is imported lazily inside `_inject_project_files()` to avoid circular dependencies. If someone adds a top-level import, it will break.
- **Embedding batch size:** Fixed at 16 texts per Ollama call. For large projects (500 files, ~2000 chunks), this means ~125 Ollama calls during indexing. On the Mac Mini M2, this takes ~30-60 seconds. First-time indexing of a large project will delay the planner stage.
- **LanceDB table overwrite:** `build_index()` uses `mode="overwrite"` — the entire index is rebuilt each time the staleness check fails. No incremental updates. This is correct for now but wasteful for projects where only 1-2 files changed.

---

## Cross-Phase Interaction Risks

| Risk | Phases | Impact | Mitigation |
|------|--------|--------|------------|
| RAG disabled + single-attempt legacy | 7C + 9F | File injection quality drops if both RAG fails AND legacy gets one shot | Ensure `nomic-embed-text` is pulled; legacy single-attempt is acceptable for small projects |
| AST scanner + subprocess allowlist | 2A + 6A | Two separate scanners check generated code — ordering matters | AST scanner runs first (catches dynamic code construction), then subprocess allowlist (catches shell commands) |
| Chain BLOCKED + path sanitisation | 3A + 8A | If sanitisation rewrites a path that appears in a BLOCKED message, the error context is altered | BLOCKED detection uses task status field, not response text — sanitisation doesn't affect detection |
| Shell truncation + Ollama empty retry | 7D + 4A | If Ollama generates truncated shell and then returns empty on retry, double fallback occurs | Each fallback logs a warning; the pipeline continues with whatever code was generated |
| Anti-fabrication + RAG context | 5B + 9F | RAG injects real code snippets that the auditor sees — could reduce false fabrication alerts | This is beneficial: the auditor has more context to judge whether the agent's output matches reality |

---

## File Change Inventory

| File | Phases | Lines Changed (approx) |
|------|--------|----------------------|
| `config.py` | 9A | +10 (RAG constants) |
| `main.py` | 4B | +15 (startup test) |
| `brain/graph.py` | 7A, 7B | +20 (timing, summary) |
| `brain/nodes/planner.py` | 7C, 9F | +89/-72 (RAG integration) |
| `brain/nodes/executor.py` | 7D | +15 (shell truncation) |
| `brain/nodes/auditor.py` | 5A, 5B | +30 (fabrication detection) |
| `brain/nodes/deliverer.py` | 0B, 8A | +20 (deploy fix, path sanitisation) |
| `tools/sandbox.py` | 1A-C, 2A-B, 5C, 6A | +120 (server, scanner, allowlist) |
| `tools/model_router.py` | 4A | +10 (empty retry) |
| `tools/rag.py` | 9B-E | +310 (NEW — entire RAG module) |
| `bot/handlers.py` | 0A, 0C, 1C, 3A-B, 9G | +70 (fixes, chain, reindex) |
| `bot/telegram_bot.py` | 9G | +4 (reindex registration) |
| `requirements.txt` | 9 | +2 (lancedb) |
| `tests/test_v8_context.py` | 9 | +11 (RAG_ENABLED patches) |
| `tests/test_v8_remediation.py` | 7, 8, 9 | +46/-46 (updated tests) |
| `tests/test_rag.py` | 9 | +353 (NEW — 21 tests) |

---

## Ultimate Test Suite Readiness Assessment

### Tests That Should Pass Unchanged (60/68)

All Tier 1-2, Tier 4-16 tests should behave as documented. The RAG layer is invisible to end users — it improves file injection quality but doesn't change the pipeline's external behaviour.

### Tests With Changed Expectations (3/68)

| Test | Expected Change | Why |
|------|----------------|-----|
| **3.9** (Subprocess Blocking) | `subprocess.run(["ls", "-la"])` now **PASSES** scanner (safe command) | Phase 6A allowlist. `subprocess.Popen` with safe args also passes. Only unsafe commands are blocked. |
| **2.4** (Debug Sidecar) | Path sanitisation now active in debug output | Phase 8A. Paths show `~/` instead of `/Users/agentruntime1/`. |
| **7.2** (Project Memory) | RAG context injected alongside LESSONS LEARNED | Phase 9F. The planner prompt now includes semantically relevant code chunks in addition to memory patterns. |

### Tests That Require Pre-Suite Setup (5/68)

| Test | Requirement | Command |
|------|-------------|---------|
| All project tests (14.x) | `nomic-embed-text` model pulled | `ollama pull nomic-embed-text` |
| All project tests (14.x) | `lancedb` installed | `pip install lancedb>=0.6.0` |
| 13.1, 13.2 | Docker enabled | `DOCKER_ENABLED=true` in `.env` |
| 12.1-12.3 | Playwright installed | `VISUAL_CHECK_ENABLED=true` in `.env` |
| 10.1-10.4 | Deploy configured | `DEPLOY_ENABLED=true` in `.env` |

### Pre-Suite Checklist

1. **Commit Phase 9** — currently unstaged
2. **Install new dependency:** `pip install lancedb>=0.6.0`
3. **Pull embedding model:** `ollama pull nomic-embed-text`
4. **Run full test suite:** `pytest tests/ -v -k "not docker"` — expect 602+ passing
5. **Update CLAUDE.md** — bump version to v8.7.0, update test counts, add RAG to feature list
6. **Push to remote** (when ready)

### Risk Summary

| Risk Level | Count | Description |
|------------|-------|-------------|
| Low | 7 phases | Phases 0, 1, 3, 4, 7, 8 — isolated changes, well-tested |
| Medium | 2 phases | Phase 2 (AST scanner false positives), Phase 5 (fabrication detection prompt cost) |
| High | 1 phase | Phase 9 (new dependency, new module, Ollama model requirement, index management) |

Phase 9 carries the most risk because it introduces a new external dependency (LanceDB), requires a new Ollama model, and rewrites the file injection path. However, every failure mode degrades gracefully to the legacy single-attempt selector, so **no task will crash** — the worst case is slightly worse file injection quality than pre-Phase-7 behaviour.
