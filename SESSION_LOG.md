# AgentSutra — Session Log

Append entries using this format:

### YYYY-MM-DD — <one-line task summary>
- **Done**: what was completed
- **Decisions**: architectural or technical choices made, and why
- **Next**: open items or follow-ups

Keep entries concise. Do not delete old entries.

---

<!-- Claude appends entries below this line -->

### 2026-03-07 — Third-pass security hardening (v8.5.2)
- **Done**: 37 security findings fixed across 6 root causes (code scanner gaps, start_server bypass, LLM output trust, audit prompt injection, handler validation, resource housekeeping). All docs updated.
- **Decisions**: Grouped 37 findings into 6 root causes for systematic fixes. Added 12 new code scanner patterns. XML-delimited audit prompts. Midnight-based budget cutoffs.
- **Next**: Improvements report implementation (Justfile, RAG, partial results, cost analytics)

### 2026-03-08 — Improvements implementation (v8.6.0)
- **Done**: Implemented Phases 1-3 and 5 from IMPLEMENTATION_PLAN.md:
  - Phase 1: Temporal window 30min→2hr, Justfile, session log rotation
  - Phase 2: Pre-commit hooks, GitHub Actions CI, enhanced Claude commands
  - Phase 3A: Cost analytics — 7-day daily breakdown, model breakdown, budget remaining
  - Phase 3B: Partial result preservation — task_state + last_completed_stage in DB, persisted after each pipeline node
  - Phase 3C: Stage timing exposure — pipeline perf stats in /health, stage durations in /status
  - Phase 3D: Launchd service plist + install script
  - Phase 3E: /retry command — re-run failed tasks
  - Phase 3F: /setup command — system configuration validation
  - Phase 5: Budget warning at >80% utilization
- **Decisions**: Skipped Phase 4 (RAG — 2-day effort, saved for dedicated session) and Phase 6 (health endpoint — optional, depends on launchd deployment). Budget degradation uses existing 70% Ollama escalation, added 80% user warning.
- **Next**: RAG context layer (Phase 4), end-to-end testing on Mac Mini, launchd deployment

<!-- session ended: 2026-03-08 17:43 -->

<!-- session ended: 2026-03-08 17:45 -->

<!-- session ended: 2026-03-08 17:46 -->

<!-- session ended: 2026-03-08 17:48 -->

<!-- session ended: 2026-03-08 17:49 -->

<!-- session ended: 2026-03-08 17:50 -->

<!-- session ended: 2026-03-08 17:53 -->

<!-- session ended: 2026-03-08 17:54 -->

<!-- session ended: 2026-03-08 17:56 -->

<!-- session ended: 2026-03-08 18:01 -->

<!-- session ended: 2026-03-08 18:03 -->

<!-- session ended: 2026-03-08 18:04 -->

<!-- session ended: 2026-03-08 18:05 -->

<!-- session ended: 2026-03-08 18:05 -->

<!-- session ended: 2026-03-08 18:08 -->

<!-- session ended: 2026-03-08 18:11 -->

<!-- session ended: 2026-03-08 18:13 -->

<!-- session ended: 2026-03-08 18:14 -->

<!-- session ended: 2026-03-08 18:16 -->

<!-- session ended: 2026-03-08 18:20 -->

<!-- session ended: 2026-03-08 18:24 -->

<!-- session ended: 2026-03-08 18:24 -->

<!-- session ended: 2026-03-08 18:29 -->

<!-- session ended: 2026-03-08 18:30 -->

<!-- session ended: 2026-03-08 18:31 -->

<!-- session ended: 2026-03-08 18:45 -->

<!-- session ended: 2026-03-08 18:47 -->

<!-- session ended: 2026-03-08 18:49 -->

<!-- session ended: 2026-03-08 18:49 -->

<!-- session ended: 2026-03-08 18:50 -->

<!-- session ended: 2026-03-08 18:52 -->

<!-- session ended: 2026-03-08 18:53 -->

<!-- session ended: 2026-03-08 18:54 -->

<!-- session ended: 2026-03-08 19:02 -->

<!-- session ended: 2026-03-08 19:07 -->

<!-- session ended: 2026-03-08 19:20 -->

