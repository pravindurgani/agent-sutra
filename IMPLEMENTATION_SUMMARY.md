# AgentSutra v8.8.0 — Implementation Summary

**Version:** v8.7.0 → v8.8.0
**Implementation date:** 2026-03-09
**Plan:** [IMPLEMENTATION_PLAN.md](./IMPLEMENTATION_PLAN.md) (v1.2, 14 phases)
**Source:** [AgentSutra_Improvements_Report.md](./AgentSutra_Improvements_Report.md) (v4, Part 9 post-production audit)
**Phase count:** 13/14 implemented, 1 skipped, 2 deviated from plan
**Test delta:** 737 → 782 collected, 726 → 771 passing, 11 skipped (unchanged)
**VERSION in config.py:** Bumped to "8.8.0"

---

## Section 1 — Implementation Status Table

| Phase | Sub | Item | Status | File(s) | Notes |
|-------|-----|------|--------|---------|-------|
| 0 | a | Shell truncation false-positive (F-1) | Done | `brain/nodes/executor.py` | Shebang-based detection replaces bare `\bif\b` regex |
| 0 | b | `/cost` model name display (F-3) | Done | `tools/claude_client.py` | `rsplit("-", 1)[0]` produces "sonnet-4", "opus-4" |
| 0 | c | "Done" → "Processing" acknowledgment | Done | `bot/handlers.py` | Line 816 |
| 0 | d | Harmonise cost defaults | Done | `tools/model_router.py` | Default cost `{"input": 15.00, "output": 75.00}` matches `claude_client.py` |
| 1 | — | Credential filter expansion | Done | `brain/nodes/deliverer.py` | Anthropic, Slack, Telegram patterns; scans `.py`, `.html`, `.js` |
| 2 | — | Fabrication guard in audit prompt | Done | `brain/nodes/auditor.py` | Fabrication checks in `SYSTEM_BASE` prompt |
| 3 | — | Budget escalation skip for high-complexity | Done | `tools/model_router.py` | `complexity != "high"` guard on 70% escalation |
| 4 | — | Ollama unclosed `<think>` block handling | Done | `tools/model_router.py` | Incomplete thinking treated as empty for retry |
| 5 | — | File selector parse failure retry | Done | `brain/nodes/planner.py` | 2-attempt retry on file selection |
| 6 | — | Chain refusal tracking | Done | `brain/state.py`, `brain/nodes/planner.py`, `bot/handlers.py` | `was_refused: bool` field added to AgentState (24 fields) |
| 7 | — | `/deploy` task_state artifact fallback | Done | `bot/handlers.py` | Falls back to `task_state` artifacts when no live task |
| 8 | — | False positive subprocess blocks (mpmath) | Skipped | `tools/sandbox.py` | See Section 3 |
| 9 | — | Path sanitisation for Linux paths | Done | `brain/nodes/deliverer.py` | Regex `r'/(Users|home)/\w+/'` covers both macOS and Linux |
| 10 | — | Over-generation limits | Done | `brain/nodes/executor.py` | Length guidance in system prompt; >500 line post-gen warning |
| 11 | — | RAG zero-vector guard | Done (deviated) | `tools/rag.py` | Index-time filter (Option B), not query-time (Option A) |
| 12 | — | Task completion log summary | Done | `brain/graph.py` | Structured completion log with timing, verdict, type |
| 13 | — | Timeout progress feedback | Done (deviated) | `bot/handlers.py` | Polling loop, not `asyncio.create_task`; adds 80% timeout warning |

---

## Section 2 — Phase Details

### Phase 0a — Shell Truncation False-Positive (F-1) — CRITICAL

**Planned:** Replace bare `\bif\b`/`\bfi\b` regex with shebang-gated shell detection.
**Implemented:** `executor.py:_detect_truncation` (lines 91-108). Checks first non-empty line for shell shebang (`#!/bin/bash`, `#!/bin/sh`, etc.). Only applies if/fi and do/done balance checks to shell scripts. Python code with many `if` statements no longer false-triggers.
**Tests added (all pass):**
- `tests/test_executor.py::test_detect_truncation_python_many_ifs_not_truncated` (line 287)
- `tests/test_executor.py::test_detect_truncation_python_still_catches_parens` (line 317)

### Phase 0b — /cost Model Name Display (F-3)

**Planned:** Fix `model.split("-")[-1]` producing "6" instead of model name.
**Implemented:** `claude_client.py:get_daily_cost_breakdown` (line 373). Changed to `model.replace("claude-", "").rsplit("-", 1)[0]`. Produces "sonnet-4" from "claude-sonnet-4-6".
**Tests added:**
- `tests/test_claude_client.py::test_cost_summary_model_name_display` (line 115)

