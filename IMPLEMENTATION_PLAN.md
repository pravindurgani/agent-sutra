# AgentSutra v9.0.0 — Implementation Plan

**Version:** 2.3
**Date:** 2026-03-10
**Source:** [AgentSutra_Improvements_Report.md](./AgentSutra_Improvements_Report.md) (v8.8.0 Test Suite Execution Report, 2026-03-09)
**Phases:** 11 (Phase 0–9, with Phase 4b)
**Estimated total scope:** ~360 source lines + ~520 test lines across 13 files
**Execution order:** Phase 0 first (P0 parallelisable), then Phases 1–5 sequentially (P1), then Phases 6–9 (P2 parallelisable)
**Supersedes:** v1.2 plan (v8.8.0 phases 0–13, all completed or skipped)

---

## Context

The v8.8.0 test suite execution report (48 prompts, 10 hours, Mac Mini M2) revealed 9 gaps (G-1 through G-9) and produced 8 fix recommendations (F-1 through F-8). This plan translates those findings into implementable phases with exact file locations, line numbers, code changes, and test specifications.

**Key production data driving this plan:**
- Planning latency: 65–380s actual vs 3–8s expected (8–31x slower)
- Classification latency: 30–55s actual vs 0.3–1s expected (35–115x slower)
- 111 Ollama empty responses during 10hr run (~450–660s wasted)
- 5 timeouts caused by planning bottleneck (up to 1051s single planning cycle)
- 3 false positives (PDF upload, psycopg2 subprocess, importlib)
- 2 quality failures (Tugi Tark data corruption, credential grep fabrication)
- 1 HTML truncation failure (Test 8.3, undetected across 3 retries)

---

## Execution Order Summary

```
Phase 0  (P0 quick wins — all parallelisable, do first)
  ├── 0a: Purpose-dependent Ollama model routing (F-1, G-5, G-9)
  ├── 0b: Classifier trigger context-awareness (F-2, G-2)
  ├── 0c: LONG_TIMEOUT increase to 1800s (F-6, G-7)
  └── 0d: Skip RAG for non-project tasks (G-1 partial)

Phase 1  (P1 — sequential: duplicate error early-exit)
Phase 2  (P1 — sequential: audit data sanity checks)
Phase 3  (P1 — sequential: HTML truncation detection)
Phase 4  (P1 — sequential: importlib smart allowlist)
Phase 4b (P1 — sequential: shutil.rmtree scanner hardening)
Phase 5  (P1 — sequential: executor respects was_refused)

Phase 6  (P2 — parallelisable: project pipeline arguments via YAML)
Phase 7  (P2 — parallelisable: task-type-specific audit criteria expansion)
Phase 8  (P2 — parallelisable: ARCHITECTURE.md per-project convention)
Phase 9  (P2 — parallelisable: Ollama health monitoring)
```

**Dependencies:**
- Phase 0a must complete before Phase 9 (model routing must be stable before adding health checks)
- All other phases are independent (Phase 2 and Phase 7 reinforce each other but neither requires the other)

---

## Phase 0a: Purpose-Dependent Ollama Model Routing

**Fixes:** F-1, G-5 (classification latency), G-9 (Ollama empty responses)
**Priority:** P0 | **Scope:** S (~35 lines) | **Risk:** Low
**Files:** `config.py`, `tools/model_router.py`

### Problem

`deepseek-r1:14b` is used for all Ollama-routed tasks. It emits `<think>` reasoning blocks (20–40s) before a one-word classification answer. It consumes ~9GB RAM on 16GB M2, leaving ~7GB headroom — insufficient under sustained load (111 empty responses observed).

### Root Cause

`config.py:90` defines a single `OLLAMA_DEFAULT_MODEL = "deepseek-r1:14b"` used everywhere.
`model_router.py:82,87` both return `config.OLLAMA_DEFAULT_MODEL` regardless of purpose.

### Changes

**config.py** — Add purpose-specific model config (after line 90):

```python
# Lines 89-90 currently:
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_DEFAULT_MODEL = os.getenv("OLLAMA_DEFAULT_MODEL", "deepseek-r1:14b")

# Replace with:
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_DEFAULT_MODEL = os.getenv("OLLAMA_DEFAULT_MODEL", "deepseek-r1:14b")
OLLAMA_CLASSIFY_MODEL = os.getenv("OLLAMA_CLASSIFY_MODEL", "qwen2.5:7b")
```

**model_router.py** — Purpose-dependent model selection in `_select_model()` (lines 67–90):

```python
def _select_model(purpose: str, complexity: str) -> tuple[str, str]:
    """Decide (provider, model) based on purpose, complexity, and resource state."""

    # Rule (a): Audit → ALWAYS Opus
    if purpose == "audit":
        return ("claude", config.COMPLEX_MODEL)

    # Rule (b): Code generation → ALWAYS Sonnet
    if purpose == "code_gen":
        return ("claude", config.DEFAULT_MODEL)

    # Rule (d): Budget escalation — check before complexity routing
    if purpose in ("classify", "plan") and complexity != "high" and _daily_spend_exceeds_threshold(0.7):
        if _ollama_available() and _ram_below_threshold(90):
            model = config.OLLAMA_CLASSIFY_MODEL if purpose == "classify" else config.OLLAMA_DEFAULT_MODEL
            return ("ollama", model)

    # Rule (c): Low-complexity classify/plan → try Ollama
    if purpose in ("classify", "plan") and complexity == "low":
        if _ollama_available() and _ram_below_threshold(75):
            model = config.OLLAMA_CLASSIFY_MODEL if purpose == "classify" else config.OLLAMA_DEFAULT_MODEL
            return ("ollama", model)

    # Rule (e): Default → Sonnet
    return ("claude", config.DEFAULT_MODEL)
```

### Expected Impact

| Metric | Before | After |
|--------|--------|-------|
| Classify latency (Ollama) | 30–55s | ~6–10s |
| Ollama RAM usage (classify) | ~9GB | ~4.5GB |
| Free headroom on 16GB M2 | ~7GB | ~11.5GB |
| Empty response rate | 111/run | Significantly reduced (RAM pressure relieved) |

**Ollama empty response correlation:** Log analysis shows ~70/94 non-retry empty responses occur during classification phase. The remaining ~24 are retries of classify-phase calls. ~99% of empty responses are classification-time. This strongly supports switching classify to `qwen2.5:7b` (less RAM) as the primary fix for empty response rate, since the problem is almost entirely concentrated in the phase this change targets.

### Pre-Requisite

Run `ollama pull qwen2.5:7b` on Mac Mini before deploying. Verify with `ollama list`.

### Tests to Add

```
tests/test_model_router.py:
  test_classify_routes_to_qwen_7b()
    — Mock Ollama available + RAM OK → assert model == "qwen2.5:7b" for purpose="classify"
  test_plan_still_routes_to_deepseek()
    — Mock Ollama available + RAM OK → assert model == "deepseek-r1:14b" for purpose="plan"
  test_budget_escalation_uses_qwen_for_classify()
    — Mock spend > 70% + Ollama available → assert classify model == "qwen2.5:7b"
  test_config_ollama_classify_model_env_override()
    — Set OLLAMA_CLASSIFY_MODEL env → assert config reads it
```