### 2026-03-08 — v8.7.0 implementation verification and docs update
- **Done**: Verified all 31 sub-phases (0A-9H) against actual code. 29 complete, 1 partial (0A retry message), 1 deliberate deviation (7C single-attempt). Ran full test suite: 726 passed, 11 skipped, 0 failed. Updated CLAUDE.md, README.md, CODEBASE_REFERENCE.md, USECASES.md for v8.7.0.
- **Decisions**: Did not fix 0A partial (retry "Completed" message) — user instructed review-only, no source changes. Flagged config.py VERSION still "8.6.0" as blocker.
- **Next**: Bump config.py VERSION to "8.7.0", push to remote, run Ultimate_Test_Suite.md

<!-- session ended: 2026-03-08 19:40 -->

<!-- session ended: 2026-03-08 19:49 -->

<!-- session ended: 2026-03-08 20:00 -->

<!-- session ended: 2026-03-08 20:12 -->

<!-- session ended: 2026-03-08 20:15 -->

<!-- session ended: 2026-03-08 20:17 -->

<!-- session ended: 2026-03-08 20:20 -->

<!-- session ended: 2026-03-08 20:21 -->

<!-- session ended: 2026-03-08 20:25 -->

<!-- session ended: 2026-03-08 20:29 -->

<!-- session ended: 2026-03-08 20:31 -->

<!-- session ended: 2026-03-08 20:39 -->

<!-- session ended: 2026-03-08 20:47 -->

<!-- session ended: 2026-03-08 20:53 -->

<!-- session ended: 2026-03-08 20:58 -->

<!-- session ended: 2026-03-08 21:12 -->

<!-- session ended: 2026-03-08 22:23 -->

<!-- session ended: 2026-03-08 22:32 -->

<!-- session ended: 2026-03-08 22:49 -->

<!-- session ended: 2026-03-08 22:54 -->

<!-- session ended: 2026-03-08 23:03 -->

<!-- session ended: 2026-03-08 23:06 -->

<!-- session ended: 2026-03-08 23:09 -->

<!-- session ended: 2026-03-08 23:13 -->

<!-- session ended: 2026-03-08 23:16 -->

<!-- session ended: 2026-03-08 23:18 -->

<!-- session ended: 2026-03-08 23:18 -->

<!-- session ended: 2026-03-08 23:22 -->

<!-- session ended: 2026-03-08 23:24 -->

<!-- session ended: 2026-03-08 23:27 -->

<!-- session ended: 2026-03-08 23:29 -->

<!-- session ended: 2026-03-08 23:31 -->

<!-- session ended: 2026-03-08 23:35 -->

<!-- session ended: 2026-03-08 23:37 -->

<!-- session ended: 2026-03-08 23:40 -->

<!-- session ended: 2026-03-08 23:42 -->

<!-- session ended: 2026-03-08 23:45 -->

<!-- session ended: 2026-03-08 23:46 -->

<!-- session ended: 2026-03-08 23:47 -->

<!-- session ended: 2026-03-08 23:51 -->

<!-- session ended: 2026-03-08 23:52 -->

<!-- session ended: 2026-03-08 23:52 -->

<!-- session ended: 2026-03-08 23:55 -->

<!-- session ended: 2026-03-08 23:55 -->

<!-- session ended: 2026-03-09 00:03 -->

<!-- session ended: 2026-03-09 00:04 -->

<!-- session ended: 2026-03-09 00:06 -->

<!-- session ended: 2026-03-09 00:09 -->

<!-- session ended: 2026-03-09 00:10 -->

<!-- session ended: 2026-03-09 00:12 -->

<!-- session ended: 2026-03-09 00:13 -->

<!-- session ended: 2026-03-09 00:17 -->

<!-- session ended: 2026-03-09 00:18 -->

<!-- session ended: 2026-03-09 00:19 -->

<!-- session ended: 2026-03-09 00:21 -->

<!-- session ended: 2026-03-09 00:22 -->

<!-- session ended: 2026-03-09 00:25 -->

<!-- session ended: 2026-03-09 00:26 -->

<!-- session ended: 2026-03-09 00:27 -->

### 2026-03-09 — v8.8.0 implementation summary + documentation update
- **Done**: Created `IMPLEMENTATION_SUMMARY.md` with all 6 sections (status table, phase details, not-implemented items, future suggestions, strengths/limitations/evolution, test coverage delta). Verified all 14 phases against actual code: 13/14 implemented, 1 skipped (Phase 8), 2 deviated (Phase 11 index-time filter, Phase 13 polling loop). Updated CLAUDE.md, README.md, CODEBASE_REFERENCE.md, USECASES.md, Ultimate_Test_Suite.md with version, line counts, field counts, test counts, known issues, roadmap.
- **Decisions**: VERSION in config.py intentionally NOT bumped (documented as blocker in summary). Tests 17.7-17.10 relabeled from [NEW v8.7] to [NEW v8.8] in Ultimate_Test_Suite.md since they test v8.8.0 features.
- **Next**: Bump config.py VERSION to "8.8.0", push to remote