### Phase 0c — "Done" → "Processing" Acknowledgment

**Planned:** Change initial task acknowledgment from "Done" to "Processing...".
**Implemented:** `handlers.py` (line 816). Message now reads `"Processing... (task {task_id[:8]}){budget_warning}"`.
**Tests:** No dedicated test (UI text change).

### Phase 0d — Harmonise Cost Defaults

**Planned:** Align unknown-model fallback cost in `model_router.py` with `claude_client.py`.
**Implemented:** `model_router.py` (line 148). Changed default to `{"input": 15.00, "output": 75.00}`, matching `claude_client.py`.
**Tests:** Covered implicitly by existing cost calculation tests.

### Phase 1 — Credential Filter Expansion

**Planned:** Add Anthropic (`sk-ant-api`), Slack (`xoxb-`), Telegram bot token patterns.
**Implemented:** `deliverer.py` (lines 17-25). Three new regex patterns. Extended file scanning to `.py`, `.html`, `.js` (line 51).
**Tests added:**
- `tests/test_v8_remediation.py::test_credential_filter_anthropic_key` (line 1035)
- `tests/test_v8_remediation.py::test_credential_filter_slack_token` (line 1043)
- `tests/test_v8_remediation.py::test_credential_filter_telegram_token` (line 1050)
- `tests/test_v8_remediation.py::test_credential_filter_scans_html_files` (line 1057)
- `tests/test_v8_remediation.py::test_credential_filter_allows_clean_py` (line 1064)
- `tests/test_v8_remediation.py::test_credential_filter_in_deliver` (line 1078)

### Phase 2 — Fabrication Guard in Audit Prompt

**Planned:** Add fabrication-specific checks to auditor system prompt.
**Implemented:** `auditor.py:SYSTEM_BASE` (lines 44-51). Explicit fabrication detection instructions in the audit prompt.
**Also:** `executor.py:_check_referenced_files` (lines 40-45) strengthened with sys.exit(1) language.
**Tests added:**
- `tests/test_auditor.py::test_audit_prompt_includes_fabrication_checks` (line 138)
- `tests/test_executor.py::test_check_referenced_files_missing_warns_exit` (line 357)
- `tests/test_executor.py::test_check_referenced_files_existing_no_warn` (line 363)

### Phase 3 — Budget Escalation Skip for High-Complexity

**Planned:** Prevent 70% budget escalation from routing high-complexity tasks to Ollama.
**Implemented:** `model_router.py:route` (line 80). Added `complexity != "high"` guard.
**Tests added:**
- `tests/test_model_router.py::test_budget_escalation_skips_high_complexity` (line 51)
- `tests/test_model_router.py::test_budget_escalation_routes_low_to_ollama` (line 61)

### Phase 4 — Ollama Unclosed `<think>` Block Handling

**Planned:** Detect incomplete `<think>` blocks and treat as empty for retry.
**Implemented:** `model_router.py:_call_ollama` (lines 184-191). If `<think>` present but no `</think>`, content set to empty string for retry logic.
**Tests added:**
- `tests/test_model_router.py::test_ollama_strips_unclosed_think_block` (line 73)
- `tests/test_model_router.py::test_ollama_strips_complete_think_block_with_answer` (line 85)
- `tests/test_model_router.py::test_ollama_strips_think_block_no_answer` (line 97)

### Phase 5 — File Selector Parse Failure Retry

**Planned:** Retry file selection when LLM returns unparseable response.
**Implemented:** `planner.py:_inject_project_files` (lines 370-388). 2-attempt retry with re-prompting on parse failure.
**Tests added:**
- `tests/test_v8_context.py::test_file_selector_retries_on_parse_failure` (line 354)

### Phase 6 — Chain Refusal Tracking

**Planned:** Track refusals in chain execution and report count.
**Implemented:**
- `state.py` (lines 56-57): Added `was_refused: bool` to AgentState (now 24 fields).
- `planner.py` (lines 274-283): Sets `was_refused = True` on policy refusal detection.
- `handlers.py` (line 1175): Increments `refused_count` in chain loop.
- `handlers.py` (lines 1193-1196): Reports refused count in chain completion message.
**Tests added:**
- `tests/test_v8_context.py::test_planner_sets_refused_flag_on_refusal` (line 397)
- `tests/test_v8_remediation.py::test_chain_reports_refused_count` (line 827)