### Verification

After deployment, run 5 classification tasks and check `agentsutra.log` for:
```
Routed classify (complexity=low) to ollama/qwen2.5:7b
```
Confirm classify times are <15s. Monitor empty response count over next 48hr run.

---

## Phase 0b: Classifier Trigger Context-Awareness

**Fixes:** F-2, G-2 (classifier over-matching)
**Priority:** P0 | **Scope:** S (~25 lines) | **Risk:** Low
**Files:** `tools/projects.py`

**Note:** `classifier.py` calls `match_project()` from `projects.py` — the fix is entirely in `projects.py`. No changes needed in `classifier.py`.

### Problem

Test 10.1: "Design a portfolio page with cards for AgentSutra, iGaming Intelligence Dashboard" routed to the igaming project instead of generating a frontend page. The classifier's `match_project()` matches trigger keywords anywhere in the message, including inside descriptions of what the user wants to create.

### Root Cause

`projects.py:60` — `trig_lower in msg_lower` is a naive substring match with no positional context. Any mention of a project name triggers project routing.

### Changes

**tools/projects.py** — Add context-exclusion to `match_project()` (modify lines 40–70):

```python
# Phrases that indicate the trigger is being MENTIONED, not invoked
_MENTION_CONTEXTS = {"about", "for", "card", "showing", "including", "like",
                     "such as", "called", "named", "titled", "featuring"}

def match_project(message: str) -> dict | None:
    """Find best-matching project by trigger keywords in the message.

    Uses positional and contextual signals to avoid matching triggers
    that appear inside descriptive text rather than as task commands.
    """
    msg_lower = message.lower().strip()
    projects = get_projects()
    if not projects:
        return None

    best_score = 0
    best_project = None

    for project in projects:
        for trigger in project.get("triggers", []):
            trig_lower = trigger.lower().strip()
            if not trig_lower:
                continue

            if len(trig_lower) < 4:
                if not re.search(rf"\b{re.escape(trig_lower)}\b", msg_lower):
                    continue
            else:
                if trig_lower not in msg_lower:
                    continue

            # Context check: skip if trigger appears after a mention-context word
            # Find the position of the trigger in the message
            trig_pos = msg_lower.find(trig_lower)
            if trig_pos > 0:
                # Get the 30 chars before the trigger match
                prefix = msg_lower[max(0, trig_pos - 30):trig_pos].strip()
                prefix_words = prefix.split()
                if prefix_words and prefix_words[-1] in _MENTION_CONTEXTS:
                    continue

            score = len(trig_lower)
            if score > best_score:
                best_score = score
                best_project = project

    return best_project if best_project else None
```

### Edge Cases

- "Run the iGaming Intelligence Dashboard" → trigger at position 8, prefix "run the" → no mention-context → MATCH (correct)
- "Design a card about iGaming Intelligence Dashboard" → prefix "about" → SKIP (correct)
- "Create a portfolio page with iGaming Intelligence Dashboard" → prefix "with" → MATCH (acceptable — "with" is not in `_MENTION_CONTEXTS`)
- If "with" causes false positives in practice, add it to `_MENTION_CONTEXTS` later

### Tests to Add

```
tests/test_projects.py:
  test_match_project_does_not_match_in_description()
    — "Design a card about iGaming Intelligence Dashboard" → returns None
  test_match_project_does_not_match_after_for()
    — "Create a page for Affiliate Job Scraper" → returns None
  test_match_project_still_matches_command_position()
    — "Run the affiliate job scraper" → returns project
  test_match_project_still_matches_direct_trigger()
    — "igaming intelligence dashboard" → returns project
  test_match_project_skips_featuring_context()
    — "Build a dashboard featuring sensispend" → returns None
```

### Verification

Run Test 10.1 equivalent: "Design a portfolio page with cards for AgentSutra, iGaming Intelligence Dashboard." Confirm classifier returns `task_type: "frontend"` (not `"project"`).

---

## Phase 0c: LONG_TIMEOUT Increase

**Fixes:** F-6, G-7 (5 tasks timed out at 900s but completed in background)
**Priority:** P0 | **Scope:** XS (1 line) | **Risk:** None
**Files:** `config.py`

### Problem

5 tasks hit the 900s bot handler timeout. All 5 eventually completed (1000–1440s). The pipeline finished, but the user saw "Task timed out."

### Root Cause

`config.py:78`: `LONG_TIMEOUT = _safe_int("LONG_TIMEOUT", 900)`

900s is insufficient when a single planning cycle can take 1051s (Test 12.1).

### Changes

**config.py** line 78:

```python
# Before:
LONG_TIMEOUT = _safe_int("LONG_TIMEOUT", 900)

# After:
LONG_TIMEOUT = _safe_int("LONG_TIMEOUT", 1800)
```

### Why 1800s

- Longest observed task: 1440s (BTC dashboard). 1800s covers this with 25% margin.
- Planning bottleneck will improve after Phase 0a (qwen2.5:7b), but 1800s provides safety margin during transition.
- Can be reduced back to 1200s after Phase 0a is verified in production.

### Tests

No new tests needed. Existing timeout tests remain valid (they mock the constant).

---

## Phase 0d: Refine Plan Complexity Routing

**Fixes:** G-1 (planning latency, partial — cost reduction + Ollama routing for simple tasks)
**Priority:** P0 | **Scope:** XS (1 source line) | **Risk:** Low
**Files:** `brain/nodes/planner.py`

### Problem

All non-project tasks currently route to Claude Sonnet with `plan_complexity="high"` (line 266). This means simple code/file/automation tasks always use Sonnet for planning even when Ollama could handle them — wasting API budget and preventing local routing.

**Note:** Test 12.1 (SaaS pricing page, 1051s planning) is NOT caused by this. That task is `ui_design` with `use_thinking=True` (line 264), and the 1051s is Sonnet thinking overhead — not Ollama or RAG. RAG file injection already only runs for `task_type == "project"` (line 227). This phase does NOT fix Test 12.1 but reduces planning cost for simple tasks and enables Ollama routing for code/file/automation planning when budget escalation is active.

### Changes

**brain/nodes/planner.py** — Refine complexity routing (around line 264–270):

```python
# Before:
    use_thinking = task_type in ("frontend", "ui_design")
    plan_complexity = "low" if task_type == "project" else "high"

# After:
    # Thinking only for visual tasks that benefit from deep layout reasoning
    use_thinking = task_type in ("frontend", "ui_design")
    # Project tasks use known commands (low complexity). Simple code/file/automation
    # tasks don't need Opus-level reasoning. Only frontend/ui_design/data are high.
    plan_complexity = "high" if task_type in ("frontend", "ui_design", "data") else "low"
```

### Impact

- `code`, `automation`, `file`, `project` tasks now route to Ollama for planning when available (saves ~$0.01–0.03 per task + potential speed gain)
- `frontend`, `ui_design`, `data` stay on Claude Sonnet with thinking (quality preserved)
- Under budget escalation, code/automation/file tasks can plan via Ollama (faster on qwen2.5:7b than deepseek-r1:14b if Phase 0a is deployed first)