<!-- session ended: 2026-03-09 00:50 -->

<!-- session ended: 2026-03-09 00:52 -->

<!-- session ended: 2026-03-09 10:29 -->

<!-- session ended: 2026-03-09 11:32 -->

<!-- session ended: 2026-03-09 11:53 -->

<!-- session ended: 2026-03-09 11:56 -->

<!-- session ended: 2026-03-09 11:58 -->

<!-- session ended: 2026-03-09 12:04 -->

<!-- session ended: 2026-03-09 12:12 -->

<!-- session ended: 2026-03-09 12:16 -->

<!-- session ended: 2026-03-09 12:21 -->

<!-- session ended: 2026-03-09 18:27 -->

<!-- session ended: 2026-03-09 22:47 -->

<!-- session ended: 2026-03-09 23:05 -->

<!-- session ended: 2026-03-09 23:12 -->

<!-- session ended: 2026-03-09 23:14 -->

<!-- session ended: 2026-03-09 23:18 -->

<!-- session ended: 2026-03-09 23:33 -->

<!-- session ended: 2026-03-09 23:47 -->

<!-- session ended: 2026-03-09 23:53 -->

<!-- session ended: 2026-03-09 23:55 -->

<!-- session ended: 2026-03-09 23:58 -->

<!-- session ended: 2026-03-10 00:00 -->

<!-- session ended: 2026-03-10 00:01 -->

<!-- session ended: 2026-03-10 00:03 -->

### 2026-03-10 — Phase 0c: LONG_TIMEOUT increase to 1800s
- **Done**: Changed `config.py:78` LONG_TIMEOUT default from 900 to 1800. Updated CLAUDE.md config table.
- **Decisions**: 1800s covers longest observed task (1440s BTC dashboard) with 25% margin. Can reduce after Phase 0a improves planning latency.
- **Next**: Phases 0a, 0b, 0d (remaining P0 quick wins)

<!-- session ended: 2026-03-10 00:11 -->

### 2026-03-10 — Phase 0a: Purpose-dependent Ollama model routing
- **Done**: Added `OLLAMA_CLASSIFY_MODEL` config (default `qwen2.5:7b`). Updated `_select_model()` to route classify to qwen, plan to deepseek. 4 new tests + 2 existing test fixes. Updated CLAUDE.md file map, config table, env vars.
- **Decisions**: qwen2.5:7b chosen for classify — ~4.5GB vs ~9GB for deepseek-r1:14b, no `<think>` blocks, ~6-10s vs 30-55s latency. Plan stays on deepseek for reasoning quality.
- **Next**: Phases 0b, 0d (remaining P0 quick wins), then P1 sequential phases

<!-- session ended: 2026-03-10 00:22 -->

### 2026-03-10 — Phase 0d: Refine plan complexity routing
- **Done**: Changed `planner.py:266` so only `frontend`, `ui_design`, `data` get `complexity="high"` (Sonnet); `code`, `automation`, `file`, `project` get `"low"` (Ollama-eligible). 3 new tests in `test_v8_context.py`. Updated CLAUDE.md pipeline table.
- **Decisions**: Project tasks already ran at low complexity; the real change is `code`/`automation`/`file` dropping from high→low. These tasks produce straightforward plans that don't need Sonnet-level reasoning.
- **Next**: Phases 0b, 1-9 (remaining v9.0.0 phases)

<!-- session ended: 2026-03-10 01:12 -->

### 2026-03-10 — Phase 0b: Classifier trigger context-awareness
- **Done**: Added `_MENTION_CONTEXTS` set and context-exclusion logic to `match_project()` in `tools/projects.py`. Triggers after words like "about", "for", "featuring", "including" are skipped. Checks last 3 prefix words (not just last) to handle multi-word triggers like "for Affiliate Job Scraper". Created `tests/test_projects.py` with 7 tests. Updated CLAUDE.md file map, test counts.
- **Decisions**: Extended prefix check from last-word-only to last-3-words to handle cases where shorter sub-triggers ("job scraper") would bypass context exclusion when a longer trigger ("affiliate job scraper") was correctly blocked.
- **Next**: Phases 1-9 (remaining v9.0.0 phases)

<!-- session ended: 2026-03-10 08:35 -->

<!-- session ended: 2026-03-10 09:13 -->