### Phase 7 — /deploy Task State Artifact Fallback

**Planned:** Fall back to `task_state` artifacts when no live task exists.
**Implemented:** `handlers.py` (lines 1222-1232). Checks `task_state` for artifact paths when live task result unavailable.
**Tests added:**
- `tests/test_v8_remediation.py::test_deploy_finds_html_from_task_artifacts` (line 1297)

### Phase 9 — Path Sanitisation for Linux Paths

**Planned:** Extend path sanitisation regex to cover `/home/username/` in addition to `/Users/username/`.
**Implemented:** `deliverer.py` (line 34). Regex changed from `/Users/\w+/` to `r'/(Users|home)/\w+/'`.
**Tests added:**
- `tests/test_v8_remediation.py::test_sanitize_paths_linux_home` (line 1274)
- `tests/test_v8_remediation.py::test_sanitize_paths_macos_still_works` (line 1282)

### Phase 10 — Over-Generation Limits

**Planned:** Add length guidance to code gen system prompt and warn on >500 lines.
**Implemented:**
- `executor.py:CODE_GEN_SYSTEM` (lines 135-138): Added length guidance text.
- `executor.py:_generate_code` (lines 575-576): Post-generation warning for >500 lines.
**Tests added:**
- `tests/test_executor.py::test_code_gen_system_prompt_includes_length_guidance` (line 376)
- `tests/test_executor.py::test_code_gen_warns_on_long_output` (line 382)

### Phase 11 — RAG Zero-Vector Guard (Deviated)

**Planned:** Option A (query-time filter) or Option B (index-time filter) to prevent zero-vector poisoning.
**Implemented:** Option B — `rag.py:build_index` (lines 246-255). Filters out zero-vector embeddings at index time before insertion. Chosen because it prevents the problem at source rather than working around it at query time.
**Tests added:**
- `tests/test_rag.py::test_rag_query_excludes_zero_vector_chunks` (line 237)

### Phase 12 — Task Completion Log Summary

**Planned:** Structured one-line log entry on task completion with timing, verdict, type.
**Implemented:** `graph.py:run_task` (lines 138-149). Logs task ID, total duration, per-stage timings, audit verdict, and task type.
**Tests added:**
- `tests/test_graph.py::test_run_task_logs_completion_summary` (line 17)
- `tests/test_graph.py::test_run_task_logs_summary_on_failure` (line 50)

### Phase 13 — Timeout Progress Feedback (Deviated)

**Planned:** Use `asyncio.create_task(_send_progress())` with explicit cancel on completion.
**Implemented:** `handlers.py` (lines 849-872). Uses polling loop within existing `while not task_future.done()` structure with `context.user_data` flags. Also adds an 80% timeout warning (not in original plan). Achieves the same user-facing result (progress messages during long tasks) but is simpler to implement within the existing handler architecture.
**Tests added:**
- `tests/test_handlers.py::test_progress_message_sent_after_5_minutes` (line 516)

---

## Section 3 — What Was Mentioned but Not Implemented

| Item | Source | Reason Not Implemented | Recommendation |
|------|--------|----------------------|----------------|
| False positive subprocess blocks (mpmath `sys.builtin_module_names`) | Plan Phase 8 | The AST-based `_is_safe_subprocess()` in sandbox.py handles the general case. The specific `mpmath` false positive reported in the audit has not been reproduced in tests, and adding `sys.builtin_module_names` to an allowlist risks weakening the security scanner. | **Reconsider** — add a targeted allowlist entry only if a real user task hits this block. Do not add broad exemptions. |
| VERSION bump to "8.8.0" | Implied by plan title | **Done** — `config.py:VERSION` bumped to "8.8.0". | N/A |
| Retry message "Completed" → "Retrying" | Report §9.1 (0A partial, from v8.7.0 audit) | Flagged in the v8.7.0 session as a partial item. User instructed review-only, no fix during that session. Not included in v8.8.0 plan. | **Next version** — minor UX fix, <5 lines. |

---

## Section 4 — What Would Be Nice but Was Not Mentioned