### Tests to Add

```
tests/test_planner.py:
  test_plan_complexity_code_is_low()
    — task_type="code" → plan() calls route_and_call with complexity="low"
  test_plan_complexity_frontend_is_high()
    — task_type="frontend" → plan() calls route_and_call with complexity="high"
  test_plan_complexity_project_is_low()
    — task_type="project" → plan() calls route_and_call with complexity="low"
```

---

## Phase 1: Duplicate Error Detection in Retry Loop

**Fixes:** F-3, G-3 (3 identical failures waste 5–10 min)
**Priority:** P1 | **Scope:** S (~25 lines) | **Risk:** Low
**Files:** `brain/graph.py`, `brain/state.py`

### Problem

Tests 7.2a/7.2b: `run_pipeline.py` produced the same "usage error" on all 3 attempts. Each retry burned 155s+ in planning, generating the same wrong command. Total waste: ~465s per attempt.

### Root Cause

`graph.py:62–70` — `should_retry()` only checks `audit_verdict` and `retry_count`. It does not compare current error to previous errors.

### Changes

**brain/state.py** — Add field (after line 57):

```python
    was_refused: bool                          # v8.8: planner refusal tracking
    previous_audit_feedback: str               # v9.0: duplicate error detection
```

**brain/graph.py** — Modify `should_retry()` (lines 62–70):

```python
def should_retry(state: AgentState) -> str:
    """Decide whether to retry execution or deliver the result.

    Early-exits if the current audit feedback matches the previous attempt's
    feedback (first 150 chars), preventing identical retries on unrecoverable errors.
    """
    if state.get("audit_verdict") == "pass":
        return "deliver"
    if state.get("retry_count", 0) >= config.MAX_RETRIES:
        logger.warning("Max retries reached for task %s", state["task_id"])
        return "deliver"

    # Duplicate error detection: if same feedback as last attempt, stop retrying
    current_feedback = (state.get("audit_feedback") or "")[:150]
    previous_feedback = (state.get("previous_audit_feedback") or "")[:150]
    if current_feedback and previous_feedback and current_feedback == previous_feedback:
        logger.warning(
            "Duplicate audit feedback for task %s — aborting retries (was: %.80s)",
            state["task_id"], current_feedback,
        )
        return "deliver"

    logger.info("Retrying task %s (attempt %d)", state["task_id"], state.get("retry_count", 0))
    return "plan"
```

**brain/nodes/auditor.py** — Store previous feedback before overwriting (in `audit()`, after verdict extraction ~line 230):

```python
    return {
        "audit_verdict": verdict,
        "audit_feedback": feedback,
        "previous_audit_feedback": state.get("audit_feedback", ""),
        "retry_count": state.get("retry_count", 0) + (0 if verdict == "pass" else 1),
    }
```

**brain/graph.py** — Add field to initial state in `run_task()` (line ~135):

```python
    "previous_audit_feedback": "",
```

### Why 150 Characters

- Long enough to capture the error signature (e.g., "run_pipeline.py: error: the following arguments are required: --client")
- Short enough to ignore variations in timestamps, stack trace line numbers, or retry count mentions
- Tested against actual log data: the igaming pipeline errors were identical in their first 150 chars across all 3 attempts

### Tests to Add

```
tests/test_graph.py:
  test_should_retry_exits_on_duplicate_feedback()
    — state with audit_feedback == previous_audit_feedback → returns "deliver"
  test_should_retry_continues_on_different_feedback()
    — state with different audit_feedback vs previous → returns "plan"
  test_should_retry_continues_on_first_failure()
    — state with previous_audit_feedback="" → returns "plan" (first failure always retries)
  test_should_retry_still_respects_max_retries()
    — state with retry_count=3 and different feedback → returns "deliver"
```

---

## Phase 2: Audit Data Sanity Checks

**Fixes:** F-4, G-4 (Tugi Tark 11,600% CTR not caught)
**Priority:** P1 | **Scope:** S (~20 lines) | **Risk:** Low
**Files:** `brain/nodes/auditor.py`

### Problem

The Tugi Tark report showed 0 impressions with 91,499 clicks and 11,600% CTR. Opus audit passed it. The auditor checks for fabrication (fake data created from nothing) but not for mathematical impossibility in parsed data.

### Root Cause

`auditor.py:12–51` — `SYSTEM_BASE` has no data sanity validation instructions.
`auditor.py:64–72` — `AUDIT_CRITERIA["data"]` checks for exit code, assertions, output files — not data validity.

### Changes

**auditor.py** — Add data sanity section to `SYSTEM_BASE` (after line 51, before the closing `"""`):

```python
DATA SANITY CHECK (for data analysis/reporting tasks):
- If the output contains percentages: verify they are between 0–100% (unless explicitly a growth rate or ratio)
- If the output contains rates (CTR, engagement rate, conversion rate): verify the denominator is non-zero
- If impressions = 0 but clicks > 0: FAIL (mathematically impossible)
- If any metric exceeds 1000% in a standard report context: FAIL with "data anomaly detected"
- Look for signs of column misalignment: repeated zero values where non-zero is expected, wildly inconsistent magnitudes across rows"""
```

**auditor.py** — Expand `AUDIT_CRITERIA["data"]` (lines 64–72):

```python
    "data": """
Evaluate:
1. Does the analysis correctly address the user's question?
2. Did execution succeed (exit code 0)?
3. Did all data validation assertions pass? Look for "ALL ASSERTIONS PASSED".
4. Were output files (charts, CSVs) generated?
5. Are there tracebacks or errors?
6. DATA SANITY: Are percentages between 0–100%? Are derived metrics consistent with source data (e.g., CTR = clicks/impressions, so impressions must be > 0 if clicks > 0)? Are there zero-denominator artifacts?

FAIL if: non-zero exit code, assertion failures, no output files when expected, traceback present, mathematically impossible values in output data.""",
```

### Tests to Add

```
tests/test_auditor.py:
  test_audit_catches_impossible_ctr()
    — Mock execution result with "Impressions: 0, Clicks: 91499, CTR: 11600%"
    — Assert audit returns verdict="fail" with "data anomaly" or "mathematically impossible"
  test_audit_passes_valid_data()
    — Mock execution result with "Impressions: 10000, Clicks: 500, CTR: 5.0%"
    — Assert audit returns verdict="pass"
  test_audit_catches_zero_denominator_rate()
    — Mock execution result with "Views: 0, Engagement Rate: 850%"
    — Assert audit returns verdict="fail"
```

### Caveat

This is a prompt-based fix — Opus must interpret the instructions correctly. The prompt additions are specific and use concrete examples (0 impressions + non-zero clicks) to maximise compliance. A post-execution numeric validator would be more deterministic but requires parsing arbitrary output formats — not worth the complexity for v9.0.

### Relationship to Phase 7

Phase 2 adds data sanity to `SYSTEM_BASE` (applies to ALL task types). Phase 7 adds it to `AUDIT_CRITERIA["data"]` (applies only to data tasks). For data tasks, Opus sees the instructions twice — this is deliberate belt-and-suspenders. The `SYSTEM_BASE` version uses generic language ("for data analysis/reporting tasks") providing a safety net for edge cases where `task_type` doesn't match the actual output (e.g., a `code` task that produces a data report). The `AUDIT_CRITERIA["data"]` version is more specific and is what Opus will follow for properly classified data tasks. Neither depends on the other; both can be deployed independently.

---

## Phase 3: HTML Truncation Detection

**Fixes:** F-5, G-6 (Test 8.3 truncated 3 times, undetected)
**Priority:** P1 | **Scope:** S (~15 lines) | **Risk:** Low
**Files:** `brain/nodes/executor.py`

### Problem

Test 8.3 (Multi-API daily brief): code truncated mid-HTML on all 3 retries. The truncation detector (`_detect_truncation()`, lines 49–118) checks unclosed Python parens/brackets and shell if/fi constructs. It does not check for unclosed HTML tags.

### Root Cause

`executor.py:49–118` — No HTML-aware checks. The function only handles Python syntax constructs and shebang-gated shell scripts.

### Changes

**executor.py** — Add HTML detection block in `_detect_truncation()` (after line 108, before the logging at line 110):

```python
    # HTML truncation — check for unclosed root elements
    # Only trigger for code that is generating HTML (contains DOCTYPE or <html)
    stripped_lower = stripped.lower()
    if "<!doctype" in stripped_lower or "<html" in stripped_lower:
        # Check that the HTML document is properly closed
        has_html_open = "<html" in stripped_lower
        has_html_close = "</html>" in stripped_lower
        if has_html_open and not has_html_close:
            truncated = True
            logger.warning("HTML truncation detected: <html> without </html>")

        # Also check for unclosed <script> and <style> blocks
        script_opens = stripped_lower.count("<script")
        script_closes = stripped_lower.count("</script>")
        style_opens = stripped_lower.count("<style")
        style_closes = stripped_lower.count("</style>")
        if script_opens > script_closes or style_opens > style_closes:
            truncated = True
            logger.warning(
                "HTML truncation: unclosed tags script=%d/%d style=%d/%d",
                script_opens, script_closes, style_opens, style_closes,
            )
```

### Why Only Root-Level Tags

- Checking every `<div>` vs `</div>` would create massive false positives (template literals, JSX fragments, innerHTML assignments all have unbalanced tags in Python string context)
- `<html>` without `</html>` is a definitive signal that the HTML document was cut off
- `<script>` without `</script>` catches the exact failure mode from Test 8.3 (code truncated inside a JS block within HTML)

### Tests to Add

```
tests/test_executor.py:
  test_truncation_detects_unclosed_html()
    — Code: "<!DOCTYPE html><html><head></head><body><div>" → returns True
  test_truncation_passes_complete_html()
    — Code: "<!DOCTYPE html><html><head></head><body></body></html>" → returns False
  test_truncation_detects_unclosed_script()
    — Code: '<!DOCTYPE html><html><body><script>function foo() {' → returns True
  test_truncation_detects_unclosed_style()
    — Code: "<!DOCTYPE html><html><head><style>.foo {" → returns True
  test_truncation_ignores_html_in_python_string()
    — Code: 'html = "<html>" + content + "</html>"' → returns False (balanced)
  test_truncation_no_false_positive_on_pure_python()
    — Code: 'print("hello world")' → returns False (no <!DOCTYPE or <html)
```

---

## Phase 4: importlib Smart Allowlist

**Fixes:** F-7, G-4 from previous plan (Phase 8 revival — Test 15.1 false positive)
**Priority:** P1 | **Scope:** M (~35 lines) | **Risk:** Medium (security-sensitive)
**Files:** `tools/sandbox.py`

### Problem