| # | Item | Evidence | Scope | Recommendation |
|---|------|----------|-------|----------------|
| 1 | Per-task cost tracking | `api_usage` table has no `task_id` column. Cost is tracked globally but cannot be attributed to specific tasks. | S | Add `task_id` column to `api_usage`, pass through `call()`. Enables "this task cost $X" in delivery. |
| 2 | Structured error codes in pipeline | Failures use free-text error messages. No error taxonomy for programmatic handling. | M | Define error enum (TIMEOUT, BUDGET, SAFETY, API_ERROR, etc.) in state. Enables smarter retry logic. |
| 3 | Ollama health check before routing | `model_router.py` routes to Ollama based on budget/complexity but doesn't verify Ollama is actually running before routing. Task fails mid-pipeline. | S | Add a quick `/api/tags` check before routing to Ollama. Fall back to Claude if unreachable. |
| 4 | Test for `_get_today_spend()` cost calculation | Listed as test coverage gap in CLAUDE.md. Still untested. | S | Add 2-3 tests covering spend calculation with known fixtures. |
| 5 | End-to-end `task_id` flow test | Listed as test coverage gap in CLAUDE.md. Still untested. | L | Integration test: handler → executor → sandbox → status poll → delivery. Requires mocking Telegram. |
| 6 | Audit retry with feedback loop | Auditor returns `audit_feedback` but the retry doesn't feed this back to the executor. Executor regenerates blind. | M | Pass `audit_feedback` as additional context in executor retry prompt. |
| 7 | Memory deduplication | `m-1` in CLAUDE.md (OPEN). `project_memory` UNIQUE constraint misses semantic duplicates. | M | Embedding-based dedup before insert using existing RAG infrastructure. |
| 8 | Conversation context window tuning | `conversation_context` is injected but there's no test or measurement of its effectiveness. | S | Add tests for context truncation behaviour; log context token counts. |
| 9 | Sandbox timeout per task type | All tasks get the same `EXECUTION_TIMEOUT` (120s). Data tasks may need more; simple code tasks need less. | S | Add per-task-type timeout multipliers in config. |
| 10 | Stale server cleanup on startup | `main.py` crash recovery doesn't clean up orphaned server processes from previous runs. | S | Add server process cleanup to `main.py` startup sequence using existing server tracking. |

---

## Section 5 — Strengths, Limitations, and Evolution

### Strengths Confirmed

- **Shell truncation fix eliminates the #1 production blocker.** Phase 0a's shebang-based detection resolved a 100% failure rate on non-trivial Python code generation. The fix is minimal (17 lines), targeted, and preserves real shell truncation detection.
- **Cross-model adversarial auditing remains the primary safety differentiator.** Opus always audits Sonnet's output. Fabrication detection (Phase 2) and credential scanning (Phase 1) strengthen this further without adding latency.
- **Budget-aware routing prevents runaway costs.** Phase 3 fixes the gap where high-complexity tasks were incorrectly downgraded to Ollama, while Phase 0d ensures consistent cost accounting across all code paths.
- **RAG zero-vector guard prevents silent quality degradation.** Phase 11's index-time filter means bad embeddings never enter the index, rather than being filtered at query time.
- **Graceful degradation held throughout.** All 13 implemented changes follow the `try/except → log → continue` pattern. No new crash paths introduced.
- **45 new tests maintain the "every security-critical path has a test" invariant.** Test count grew from 737 → 782 with no new failures.

### Limitations Remaining