Test 15.1: `importlib.import_module("sys")` was blocked by Tier 4 scanner. The task (analyzing Python's built-in modules) is legitimate but uses dynamic imports — the correct tool for introspection tasks.

### Root Cause

`sandbox.py:427` — `r"\bimportlib\s*\.\s*import_module\s*\("` blocks all `importlib.import_module()` calls without inspecting the argument. `importlib.import_module("sys")` and `importlib.import_module("config")` are treated identically.

### Changes

**tools/sandbox.py** — Replace regex pattern with AST-based check (modify line 427 and add helper):

Step 1: Remove the importlib regex from `_CODE_BLOCKED_PATTERNS` (line 427):

```python
    # Line 427 — REMOVE this line:
    # (re.compile(r"\bimportlib\s*\.\s*import_module\s*\(", re.IGNORECASE), "importlib dynamic import"),
```

Step 2: Add AST-based importlib checker (after `_is_safe_subprocess()`, ~line 543):

```python
# Known-safe modules for importlib.import_module()
_IMPORTLIB_SAFE_MODULES = frozenset({
    "sys", "os", "math", "json", "re", "datetime", "pathlib", "collections",
    "itertools", "functools", "typing", "abc", "io", "string", "textwrap",
    "copy", "pprint", "enum", "dataclasses", "decimal", "fractions",
    "statistics", "random", "hashlib", "hmac", "secrets", "struct",
    "codecs", "unicodedata", "difflib", "csv", "html", "xml",
    "urllib", "http", "email", "logging", "warnings", "contextlib",
    "inspect", "dis", "ast", "token", "tokenize", "types", "builtins",
    "importlib", "pkgutil", "platform", "sysconfig", "time", "calendar",
    "operator", "numbers",
})


def _is_safe_importlib(code: str) -> bool:
    """True if ALL importlib.import_module() calls use known-safe module names.

    Uses AST parsing to extract the first argument of each call. Only allows
    string literals from the safe set. Dynamic arguments (variables, f-strings)
    are rejected.

    Args:
        code: Python source code to inspect.

    Returns:
        True if all importlib calls are safe, False if any are unsafe or unparseable.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Match importlib.import_module(...)
        if not (isinstance(func, ast.Attribute)
                and func.attr == "import_module"
                and isinstance(func.value, ast.Name)
                and func.value.id == "importlib"):
            continue
        # Must have at least one argument
        if not node.args:
            return False
        first_arg = node.args[0]
        # Only allow string literal arguments
        if not (isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str)):
            return False
        # Check the module name against safe list (base module only)
        module_base = first_arg.value.split(".")[0]
        if module_base not in _IMPORTLIB_SAFE_MODULES:
            return False
    return True
```

Step 3: Integrate into `_check_code_safety()` (~line 546):

In `_check_code_safety()`, after the `_CODE_BLOCKED_PATTERNS` loop and before the subprocess check, add:

```python
    # importlib.import_module — AST-based check (safe modules allowed, config/dotenv blocked)
    if re.search(r"\bimportlib\s*\.\s*import_module\s*\(", code, re.IGNORECASE):
        if not _is_safe_importlib(code):
            return "BLOCKED: importlib.import_module with unsafe or dynamic module name"
```

### Security Invariant

- `importlib.import_module("config")` → BLOCKED (not in safe set, exposes API keys)
- `importlib.import_module("dotenv")` → BLOCKED (not in safe set, reads .env files)
- `importlib.import_module(module_name)` → BLOCKED (dynamic argument, can't verify)
- `importlib.import_module("sys")` → ALLOWED
- `importlib.import_module("os.path")` → ALLOWED (base module "os" is in safe set)

### Tests to Add

```
tests/test_sandbox.py:
  test_importlib_allowed_for_sys()
    — Code: 'import importlib; m = importlib.import_module("sys")' → no block
  test_importlib_allowed_for_math()
    — Code: 'import importlib; m = importlib.import_module("math")' → no block
  test_importlib_blocked_for_config()
    — Code: 'import importlib; m = importlib.import_module("config")' → BLOCKED
  test_importlib_blocked_for_dotenv()
    — Code: 'import importlib; m = importlib.import_module("dotenv")' → BLOCKED
  test_importlib_blocked_for_dynamic_arg()
    — Code: 'import importlib; m = importlib.import_module(user_input)' → BLOCKED
  test_importlib_allowed_for_os_path()
    — Code: 'import importlib; m = importlib.import_module("os.path")' → no block
  test_importlib_blocked_for_requests()
    — Code: 'import importlib; m = importlib.import_module("requests")' → BLOCKED (not in safe set; normal imports should be used)
```

### Risk Notes

- **False negative risk:** A module not in `_IMPORTLIB_SAFE_MODULES` that is legitimate will be blocked. Mitigation: the safe set covers all stdlib modules used in typical introspection tasks. Third-party modules should use normal `import` statements.
- **Bypass risk:** Attacker uses `getattr(importlib, "import_module")("config")` — already blocked by `getattr(os)` pattern (line 446). But `getattr(importlib, ...)` is NOT blocked. Add a note to check this pattern in the next security audit.

---

## Phase 4b: shutil.rmtree Scanner Hardening

**Fixes:** Test 3.1 scanner gap (code with destructive rm -rf reached execution)
**Priority:** P1 | **Scope:** S (~25 lines) | **Risk:** Medium (security-sensitive)
**Files:** `tools/sandbox.py`

### Problem

Test 3.1: `rm -rf ~/Documents` task resulted in PARTIAL — code was generated, passed the code scanner, and started execution before sandbox blocked the actual rm command. The code scanner should have caught the destructive operation before execution.

### Root Cause

`sandbox.py:431` — The shutil.rmtree pattern only catches literal path arguments:
```python
(re.compile(r"shutil\.rmtree\s*\(\s*['\"]?(/|~|Path\.home)", re.IGNORECASE), "recursive delete of home/root"),
```

This misses:
- `shutil.rmtree(os.path.expanduser("~/Documents"))` — path constructed via function call
- `shutil.rmtree(target_dir)` — variable indirection
- `shutil.rmtree(Path.home() / "Documents")` — Path object with division

### Changes

**tools/sandbox.py** — Add AST-based shutil.rmtree checker (after `_is_safe_importlib()`, before `_check_code_safety()`):

```python
def _is_safe_shutil_rmtree(code: str) -> bool:
    """True if ALL shutil.rmtree() calls use safe (non-home, non-root) targets.

    Blocks:
    - Any shutil.rmtree with a dynamic/variable argument (can't verify target)
    - Any shutil.rmtree with a string literal starting with /, ~, or containing home
    - Any shutil.rmtree with os.path.expanduser or Path.home() in the argument tree

    Args:
        code: Python source code to inspect.

    Returns:
        True if all calls are safe, False if any are unsafe or unparseable.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Match shutil.rmtree(...)
        if not (isinstance(func, ast.Attribute)
                and func.attr == "rmtree"
                and isinstance(func.value, ast.Name)
                and func.value.id == "shutil"):
            continue
        if not node.args:
            return False  # No argument — can't verify
        first_arg = node.args[0]

        # Block variable arguments (can't verify target at scan time)
        if isinstance(first_arg, ast.Name):
            return False

        # Block string literals pointing to dangerous paths
        if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
            path_val = first_arg.value.strip()
            if path_val.startswith(("/", "~")) or "home" in path_val.lower():
                return False
            # Block current/parent directory wipes
            if path_val in (".", "..") or path_val.startswith(".."):
                return False

        # Block any call expression as argument (expanduser, Path.home(), etc.)
        # shutil.rmtree(some_function(...)) — can't verify what it returns
        if isinstance(first_arg, ast.Call):
            return False

        # Block Path operations: Path.home() / "x", Path("/") / "x"
        if isinstance(first_arg, ast.BinOp):
            return False  # Path division — can't verify safely

    return True
```

**tools/sandbox.py** — Integrate into `_check_code_safety()`:

In the pattern loop section, after the existing regex-based shutil.rmtree check, add AST-based fallback:

```python
    # AST-based shutil.rmtree check — catches variable indirection and function calls
    if "shutil" in code and "rmtree" in code:
        if not _is_safe_shutil_rmtree(code):
            return "BLOCKED: shutil.rmtree with unsafe or unverifiable target path"
```

### Security Invariant

- `shutil.rmtree("/tmp/workspace/output")` → BLOCKED (starts with `/`)
- `shutil.rmtree("./output")` → ALLOWED (relative path, within workspace)
- `shutil.rmtree(".")` → BLOCKED (current directory wipe)
- `shutil.rmtree("..")` → BLOCKED (parent directory wipe)
- `shutil.rmtree("../sibling")` → BLOCKED (starts with `..`)
- `shutil.rmtree(target_dir)` → BLOCKED (variable, can't verify)
- `shutil.rmtree(os.path.expanduser("~/Documents"))` → BLOCKED (call expression)
- `shutil.rmtree(Path.home() / "Documents")` → BLOCKED (BinOp)

**Note:** The existing regex pattern at `sandbox.py:431` is preserved as the first line of defense for literal paths. The AST check is an addition that catches what the regex misses (variable indirection, function calls, `..` traversal). Both coexist — do NOT remove line 431.

### Tests to Add

```
tests/test_sandbox.py:
  test_rmtree_blocked_with_expanduser()
    — Code: 'import shutil, os; shutil.rmtree(os.path.expanduser("~/Documents"))' → BLOCKED
  test_rmtree_blocked_with_variable()
    — Code: 'import shutil; target="/home/user"; shutil.rmtree(target)' → BLOCKED
  test_rmtree_blocked_with_path_home()
    — Code: 'import shutil; from pathlib import Path; shutil.rmtree(Path.home() / "docs")' → BLOCKED
  test_rmtree_blocked_current_dir()
    — Code: 'import shutil; shutil.rmtree(".")' → BLOCKED
  test_rmtree_blocked_parent_traversal()
    — Code: 'import shutil; shutil.rmtree("../other")' → BLOCKED
  test_rmtree_allowed_relative_path()
    — Code: 'import shutil; shutil.rmtree("./output")' → ALLOWED
  test_rmtree_blocked_absolute_path()
    — Code: 'import shutil; shutil.rmtree("/tmp/data")' → BLOCKED
```

---

## Phase 5: Executor Respects `was_refused`

**Fixes:** Test 4.1 planner/executor disconnect
**Priority:** P1 | **Scope:** XS (~8 lines) | **Risk:** Low
**Files:** `brain/nodes/executor.py`

### Problem

Test 4.1: planner said "I'll refuse this task" (`was_refused=True`), but the executor still generated and ran code (`while True: pass`). The sandbox timeout killed it, but code should never have been generated.

### Root Cause

`executor.py` does not check `state["was_refused"]` before generating code. The field was added in v8.8.0 but only used in `handlers.py` for chain refusal counting.

### Changes

**brain/nodes/executor.py** — Add early return at the top of `execute()` function:

```python
def execute(state: AgentState) -> dict:
    """Execute the plan by generating and running code."""

    # If planner refused the task, skip code generation and force immediate delivery.
    # Set retry_count = MAX_RETRIES to prevent wasted audit-retry cycles.
    # Without this, the auditor would see empty code → return verdict="fail"
    # ("code doesn't address the request") → should_retry() loops back to plan
    # → planner refuses again → 3 wasted cycles of ~60s each.
    if state.get("was_refused"):
        logger.info("Skipping execution — planner refused task %s", state["task_id"])
        return {
            "execution_result": "Task was refused by the planner on policy grounds. No code generated.",
            "code": "",
            "retry_count": config.MAX_RETRIES,  # Force skip to delivery
        }

    # ... rest of existing execute() logic
```

### Why `retry_count = MAX_RETRIES`

The auditor has NO special handling for empty code or refused tasks. When executor returns `code=""` and `execution_result="Task was refused..."`:

1. Auditor sends to Opus: plan says "refuse", code is empty, execution says refused
2. Opus returns `verdict="fail"` ("code doesn't address the request")
3. `should_retry()` sees fail + retry_count < MAX_RETRIES → loops back to plan
4. Planner refuses again → executor skips again → auditor fails again
5. After 3 cycles, MAX_RETRIES reached → delivers

Setting `retry_count = MAX_RETRIES` in the executor's return short-circuits this loop. Same pattern used by `_detect_environment_error()` in `auditor.py:150`.

**Alternative considered:** Add `was_refused` check to `should_retry()` → return "deliver". This works but is less clean — the executor already knows the task is refused and should signal "don't retry" at the source rather than relying on a downstream check.

### Impact

- Refused tasks skip code gen AND skip all retry cycles → straight to deliver
- Saves 3 × ~60s = ~180s of wasted audit-retry cycles on refused tasks
- Honest refusal message preserved in `execution_result` for deliverer

### Tests to Add

```
tests/test_executor.py:
  test_execute_skips_on_was_refused()
    — state with was_refused=True → returns immediately, code="", retry_count=MAX_RETRIES
  test_execute_proceeds_on_was_refused_false()
    — state with was_refused=False → proceeds to code generation (mock Claude)
```

**Note:** A third test (`test_execute_refused_forces_delivery` — full pipeline integration) was considered but the key behaviour (`should_retry()` returning "deliver" when `retry_count >= MAX_RETRIES`) is already covered by `test_should_retry_still_respects_max_retries` in Phase 1. The two unit tests above are sufficient.

---

## Phase 6: Project Pipeline Arguments via YAML

**Fixes:** F-8, G-8 (run_pipeline.py wrong arguments)
**Priority:** P2 | **Scope:** M (~40 lines) | **Risk:** Low
**Files:** `projects_macmini.yaml`, `tools/projects.py`, `brain/nodes/planner.py`

### Problem

Tests 7.2a/7.2b/14.3: planner generated `python3 run_pipeline.py` without required `--client` flag. RAG returned code chunks but not the CLI interface spec. All 3 attempts failed with the same usage error.

### Root Cause

`projects_macmini.yaml` defines commands but not their required arguments. The planner generates plausible invocations by guessing from code context.

### Changes

**projects_macmini.yaml** — Add `run_instructions` field to projects with CLI interfaces:

```yaml
  - name: "iGaming Intelligence Dashboard"
    path: "/Users/agentruntime1/Desktop/igaming-intelligence-dashboard"
    description: |
      Competitive intelligence dashboard for the iGaming industry.
      NER pipeline, Gemini enrichment, Streamlit frontend.
    commands:
      run: "cd {path} && source venv/bin/activate && python run_pipeline.py"
    run_instructions: |
      IMPORTANT: run_pipeline.py REQUIRES at least one argument — it errors without args.
      Usage: python run_pipeline.py --full-pipeline | --update-only | --scrape-only
      --full-pipeline: scrape + enrich + deploy (use this for "run the pipeline")
      --update-only: re-enrich existing data without scraping
      --scrape-only: scrape without enrichment or deploy
      ALWAYS pass an explicit flag. Never call run_pipeline.py without arguments.
      NOTE: Verify actual CLI interface via `python run_pipeline.py --help` on
      Mac Mini before deploying this config — the flags above are inferred from
      test failure logs (Tests 7.2a/b: "the following arguments are required")
      and may not match the current CLI exactly.
    timeout: 300
    triggers:
      - "igaming"
      - "igaming intelligence"
      # ... existing triggers
```

**tools/projects.py** — Include `run_instructions` in `get_project_context()` (modify lines 73–91):

```python
def get_project_context(project: dict) -> str:
    """Format a single project's context for injection into prompts."""
    parts = [
        f"Project: {project['name']}",
        f"Path: {project['path']}",
        f"Description: {project.get('description', 'N/A')}",
    ]
    if project.get("commands"):
        cmds = "\n".join(f"  {k}: {v}" for k, v in project["commands"].items())
        parts.append(f"Commands:\n{cmds}")
    if project.get("run_instructions"):
        parts.append(f"Run Instructions:\n{project['run_instructions']}")
    if project.get("timeout"):
        parts.append(f"Timeout: {project['timeout']}s")
    return "\n".join(parts)
```

### Tests to Add

```
tests/test_projects.py:
  test_project_context_includes_run_instructions()
    — Project with run_instructions → context string contains "Run Instructions:"
  test_project_context_without_run_instructions()
    — Project without run_instructions → context string does not contain "Run Instructions:"
```

### Verification

Run Test 7.2 equivalent: "Run the igaming competitor intelligence dashboard." Confirm planner output includes `--full-pipeline` flag.

---

## Phase 7: Task-Type-Specific Audit Criteria Expansion

**Fixes:** Audit versatility improvement (addresses limitations 7, partially 5)
**Priority:** P2 | **Scope:** S (~30 lines) | **Risk:** Low
**Files:** `brain/nodes/auditor.py`

### Problem

The auditor uses one-size-fits-all criteria for each task type. Data tasks don't get math checks (Phase 2 adds them to the base prompt, but criteria-level specificity would reinforce). Frontend tasks don't get completeness checks beyond HTML structure.

### Changes

**auditor.py** — Expand `AUDIT_CRITERIA["frontend"]` (lines 115–125):

```python
    "frontend": """
Evaluate:
1. Was an HTML file generated?
2. Does the HTML contain proper structure (<!DOCTYPE html>, <html>, <head>, <body>, </html>)?
3. Does it include Tailwind CSS (CDN link present)?
4. For React apps: are React, ReactDOM, and Babel CDN scripts included?
5. Does it implement the requested features (components, interactivity, data display)?
6. Is it self-contained (no broken external dependencies, all via CDN)?
7. Is it responsive (mobile-first breakpoints)?
8. Is the HTML COMPLETE? Check that </html> is present at the end. If the code ends mid-tag or mid-script, FAIL with "code appears truncated".
9. Are all <script> blocks closed with </script>? Are all <style> blocks closed with </style>?

FAIL if: no HTML file generated, broken HTML structure, missing Tailwind/React CDN, doesn't implement requested features, code appears truncated (missing </html>).""",
```

**auditor.py** — Expand `AUDIT_CRITERIA["data"]` (lines 64–72, reinforcing Phase 2):

```python
    "data": """
Evaluate:
1. Does the analysis correctly address the user's question?
2. Did execution succeed (exit code 0)?
3. Did all data validation assertions pass? Look for "ALL ASSERTIONS PASSED".
4. Were output files (charts, CSVs) generated?
5. Are there tracebacks or errors?
6. DATA INTEGRITY: Are percentages between 0–100%? Are counts non-negative? If impressions=0, are clicks also 0? If a rate exceeds 1000%, flag as likely column misalignment.
7. Does the output contain the ACTUAL data requested, not sample/mock/placeholder data?

FAIL if: non-zero exit code, assertion failures, no output files when expected, traceback present, mathematically impossible values, sample data substituted for real data.""",
```

**auditor.py** — Add `AUDIT_CRITERIA["project"]` enhancement for pipeline tasks (lines 74–85):

```python
    "project": """
Evaluate:
1. Did the project command execute successfully (exit code 0)?
2. Were the correct parameters extracted and used (check the command for proper client name, file paths, flags)?
3. Did the command produce expected output files?
4. Is the stdout output meaningful (not empty or error-only)?
5. Were there any errors or warnings that indicate failure?
6. Did the command use the CORRECT arguments as specified in the project's run_instructions?
7. If the task involves data processing: are output metrics plausible (no impossible percentages, no zero-denominator rates)?

NOTE: Project commands do NOT use Python assert statements. Do NOT look for "ALL ASSERTIONS PASSED".
Instead, check: exit code 0, expected files created, meaningful output in stdout, correct arguments used.

FAIL if: non-zero exit code, wrong parameters used, no output files when expected, error messages in output, impossible data values in output.""",
```

### Tests to Add

```
tests/test_auditor.py:
  test_frontend_audit_detects_truncated_html()
    — Mock execution with HTML missing </html> → assert verdict="fail"
  test_data_audit_detects_impossible_rate()
    — Mock execution with "CTR: 11600%" → assert verdict="fail"
  test_project_audit_checks_correct_arguments()
    — Mock execution with wrong pipeline args → assert verdict="fail"
```

---

## Phase 8: ARCHITECTURE.md Per-Project Convention

**Fixes:** Context evaporation limitation, G-8 (CLI argument handling), planning quality
**Priority:** P2 | **Scope:** M (~50 lines code + convention doc) | **Risk:** Low
**Files:** `brain/nodes/planner.py`, `tools/projects.py`

### Problem

The planner's project context comes from three sources: YAML triggers (shallow), project_memory (50 FIFO rows, pattern-focused), RAG chunks (fragmented code). None provide architectural overview: tech stack, directory structure, key entry points, CLI interfaces. This gap causes wrong invocations (Tests 7.2a/b), wrong routing (Test 10.1), and slow planning (no context → longer reasoning).

### Convention

Each registered project gets an `ARCHITECTURE.md` in its root directory:

```markdown
# {Project Name} — Architecture

## Tech Stack
- Python 3.11, FastAPI, Supabase
- Deployed via GitHub Actions

## Directory Structure
src/
  api/         → FastAPI routes
  services/    → Business logic
  models/      → Pydantic schemas
tests/         → pytest

## Key Entry Points
- `run_pipeline.py` → Main CLI. Usage: `python run_pipeline.py [--full-pipeline|--update-only]`
- `src/main.py` → FastAPI app entry

## Important Patterns
- All config via .env (never hardcode)
- Async database access via asyncpg

## Known Gotchas
- Pipeline requires `--full-pipeline` flag for complete run
- Streamlit app must be started separately
```

**Maximum 200 lines.** Human-maintained (agent suggests updates, human approves).

### Changes

**brain/nodes/planner.py** — Read ARCHITECTURE.md before RAG injection (insert before line 227):

```python
    # Inject project ARCHITECTURE.md if it exists (read before RAG for structural context)
    if task_type == "project" and state.get("project_config", {}).get("path"):
        arch_path = Path(state["project_config"]["path"]) / "ARCHITECTURE.md"
        if arch_path.is_file():
            try:
                arch_content = arch_path.read_text(encoding="utf-8", errors="replace")[:5000]
                system += f"\n\nPROJECT ARCHITECTURE ({state.get('project_name', 'unknown')}):\n{arch_content}"
                logger.info("Injected ARCHITECTURE.md for %s (%d chars)", state.get("project_name"), len(arch_content))
            except OSError as e:
                logger.warning("Failed to read ARCHITECTURE.md: %s", e)
```

**brain/nodes/deliverer.py** — After successful project task, suggest ARCHITECTURE.md update (append to final_response if project task passed):

```python
    # Suggest ARCHITECTURE.md update for successful project tasks
    if state.get("task_type") == "project" and state.get("audit_verdict") == "pass":
        project_path = state.get("project_config", {}).get("path", "")
        arch_path = Path(project_path) / "ARCHITECTURE.md" if project_path else None
        if arch_path and not arch_path.is_file():
            # Only mention if ARCHITECTURE.md doesn't exist yet
            response += "\n\n_Tip: This project has no ARCHITECTURE.md yet. Consider creating one to improve future task planning._"
```

### What NOT to Do

- Do NOT auto-write ARCHITECTURE.md — agent suggestions only, human approves
- Do NOT block on missing ARCHITECTURE.md — graceful degradation (just skip)
- Do NOT inject for non-project tasks — only `task_type == "project"`

### Tests to Add

```
tests/test_planner.py:
  test_architecture_md_injected_when_present()
    — Create temp ARCHITECTURE.md → plan() system prompt contains "PROJECT ARCHITECTURE"
  test_architecture_md_skipped_when_missing()
    — No ARCHITECTURE.md → plan() system prompt does not contain "PROJECT ARCHITECTURE"
  test_architecture_md_capped_at_5000_chars()
    — Create 10K char ARCHITECTURE.md → injected content is ≤5000 chars
```

---

## Phase 9: Ollama Health Monitoring

**Fixes:** G-9 (111 empty responses undetected until log analysis)
**Priority:** P2 | **Scope:** S (~30 lines) | **Risk:** Low
**Files:** `tools/model_router.py`, `bot/handlers.py`

### Problem

111 Ollama empty responses during the 10hr test run, each burning 4–6s before Claude escalation. No visibility into this without manually grepping logs. The `/health` command shows pipeline stage averages but not Ollama reliability metrics.

### Changes

**tools/model_router.py** — Add module-level stats dict (after line 22):

Uses a mutable dict (consistent with the `_client = None` singleton pattern used elsewhere in the codebase) instead of `global` declarations:

```python
# Ollama reliability counters (reset on process restart)
_ollama_stats = {
    "calls": 0,
    "empty_responses": 0,
    "errors": 0,
    "fallbacks_to_claude": 0,
}
```

Update `route_and_call()` (in the Ollama call block, ~lines 40–53):

```python
    if provider == "ollama":
        for attempt in range(2):
            _ollama_stats["calls"] += 1
            try:
                result = _call_ollama(prompt, system=system, model=model, max_tokens=max_tokens)
                if result and result.strip():
                    return result
                _ollama_stats["empty_responses"] += 1
                logger.warning("Ollama returned empty (attempt %d/2)", attempt + 1)
                # ... existing retry logic
            except Exception as e:
                _ollama_stats["errors"] += 1
                # ... existing error handling
        # Fallback to Claude
        _ollama_stats["fallbacks_to_claude"] += 1
        # ... existing fallback logic
```

Add getter function:

```python
def get_ollama_stats() -> dict:
    """Return Ollama reliability counters for /health display."""
    total = _ollama_stats["calls"]
    failures = _ollama_stats["empty_responses"] + _ollama_stats["errors"]
    return {
        **_ollama_stats,
        "reliability_pct": round((1 - failures / max(total, 1)) * 100, 1),
    }
```

**bot/handlers.py** — Add Ollama stats to `/health` output:

```python
    # In the /health handler, after existing pipeline stats:
    from tools.model_router import get_ollama_stats
    ollama = get_ollama_stats()
    if ollama["calls"] > 0:
        health_text += (
            f"\n\n**Ollama Reliability:**\n"
            f"Calls: {ollama['calls']} | Empty: {ollama['empty_responses']} | "
            f"Errors: {ollama['errors']} | Claude fallbacks: {ollama['fallbacks_to_claude']}\n"
            f"Reliability: {ollama['reliability_pct']}%"
        )
```

### Tests to Add

```
tests/test_model_router.py:
  test_ollama_stats_increment_on_empty()
    — Mock Ollama returning empty → stats["empty_responses"] == 1
  test_ollama_stats_increment_on_fallback()
    — Mock Ollama failing → stats["fallbacks_to_claude"] == 1
  test_ollama_reliability_calculation()
    — Set counters manually → verify percentage calculation
```

---

## Test Coverage Summary

| Phase | New Tests | Estimated LOC |
|-------|-----------|---------------|
| 0a | 4 | ~40 |
| 0b | 5 | ~50 |
| 0c | 0 | 0 |
| 0d | 3 | ~30 |
| 1 | 4 | ~40 |
| 2 | 3 | ~30 |
| 3 | 6 | ~60 |
| 4 | 7 | ~70 |
| 4b | 7 | ~70 |
| 5 | 2 | ~20 |
| 6 | 2 | ~20 |
| 7 | 3 | ~30 |
| 8 | 3 | ~30 |
| 9 | 3 | ~30 |
| **Total** | **52** | **~520** |

---

## Rollout Strategy

### Phase 0 (P0) — Deploy together, test immediately

1. Pull `qwen2.5:7b` on Mac Mini: `ollama pull qwen2.5:7b`
2. Deploy all Phase 0 changes in one commit
3. Run 10 classification tasks, verify latency <15s in logs
4. Run Test 10.1 equivalent (portfolio page), verify no project over-matching
5. Run a long task (Test 12.1 equivalent), verify no 900s timeout

### Phases 1–5 (P1) — Deploy sequentially, test each

Each phase gets its own commit. Run the relevant test from the Ultimate Test Suite after each:
- Phase 1: Run Tests 7.2a/7.2b → verify early exit on duplicate error
- Phase 2: Create mock data report with impossible metrics → verify audit catches it
- Phase 3: Run Test 8.3 equivalent → verify HTML truncation detected
- Phase 4: Run Test 15.1 → verify importlib allowed for sys.builtin_module_names
- Phase 4b: Run Test 3.1 equivalent → verify shutil.rmtree with variable/expanduser blocked before execution
- Phase 5: Run Test 4.1 → verify no code generated for refused task, no retry cycles wasted

### Phases 6–9 (P2) — Deploy in parallel, test in next full test suite run

These phases improve quality but don't fix blockers. Include in the next full test suite execution.

---

## Mapping: Limitation → Phase

| Limitation | Root Cause | Fix Phase(s) |
|------------|-----------|-------------|
| 1. Planning bottleneck (65–380s) | Ollama model overhead, complexity routing too conservative, no early-exit on repeats | 0a, 0d, 1 |
| 2. Classifier over-matching | Naive substring trigger matching | 0b |
| 3. Ollama 30–55s classification | deepseek-r1:14b `<think>` overhead, 9GB RAM | 0a |
| 4. importlib false positive | Regex blocks all importlib, no argument inspection | 4 |
| 5. HTML truncation undetected | Detector only checks Python/shell constructs | 3, 7 |
| 6. Wrong pipeline arguments | Planner doesn't know CLI interfaces | 6, 8 |
| 7. Audit misses impossible data | No data sanity instructions in audit prompt | 2, 7 |
| 8. shutil.rmtree scanner gap | Regex only catches literal paths, misses variable/function indirection | 4b |

## Mapping: Evolution Roadmap → Phase

| Roadmap Item | Phase |
|-------------|-------|
| F-1: Switch classify to qwen2.5:7b | 0a |
| F-2: Classifier trigger context-awareness | 0b |
| F-3: Duplicate error detection | 1 |
| F-4: Audit data sanity | 2 |
| F-5: HTML truncation detection | 3 |
| F-6: LONG_TIMEOUT increase | 0c |
| F-7: importlib smart allowlist | 4 |
| F-8: Project pipeline arguments | 6 |
| Refine plan complexity routing | 0d |
| ARCHITECTURE.md per project | 8 |
| Task-type-specific audit criteria | 7 |
| Purpose-dependent Ollama routing | 0a |
| Ollama health monitoring | 9 |
| Executor respects was_refused | 5 |
| shutil.rmtree scanner hardening | 4b |

---

## Files Changed Summary

| File | Phases | Lines Changed (est.) |
|------|--------|---------------------|
| `config.py` | 0a, 0c | ~5 |
| `tools/model_router.py` | 0a, 9 | ~60 |
| `tools/projects.py` | 0b, 6 | ~40 |
| `brain/nodes/classifier.py` | — | 0 (no changes needed) |
| `brain/nodes/planner.py` | 0d, 8 | ~25 |
| `brain/nodes/executor.py` | 3, 5 | ~30 |
| `brain/nodes/auditor.py` | 2, 7 | ~50 |
| `brain/nodes/deliverer.py` | 8 | ~10 |
| `brain/graph.py` | 1 | ~20 |
| `brain/state.py` | 1 | ~2 |
| `tools/sandbox.py` | 4, 4b | ~85 |
| `bot/handlers.py` | 9 | ~10 |
| `projects_macmini.yaml` | 6 | ~20 |
| **Total** | | **~360 source + ~520 test = ~880** |