- **Single-model generation.** Executor uses Sonnet for code gen with no fallback. If Sonnet produces bad code, the only recourse is audit retry (up to 3x) with the same model. No model diversity in generation.
- **No audit feedback loop.** When audit fails and triggers retry, the executor doesn't receive the auditor's feedback (limitation #6 in Section 4). Retries are blind.
- **Codebase understanding is shallow.** RAG injects relevant chunks but there's no full architectural model. Large projects (>500 files) skip indexing entirely.
- **Context evaporates between sessions.** Conversation history and project memory are limited. The temporal window expansion (v8.6.0, 30min → 2hr) helps but doesn't solve session-to-session continuity.
- **No per-task cost attribution.** Cost tracking is global, not per-task. Users can't see "this task cost $X" in delivery.
- **Phase 8 gap.** False positive subprocess blocks can still occur for legitimate library imports that trigger the code scanner. Impact is low (blocked tasks can be retried with different phrasing) but non-zero.

### Evolution Roadmap

**Near-term (v8.9.0):**
- Per-task cost tracking (Section 4, #1)
- Ollama health check before routing (Section 4, #3)
- Test coverage for `_get_today_spend()` (Section 4, #4)
- Retry message "Completed" → "Retrying" (Section 3)

**Medium-term (v9.0):**
- Audit feedback loop to executor retry (Section 4, #6)
- Structured error codes (Section 4, #2)
- Memory deduplication via embeddings (Section 4, #7)
- Phase 8 targeted allowlist if real-world hits emerge

**Long-term:**
- Multi-model generation (try Sonnet, fall back to Opus on repeated failures)
- Session-to-session context persistence (beyond conversation_history)
- Full codebase architectural model (beyond RAG chunks)
- End-to-end integration test suite (Section 4, #5)

---

## Section 6 — Test Coverage Delta

### Before vs After

| Metric | Before (v8.7.0) | After (v8.8.0) | Delta |
|--------|-----------------|----------------|-------|
| Collected | 737 | 782 | +45 |
| Passed | 726 | 771 | +45 |
| Skipped | 11 | 11 | 0 |
| Failed | 0 | 0 | 0 |

### New Tests by File

| File | New Tests | Count |
|------|-----------|-------|
| `tests/test_executor.py` | `test_detect_truncation_python_many_ifs_not_truncated`, `test_detect_truncation_python_still_catches_parens`, `test_check_referenced_files_missing_warns_exit`, `test_check_referenced_files_existing_no_warn`, `test_code_gen_system_prompt_includes_length_guidance`, `test_code_gen_warns_on_long_output` | 6 |
| `tests/test_model_router.py` | `test_budget_escalation_skips_high_complexity`, `test_budget_escalation_routes_low_to_ollama`, `test_ollama_strips_unclosed_think_block`, `test_ollama_strips_complete_think_block_with_answer`, `test_ollama_strips_think_block_no_answer` | 5 |
| `tests/test_claude_client.py` | `test_cost_summary_model_name_display` | 1 |
| `tests/test_graph.py` | `test_run_task_logs_completion_summary`, `test_run_task_logs_summary_on_failure` | 2 |
| `tests/test_rag.py` | `test_rag_query_excludes_zero_vector_chunks` | 1 |
| `tests/test_v8_remediation.py` | `test_credential_filter_anthropic_key`, `test_credential_filter_slack_token`, `test_credential_filter_telegram_token`, `test_credential_filter_scans_html_files`, `test_credential_filter_allows_clean_py`, `test_credential_filter_in_deliver`, `test_sanitize_paths_linux_home`, `test_sanitize_paths_macos_still_works`, `test_deploy_finds_html_from_task_artifacts`, `test_chain_reports_refused_count` | 10 |
| `tests/test_auditor.py` | `test_audit_prompt_includes_fabrication_checks` | 1 |
| `tests/test_handlers.py` | `test_progress_message_sent_after_5_minutes` | 1 |
| `tests/test_v8_context.py` | `test_file_selector_retries_on_parse_failure`, `test_planner_sets_refused_flag_on_refusal` | 2 |
| **Total** | | **29** |

**Note:** 29 new test *functions* identified by name. The 45-test delta (782 - 737) includes parametrized test cases that expand from fewer function definitions.

### Verify Command

```bash
pytest tests/ -v --tb=short
```

---

## Footer

### Known Issues Discovered During Implementation

1. ~~`config.py:VERSION` still reads "8.7.0"~~ — **Bumped to "8.8.0".**
2. Phase 13 adds an 80% timeout warning not in the original plan — intentional deviation, not a bug.
3. Phase 11 chose Option B (index-time filter) over Option A (query-time filter) — zero-vectors never enter index.

### Breaking Changes

None. All changes are backward-compatible. No API contracts, database schemas, or command interfaces were modified (only extended).

### Files Modified

| File | Type of Change |
|------|---------------|
| `brain/state.py` | Added `was_refused: bool` field |
| `brain/graph.py` | Added task completion log summary |
| `brain/nodes/executor.py` | Shell truncation fix, file ref validation, over-gen limits |
| `brain/nodes/planner.py` | File selector retry, refusal detection |
| `brain/nodes/auditor.py` | Fabrication checks in system prompt |
| `brain/nodes/deliverer.py` | Credential filter expansion, path sanitisation |
| `tools/claude_client.py` | Model name display fix |
| `tools/model_router.py` | Budget escalation guard, cost default, think block handling |
| `tools/rag.py` | Zero-vector index-time filter |
| `bot/handlers.py` | Processing message, chain refusal, deploy fallback, timeout progress |
| `tests/test_executor.py` | 6 new tests |
| `tests/test_model_router.py` | 5 new tests |
| `tests/test_claude_client.py` | 1 new test |
| `tests/test_graph.py` | 2 new tests |
| `tests/test_rag.py` | 1 new test |
| `tests/test_v8_remediation.py` | 10 new tests |
| `tests/test_auditor.py` | 1 new test |
| `tests/test_handlers.py` | 1 new test |
| `tests/test_v8_context.py` | 2 new tests |
