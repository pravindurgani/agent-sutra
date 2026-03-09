# AgentSutra v8.8.0 — Implementation Plan

**Version:** 1.2
**Date:** 2026-03-08
**Source:** [AgentSutra_Improvements_Report.md](./AgentSutra_Improvements_Report.md) (v4, 2026-03-08)
**Phases:** 14 (Phase 0–13)
**Estimated total scope:** ~480 lines changed
**Execution order:** Phase 0 first (all items parallelisable), then Phases 1–8 sequentially, then Phases 9–13 (parallelisable P2 items)

---

## Execution Order Summary

```
Phase 0  (P0 quick wins — all parallelisable)
  ├── 0a: Shell truncation false-positive (F-1) — CRITICAL, unblocks all code gen
  ├── 0b: /cost model name display (F-3)
  ├── 0c: "Done" → "Processing" acknowledgment (3.2)
  └── 0d: Harmonise cost defaults (9.4)

Phase 1  (P1 security — sequential: credential filter)
Phase 2  (P1 security — sequential: fabrication guard)
Phase 3  (P1 pipeline — sequential: budget escalation, F-5)
Phase 4  (P1 pipeline — sequential: Ollama empty response handling)
Phase 5  (P1 pipeline — sequential: file selector parse failures)
Phase 6  (P1 UX — sequential: chain refusal status)
Phase 7  (P1 UX — sequential: /deploy task type check)
Phase 8  (P1 security — sequential: false positive subprocess blocks)

Phase 9  (P2 — parallelisable: path sanitisation + Linux paths)
Phase 10 (P2 — parallelisable: over-generation limits)
Phase 11 (P2 — parallelisable: RAG zero-vector guard)
Phase 12 (P2 — parallelisable: task completion log summary)
Phase 13 (P2 — parallelisable: timeout progress feedback)
```

**Dependencies:**
- Phase 0a must complete before Phases 2, 5, 8 (they involve code gen testing)
- Phase 3 depends on Phase 0d (cost defaults must be consistent first)
- All other phases are independent

---

## Phase 0a — Fix Shell Truncation False-Positive on Python (F-1)

**Why:** Report §9.1 (F-1). The `\bif\b` regex in `_detect_truncation()` matches Python's `if` keyword. Any Python code with >2 `if` statements triggers false shell truncation, causing 100% failure on non-trivial Python code gen. This is the single biggest blocker.

**What:** `brain/nodes/executor.py`, function `_detect_truncation()`, lines 91–97.

**How:**
1. Only apply the shell `if/fi` and `do/done` checks when the code starts with a shell shebang
2. Check if the first non-empty, non-comment line matches `#!/bin/bash`, `#!/bin/sh`, `#!/usr/bin/env bash`, or `#!/usr/bin/env sh`
3. If no shebang, skip the shell truncation checks entirely

```python
# Replace lines 91-97 with:
# 7D: Shell script truncation — only for actual shell scripts
_is_shell = False
for line in stripped.split("\n"):
    line = line.strip()
    if not line or line.startswith("#!"):
        if line.startswith("#!") and ("bash" in line or "/sh" in line):
            _is_shell = True
        break
    break  # first non-empty line isn't a shebang

if _is_shell:
    if_count = len(re.findall(r'\bif\b', stripped))
    fi_count = len(re.findall(r'\bfi\b', stripped))
    do_count = len(re.findall(r'\bdo\b', stripped))
    done_count = len(re.findall(r'\bdone\b', stripped))
    shell_truncated = (if_count > fi_count + 2) or (do_count > done_count + 1)
    truncated = truncated or shell_truncated
```

**Known gap:** Shell scripts invoked via `bash script.sh` (no shebang) will skip the if/fi check. This is acceptable because: (1) the paren/bracket/brace/string checks still catch most truncation regardless of language, (2) AgentSutra's executor generates shebangs for shell scripts via `CODE_GEN_SYSTEM` prompt, and (3) the false-positive cost (100% failure on all Python code gen) vastly outweighs the false-negative risk (occasional missed shell truncation on shebang-less scripts).

**What NOT to do:**
- Do NOT use heuristics like "check first 10 lines for `def`/`import`/`class`" — fragile against Python files starting with comments or docstrings
- Do NOT remove shell truncation detection entirely — it catches real shell truncation
- Do NOT create a separate `_is_shell_script()` utility function — inline is fine for this

**Tests to add:**
- `test_detect_truncation_python_many_ifs_not_truncated` — Python code with 20 `if` statements, no shebang → returns `False`
- `test_detect_truncation_shell_script_unclosed_if` — Bash script with `#!/bin/bash` shebang, 5 `if` and 0 `fi` → returns `True`
- `test_detect_truncation_shell_script_balanced` — Bash script with balanced if/fi → returns `False`
- `test_detect_truncation_python_still_catches_parens` — Python code with unclosed parens still returns `True`
- `test_detect_truncation_shell_no_shebang_unclosed_parens` — Shell-style code without shebang but with 5 unclosed parens → returns `True` (caught by paren check, not if/fi)
- **Test file:** `tests/test_executor.py` (existing)

**Acceptance criteria:**
- `pytest tests/test_executor.py -k "truncation" -v` passes with 0 failures
- Python code with 20+ `if` statements is NOT flagged as truncated
- Shell scripts with genuine truncation ARE still flagged

**Verify:** `pytest tests/test_executor.py -k "truncation" -v`

**Estimated scope:** S (<20 lines)

---

## Phase 0b — Fix /cost Model Name Display (F-3)

**Why:** Report §9.2 (F-3). `model.split("-")[-1]` on `claude-sonnet-4-6` produces `"6"`, displayed as "6: $30.59 (100%)" — meaningless.

**What:** `tools/claude_client.py`, function `get_daily_cost_breakdown()`, line 373.

**How:**
```python
# Replace:
short = model.split("-")[-1] if "-" in model else model
# With:
short = model.replace("claude-", "").rsplit("-", 1)[0] if model.startswith("claude-") else model
```

This produces `"sonnet-4"` from `claude-sonnet-4-6` and `"opus-4"` from `claude-opus-4-6`.

**What NOT to do:**
- Do NOT create a `_format_model_name()` helper — this is a one-liner used in one place
- Do NOT change how models are stored in the database — only the display format
- Do NOT touch `MODEL_COSTS` keys or any other model string handling

**Tests to add:**
- `test_cost_summary_model_name_display` — mock `api_usage` rows with `claude-sonnet-4-6` and `claude-opus-4-6`, verify output contains `"sonnet-4"` and `"opus-4"`, not `"6"`
- **Test file:** `tests/test_claude_client.py` (existing)

**Acceptance criteria:**
- `/cost` command displays model names as `sonnet-4` and `opus-4`, not `6`

**Verify:** `pytest tests/test_claude_client.py -k "cost" -v`

**Estimated scope:** S (<5 lines)

---

## Phase 0c — Fix Misleading "Done" Acknowledgment (3.2)

**Why:** Report §3.2. The bot sends `"Done. (task XXXX)"` when a task is *accepted*, then edits the same message to `"Done."` again on completion. The initial message is misleading — users think the task is already finished.

**What:** `bot/handlers.py`, lines 816 and 905.

**How:**
```python
# Line 816 — change initial acknowledgment:
status_msg = await update.message.reply_text(f"Processing... (task {task_id[:8]}){budget_warning}")

# Line 905 — completion message stays the same:
await status_msg.edit_text(f"Done. (task {task_id[:8]})")
```

**What NOT to do:**
- Do NOT add progress stages to the status message — that's Phase 12 territory (report §3.4)
- Do NOT touch the retry handler messages — they already say "Retrying..."
- Do NOT change the `/chain` status messages — separate issue

**Tests to add:**
- No test needed — string-only change with no logic. Verified by manual Telegram interaction.

**Acceptance criteria:**
- Initial task acknowledgment says "Processing..." not "Done."
- Completion message still says "Done."

**Verify:** Manual: send a task via Telegram, observe the initial message says "Processing..."

**Estimated scope:** S (<5 lines)

---

## Phase 0d — Harmonise Cost Defaults (9.4)

**Why:** Report §9.4. `_get_today_spend()` in `model_router.py:148` uses `{"input": 3.00, "output": 15.00}` as fallback for unknown models, while `_check_budget()` in `claude_client.py:119` uses `{"input": 15.00, "output": 75.00}`. This mismatch can cause budget threshold miscalculation.

**What:** `tools/model_router.py`, function `_get_today_spend()`, line 148.

**How:**
```python
# Replace:
costs = _MODEL_COSTS.get(model, {"input": 3.00, "output": 15.00})
# With:
costs = _MODEL_COSTS.get(model, {"input": 15.00, "output": 75.00})
```

This matches the conservative default already used in `claude_client.py`.

**What NOT to do:**
- Do NOT extract the default into a shared constant — it's only used in 3 places and they should all match the `claude_client.py` values
- Do NOT change the defaults in `claude_client.py` — those are already correct (conservative: assume expensive model)

**Tests to add:**
- `test_get_today_spend_unknown_model_uses_expensive_default` — insert a row with model `"claude-unknown-99"` into `api_usage`, verify `_get_today_spend()` calculates cost using the 15.00/75.00 rates
- **Test file:** `tests/test_model_router.py` (existing)

**Acceptance criteria:**
- `_get_today_spend()` and `_check_budget()` use identical fallback pricing for unknown models

**Verify:** `pytest tests/test_model_router.py -k "spend" -v`

**Estimated scope:** S (<5 lines)

---

## Phase 1 — Credential Filter Gaps (9.6)

**Why:** Report §9.6. Missing credential patterns: `sk-ant-api*` (Anthropic), `xoxb-*` (Slack), Telegram bot tokens. Missing file extensions: `.py`, `.html`, `.js`.

**What:** `brain/nodes/deliverer.py`, lines 17–22 (`_CREDENTIAL_RE`) and line 48 (`_has_credential_patterns` suffix check).

**How:**

1. Add missing patterns to `_CREDENTIAL_RE`:
```python
_CREDENTIAL_RE = [
    re.compile(r'\bghp_[a-zA-Z0-9]{36}\b'),            # GitHub PAT
    re.compile(r'\bya29\.[a-zA-Z0-9_-]{50,}\b'),        # Google OAuth
    re.compile(r'\bsk-[a-zA-Z0-9]{48}\b'),               # OpenAI key
    re.compile(r'\bAKIA[A-Z0-9]{16}\b'),                 # AWS access key
    re.compile(r'\bsk-ant-api\d{2}-[a-zA-Z0-9_-]{90,}\b'),  # Anthropic key
    re.compile(r'\bxoxb-[0-9]+-[a-zA-Z0-9]+\b'),        # Slack bot token
    re.compile(r'\b\d{8,10}:[A-Za-z0-9_-]{35}\b'),      # Telegram bot token
]
```

2. Extend scanned extensions at line 48:
```python
if path.suffix not in ('.log', '.txt', '.json', '.yaml', '.yml', '.csv', '.py', '.html', '.js'):
    return False
```

**What NOT to do:**
- Do NOT add patterns for every possible API key format — only add patterns for services AgentSutra actually uses or is likely to encounter
- Do NOT scan binary files (`.png`, `.pdf`, `.zip`) — only text formats
- Do NOT add regex patterns for generic "looks like a secret" — high false positive rate

**Tests to add:**
- `test_credential_filter_anthropic_key` — content with `sk-ant-api03-...` → returns `True`
- `test_credential_filter_slack_token` — content with `xoxb-123456-abc...` → returns `True`
- `test_credential_filter_telegram_token` — content with `1234567890:ABCdef...` → returns `True`
- `test_credential_filter_scans_py_files` — `.py` file with embedded GitHub PAT → returns `True`
- `test_credential_filter_scans_html_files` — `.html` file with embedded key → returns `True`
- `test_credential_filter_allows_clean_py` — `.py` file without credentials → returns `False`
- **Test file:** `tests/test_deliverer.py` (existing)

**Acceptance criteria:**
- All 3 new credential patterns are detected in test artifacts
- `.py`, `.html`, `.js` files are now scanned
- Existing tests still pass (no regression on current patterns)

**Verify:** `pytest tests/test_deliverer.py -k "credential" -v`

**Estimated scope:** S (<20 lines)

---

## Phase 2 — Data Fabrication Guard (2.1)

**Why:** Report §2.1. Agent fabricates fake files (logs with fake tokens, fake SDKs, sample directories) when real files don't exist. Violates invariant #8. The existing `_check_referenced_files()` warns but the LLM ignores the warning.

**Confidence: Medium.** Both changes below are prompt-based — they rely on LLM compliance. The executor warning is low-confidence (the LLM that fabricated a quantum computing SDK will likely ignore stronger wording too). The auditor prompt additions are higher-confidence because Opus is a separate, adversarial reviewer and already catches some fabrication (e.g., the GitHub scraper padding case in the report). Together they raise the bar, but neither guarantees prevention.

**What:** `brain/nodes/auditor.py` (primary — strengthen fabrication prompt), and `brain/nodes/executor.py` (secondary — strengthen `_check_referenced_files()` warning).

**How:**

1. **Primary fix — auditor prompt** (higher confidence). In `auditor.py`, add explicit fabrication checks to the audit system prompt. These are specific enough for Opus to act on:
```
- FAIL if the code creates sample/fake/mock data files when the task asked to READ or ANALYSE existing data
- FAIL if the code fabricates an entire library/SDK module instead of importing a real, installable package
- FAIL if the code writes credential-shaped strings (ghp_, sk-, xoxb-, ya29., AKIA) to any output file
- FAIL if the code creates a fake directory tree or generates synthetic data to substitute for missing real data
```

2. **Secondary fix — executor warning** (lower confidence). In `executor.py`, `_check_referenced_files()`, change the warning text to be more explicit about exit behaviour:
```python
return (
    "\nWARNING: These files do NOT exist: " + ", ".join(missing) + ". "
    "Do NOT create fake/sample versions. Call sys.exit(1) with a clear error message "
    "stating which file was not found. NEVER fabricate data for a missing file."
)
```

**What NOT to do:**
- Do NOT add a separate fabrication detection pass in the executor — the auditor is the gate (invariant #2)
- Do NOT block all file creation — legitimate tasks create output files
- Do NOT try to detect fabrication programmatically by comparing file contents — impossible to verify
- Do NOT over-invest in the executor warning — it's the weaker of the two fixes

**Tests to add:**
- `test_check_referenced_files_missing_warns_exit` — message references `data.csv`, file doesn't exist → warning includes `sys.exit(1)`
- `test_check_referenced_files_existing_no_warn` — message references `data.csv`, file exists → empty string
- `test_audit_prompt_includes_fabrication_checks` — verify the auditor system prompt contains "fabricates" or "fake/mock data" (string assertion on prompt constant)
- **Test file:** `tests/test_executor.py` and `tests/test_auditor.py` (existing)

**Acceptance criteria:**
- Auditor prompt includes 4 specific fabrication check instructions
- `_check_referenced_files()` returns stronger "sys.exit(1)" language for missing files
- Neither change breaks existing tests

**Verify:** `pytest tests/test_executor.py -k "referenced_files" -v && pytest tests/test_auditor.py -v`

**Estimated scope:** S (<20 lines)

---

## Phase 3 — Budget Escalation Skips High-Complexity (F-5)

**Why:** Report §9.3 (F-5). Budget escalation at `model_router.py:80` routes to Ollama regardless of complexity. High-complexity plans sent to Ollama time out (120s), wasting time before falling back to Claude anyway.

**What:** `tools/model_router.py`, function `_select_model()`, line 80.

**How:**
```python
# Replace line 80:
if purpose in ("classify", "plan") and _daily_spend_exceeds_threshold(0.7):
# With:
if purpose in ("classify", "plan") and complexity != "high" and _daily_spend_exceeds_threshold(0.7):
```

**What NOT to do:**
- Do NOT add a separate complexity-aware budget router — the existing `_select_model()` handles this with one condition
- Do NOT change the 0.7 threshold or any other budget logic
- Do NOT add budget pre-check before Ollama fallback to Claude (report §9.3 F-4) — that's a separate, lower-priority change

**Tests to add:**
- `test_budget_escalation_skips_high_complexity` — mock `_daily_spend_exceeds_threshold(0.7)` → `True`, call `_select_model("plan", "high")` → returns Claude, not Ollama
- `test_budget_escalation_routes_low_to_ollama` — mock same threshold → `True`, call `_select_model("plan", "low")` with Ollama available → returns Ollama
- **Test file:** `tests/test_model_router.py` (existing)

**Acceptance criteria:**
- High-complexity tasks never route to Ollama under budget pressure
- Low-complexity tasks still route to Ollama under budget pressure

**Verify:** `pytest tests/test_model_router.py -k "budget" -v`

**Estimated scope:** S (<5 lines)

---

## Phase 4 — Stabilise Ollama Empty Response Handling (1.2)

**Why:** Report §1.2. 36 "empty response" failures on Mar 08 — Ollama returns 200 with empty/unparseable content. Current retry (2 attempts, 2s delay) exists but 61% still fail. The `deepseek-r1:14b` model produces thinking tokens without a final answer.

**What:** `tools/model_router.py`, function `_call_ollama()`, lines 181–184.

**How:**

The existing `<think>` stripping at line 183–184 only strips if both `<think>` and `</think>` are present. If the model returns *only* a thinking block without closing it, or if the content is entirely within `<think>...</think>` with nothing after, the result is empty.

1. Handle case where content is entirely a thinking block (nothing after `</think>`):
```python
# After existing think-stripping (line 184):
if "<think>" in content and "</think>" in content:
    content = content.split("</think>", 1)[-1].strip()
# Add: handle unclosed think block
elif "<think>" in content and "</think>" not in content:
    content = ""  # Incomplete thinking — treat as empty for retry
```

2. Log the raw response length when it's empty for debugging:
```python
if not content:
    logger.warning("Ollama returned empty content (raw length: %d)", len(response.json().get("message", {}).get("content", "")))
```

**What NOT to do:**
- Do NOT increase retry count beyond 2 — Ollama empty responses are a model issue, more retries just waste time
- Do NOT switch the default Ollama model here — that's an ops decision, not a code change
- Do NOT add structured output / JSON mode to Ollama — the chat API doesn't reliably support it

**Tests to add:**
- `test_ollama_strips_unclosed_think_block` — response with `<think>reasoning...` (no closing tag) → returns empty string (triggers retry/fallback)
- `test_ollama_strips_complete_think_block_with_answer` — response with `<think>...</think>actual answer` → returns `"actual answer"`
- `test_ollama_strips_think_block_no_answer` — response with `<think>...</think>` and nothing after → returns empty string
- **Test file:** `tests/test_model_router.py` (existing)

**Acceptance criteria:**
- Unclosed `<think>` blocks are handled (treated as empty → triggers fallback)
- Complete think blocks with content after `</think>` still work correctly
- Raw response length is logged on empty responses

**Verify:** `pytest tests/test_model_router.py -k "ollama" -v`

**Estimated scope:** S (<15 lines)

---

## Phase 5 — File Selector Parse Failure Retry (1.5)

**Why:** Report §1.5. 21 occurrences of `"File selector returned unparseable response"` — the Claude-based file selector returns empty or non-JSON, silently falling back to no file injection.

**What:** `brain/nodes/planner.py`, function `_inject_project_files()`, lines 357–367 (legacy fallback path).

**How:**

Add one retry on JSON parse failure, and log the raw response for debugging:

```python
# Replace the single call + catch block (lines 357-367) with:
selected = None
for attempt in range(2):
    try:
        selection = claude_client.call(
            selector_prompt, system=_FILE_SELECTOR_SYSTEM,
            max_tokens=300, temperature=0.0,
        )
        parsed = json.loads(selection)
        if isinstance(parsed, list):
            selected = parsed
            break
        logger.warning("File selector returned non-list: %s", type(parsed).__name__)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(
            "File selector parse failure (attempt %d/2): %s — raw: %.100s",
            attempt + 1, e, selection if 'selection' in dir() else "<no response>",
        )
    except Exception as e:
        logger.warning("File selector failed: %s", e)
        break  # Don't retry on non-parse errors (API errors, etc.)

if selected is None:
    return system
```

**What NOT to do:**
- Do NOT add JSON mode / structured output constraint — `claude_client.call()` doesn't support response format parameters and adding it is a larger change
- Do NOT retry more than once — parse failures are usually model confusion, not transient errors
- Do NOT change the RAG path — this only affects the legacy fallback

**Tests to add:**
- `test_file_selector_retries_on_parse_failure` — mock `claude_client.call` to return `"not json"` first, then `'["file.py"]'` second → files are injected
- `test_file_selector_logs_raw_response_on_failure` — mock to return garbage → verify log contains the raw response
- **Test file:** `tests/test_planner.py` (existing)

**Acceptance criteria:**
- File selector retries once on JSON parse failure
- Raw failing response is logged (truncated to 100 chars)
- If both attempts fail, falls back gracefully (returns system unchanged)

**Verify:** `pytest tests/test_planner.py -k "file_selector or inject" -v`

**Estimated scope:** M (~25 lines)

---

## Phase 6 — Chain Refusal Status Bug (3.1)

**Why:** Report §3.1. Chain reports "all 2 steps passed" when both steps were `rm -rf ~/` security refusals.

**Root cause (confirmed by code investigation):** The planner recognises the dangerous task and generates a *refusal plan* ("I cannot execute this task"). The executor then generates benign Python code from that plan — e.g., `print("I cannot execute this task. rm -rf ~/ would delete your home directory.")`. This benign code:
- Runs successfully (exit code 0) → `exec_failed = False`
- Contains `rm -rf ~/` only inside a string literal, which doesn't match the Tier 1 word-boundary regex → `exec_blocked = False` (no `"BLOCKED:"` in result)
- Produces no security violations → `audit_verdict = "pass"`

All three conditions in the strict-AND gate (line 1154) are False, so the chain continues. After all steps, it reports "all passed." The pipeline correctly *handled* the dangerous request (nothing bad happened), but the chain *misreports* the outcome.

**What:** `brain/nodes/executor.py` — add a `was_refused` flag to AgentState. `bot/handlers.py` — check the flag in the chain handler.

**Why not text matching:** Checking `final_response` for phrases like "security policy", "i cannot", "i can't" is fragile — legitimate responses discussing limitations (e.g., "I can't determine the exact date without more context") would false-positive. A structured flag is more reliable.

**How:**

1. Add `was_refused: bool` to `AgentState` in `brain/state.py` (default `False`).

2. In `brain/nodes/planner.py`, when the planner's system prompt includes security refusal language and the plan output contains refusal indicators, set the flag. The planner already has `SEC-2` credential file detection (lines 43-56) — after that check, if the plan starts with refusal language ("I cannot", "This task", "I'm unable"), set `state["was_refused"] = True`:
```python
# After plan is generated:
plan_lower = plan.strip().lower()[:100]
if any(plan_lower.startswith(p) for p in [
    "i cannot", "i can't", "i'm unable", "this task cannot",
    "this request", "i will not", "i won't",
]):
    state["was_refused"] = True
    logger.info("Planner refused task: %.80s", plan.strip())
```

3. In `bot/handlers.py`, chain handler, after each step's pipeline run (line 1172), check the flag:
```python
# Before the for loop:
refused_count = 0

# After line 1172 (previous_artifacts = ...):
if result.get("was_refused", False):
    refused_count += 1

# Replace the else block at lines 1187-1191:
else:
    if refused_count > 0:
        await update.message.reply_text(
            f"Chain complete - {refused_count}/{len(steps)} steps refused by security policy."
        )
    else:
        await update.message.reply_text(
            f"Chain complete - all {len(steps)} steps passed."
        )
```

**What NOT to do:**
- Do NOT halt the chain on refusals — the current behaviour of continuing is correct (a chain might have some refused and some valid steps)
- Do NOT scan `final_response` for refusal phrases — false positives on legitimate responses discussing limitations
- Do NOT add complex NLP-based refusal detection — prefix matching on the planner output is sufficient because the planner generates refusals with consistent phrasing

**Tests to add:**
- `test_planner_sets_refused_flag_on_refusal` — mock planner output starting with "I cannot execute" → `state["was_refused"]` is `True`
- `test_planner_does_not_set_refused_on_normal_plan` — mock planner output with a real plan → `state["was_refused"]` is `False`
- `test_chain_reports_refused_count` — mock 2 steps both with `was_refused=True` → completion message mentions refused count
- `test_chain_reports_all_passed_when_none_refused` — mock 2 steps with `was_refused=False` → message says "all passed"
- **Test file:** `tests/test_planner.py` and `tests/test_handlers.py` (existing)

**Acceptance criteria:**
- Planner sets `was_refused=True` when it generates a refusal plan
- Chain completion message distinguishes "all passed" from "N steps refused"
- Normal plans (non-refusal) do not trigger the flag

**Verify:** `pytest tests/ -k "chain or refused" -v`

**Estimated scope:** M (~30 lines across 3 files)

---

## Phase 7 — /deploy Accepts Code-Typed HTML (3.5)

**Why:** Report §3.5. A 404 error page HTML was classified as `code` (not `frontend`), so `/deploy` rejected it. But `/deploy` already works by globbing for `.html` files — it doesn't actually check `task_type`. Re-reading the handler (lines 1209–1216): it globs `config.OUTPUTS_DIR` for `*{task_id_prefix}*.html`. If no HTML files are found, it says "No HTML artifacts found."

**Investigation:** The failure was not a task_type check — it was that the HTML file wasn't named with the task_id prefix, or wasn't in `OUTPUTS_DIR`. The report says "task classification affects downstream functionality" but the code shows `/deploy` doesn't check task_type at all.

**Revised what:** The issue might be that the task's HTML artifact was saved with a different naming convention. Check if the artifact path matches the glob pattern. The fix is to also check the task's artifact list from the database.

**How:**
```python
# After line 1210 (html_files = ...), if no matches, try the task's stored artifacts:
if not html_files:
    task = await db.get_task_by_prefix(task_id_prefix)
    if task and task.get("task_state"):
        try:
            state = json.loads(task["task_state"]) if isinstance(task["task_state"], str) else task["task_state"]
            for artifact_path in state.get("artifacts", []):
                p = Path(artifact_path)
                if p.exists() and p.suffix == ".html":
                    html_files.append(p)
        except (json.JSONDecodeError, TypeError):
            pass
```

**What NOT to do:**
- Do NOT add a task_type check to `/deploy` — the current design correctly ignores it
- Do NOT scan the entire workspace for HTML files — only check task-specific artifacts
- Do NOT change the glob pattern — it works for most cases

**Tests to add:**
- `test_deploy_finds_html_from_task_artifacts` — task with HTML artifact stored in `task_state` but not matching glob → `/deploy` still finds it
- **Test file:** `tests/test_handlers.py` (existing)

**Acceptance criteria:**
- `/deploy` succeeds for tasks where HTML artifacts exist but don't match the glob pattern
- Existing glob-based lookup still works as primary path

**Verify:** `pytest tests/test_handlers.py -k "deploy" -v`

**Estimated scope:** S (<15 lines)

---

## Phase 8 — Reduce False Positive Security Blocks (2.3)

**Why:** Report §2.3. Two confirmed false positives: mpmath pi computation blocked, `sys.builtin_module_names` introspection blocked. Additionally, `subprocess.run(["ls", "-la"])` blocked despite `ls` being on the safe list.

**Investigation results (confirmed):** Neither `mpmath` nor `sys.builtin_module_names` appears in any blocklist pattern (`_BLOCKED_PATTERNS` or `_CODE_BLOCKED_PATTERNS`). The blocks were caused by the *generated code* using operations that match existing Tier 4 patterns — most likely `subprocess` calls (the #1 false positive trigger per the report, 15 occurrences) or `__import__()` / `importlib.import_module()` for dynamic module introspection.

**Pre-implementation step (REQUIRED):** Before writing any code, reproduce both false positives:
1. Write a test script that `import mpmath; print(mpmath.mp.dps)` and run it through `_check_code_safety()`
2. Write a test script that `import sys; print(sys.builtin_module_names)` and run it through `_check_code_safety()`
3. If neither triggers a block, the false positive is in the **planner or auditor** (prompt-level refusal), not the code scanner. In that case, pivot to a prompt fix instead.

**What:** `tools/sandbox.py`, function `_check_code_safety()` and `_is_safe_subprocess()`.

**How (conditional on reproduction):**

**If the false positive is in the code scanner (subprocess or dynamic import patterns):**
1. Read `_check_code_safety()` to confirm `_is_safe_subprocess()` runs before any regex would block subprocess usage. If the order is wrong, fix it.
2. Verify `_is_safe_subprocess()` correctly handles `subprocess.run(["ls", "-la"])` — the first element `"ls"` is on `_SUBPROCESS_SAFE_CMDS`. If the AST extraction doesn't handle lists with flags, fix the first-element extraction.
3. For `sys.builtin_module_names` — if `__import__()` is used in the generated code, consider whether `__import__("sys")` should be allowlisted (read-only, no security impact).

**If the false positive is in the planner/auditor (prompt-level refusal):**
1. The planner's system prompt may contain overly broad security restrictions that cause Claude to refuse benign math/introspection tasks
2. Add clarifying language to the planner prompt: "Standard library modules (sys, os.path, math, statistics) and scientific computing libraries (numpy, scipy, sympy, mpmath) are safe to use for read-only operations."
3. This is a prompt-only change in `brain/nodes/planner.py`

**What NOT to do:**
- Do NOT add a blanket "safe libraries" whitelist to the code scanner — the scanner blocks operations, not library names
- Do NOT weaken Tier 4 patterns — only fix the specific false positive source
- Do NOT guess at the root cause — reproduce first, then fix
- Do NOT remove the `__import__` block entirely — it's a real attack vector; if needed, allowlist specific safe modules

**Tests to add:**
- `test_mpmath_code_not_blocked` — code with `import mpmath; mpmath.mpf(3.14)` → NOT blocked by code scanner
- `test_sys_builtin_modules_not_blocked` — code with `import sys; sys.builtin_module_names` → NOT blocked
- `test_subprocess_ls_with_flags_allowed` — code with `subprocess.run(["ls", "-la", "/tmp"])` → NOT blocked
- `test_subprocess_dangerous_still_blocked` — code with `subprocess.run(["rm", "-rf", "/"])` → blocked
- **Test file:** `tests/test_sandbox.py` (existing)

**Acceptance criteria:**
- Both mpmath and sys.builtin_module_names reproduction cases pass the scanner
- `subprocess.run(["ls", ...])` is allowed (ls is on safe list)
- `subprocess.run(["rm", ...])` is still blocked (rm is not on safe list)
- Root cause is confirmed and documented in the commit message

**Verify:** `pytest tests/test_sandbox.py -k "subprocess or mpmath or builtin" -v`

**Estimated scope:** S–M (10–30 lines, depending on which layer the fix targets)

---

## Phase 9 — Path Sanitisation + Linux Paths (2.2)

**Why:** Report §2.2 + §9.5 (P2-3). Production paths like `/Users/agentruntime1/` appear in 8+ delivered artifacts. The existing `_sanitize_paths()` in `deliverer.py:34` handles macOS paths but not Linux `/home/` paths.

**What:** `brain/nodes/deliverer.py`, function `_sanitize_paths()`, line 34.

**How:**
```python
def _sanitize_paths(text: str) -> str:
    text = re.sub(r'/(Users|home)/\w+/', '~/', text)
    text = re.sub(r'\bAdmin\.local\b', '<hostname>', text)
    return text
```

Single regex change: `r'/Users/\w+/'` → `r'/(Users|home)/\w+/'`.

**What NOT to do:**
- Do NOT sanitise paths in the actual output files — only in the Telegram delivery message
- Do NOT add hostname detection beyond `Admin.local` — it's the only known hostname
- Do NOT add path sanitisation to the executor or auditor — deliverer is the right place

**Tests to add:**
- `test_sanitize_paths_linux_home` — `/home/agentruntime1/foo` → `~/foo`
- `test_sanitize_paths_macos_still_works` — `/Users/agentruntime1/foo` → `~/foo`
- **Test file:** `tests/test_deliverer.py` (existing)

**Acceptance criteria:**
- Both `/Users/X/` and `/home/X/` are replaced with `~/`
- Existing macOS path sanitisation still works

**Verify:** `pytest tests/test_deliverer.py -k "sanitize" -v`

**Estimated scope:** S (<5 lines)

---

## Phase 10 — Over-Generation Token Limits (4.3)

**Why:** Report §4.3. Several single API calls consumed 50K–78K output tokens (~$0.75–$1.17 each). Code generation has `max_tokens=8192` at `executor.py:556` but this is overridden to 128000 when thinking is enabled (`claude_client.py:187-188`). With `thinking=True` (the default, confirmed at `executor.py:556`), the `max_tokens` parameter is completely ineffective.

**Tradeoff:** Disabling thinking (`thinking=False`) would make `max_tokens=8192` effective but sacrifices extended thinking, which is the main quality driver for complex code generation. This is a quality-vs-cost tradeoff, not a pure win.

**What:** `brain/nodes/executor.py`, code generation call at line 556 and HTML generation at line 655.

**How — Approach: scope the prompt, not the model.**

Rather than disabling thinking (which hurts quality), add explicit length guidance to the `CODE_GEN_SYSTEM` prompt. The model's internal allocation with thinking enabled tends to self-limit output length when the prompt sets clear expectations. Combined with a higher `max_tokens` that's still below the current unbounded 128000:

1. Add to `CODE_GEN_SYSTEM` (around line 109):
```python
# Add to the system prompt rules:
"- Keep code concise. A typical task should produce 50-300 lines of code\n"
"- Do NOT generate entire frameworks, full CSS libraries, or production boilerplate unless explicitly asked\n"
"- If the task says 'simple' or 'basic', keep the code under 100 lines\n"
```

2. For the `max_tokens` floor issue — rather than fighting the 128000 floor, accept that thinking-enabled calls get a large budget but guide the model to use it wisely via the prompt. The thinking budget is shared: if the model thinks for 120K tokens, it only has 8K for output — which is the desired behaviour.

3. As a secondary measure, add a post-generation length warning in the executor. After code generation, if the output exceeds 500 lines, log a warning:
```python
if code and code.count("\n") > 500:
    logger.warning("Code gen produced %d lines — possible over-generation", code.count("\n"))
```

**What NOT to do:**
- Do NOT set `thinking=False` on code generation — it sacrifices the primary quality driver for complex tasks
- Do NOT change the 128000 floor in `claude_client.py` — it's correct for thinking-enabled calls (too low and thinking consumes all budget, leaving zero for output)
- Do NOT add per-task-type token limits — adds complexity for marginal benefit
- Do NOT truncate generated code post-hoc — that would break the code

**Tests to add:**
- `test_code_gen_system_prompt_includes_length_guidance` — verify `CODE_GEN_SYSTEM` contains "50-300 lines" or similar length guidance (string assertion)
- `test_code_gen_warns_on_long_output` — mock code gen returning 600-line code → verify log warning about over-generation
- **Test file:** `tests/test_executor.py` (existing)

**Acceptance criteria:**
- `CODE_GEN_SYSTEM` includes explicit length guidance
- Over-generation (>500 lines) is logged as a warning
- Thinking remains enabled for code generation (quality preserved)

**Verify:** `pytest tests/test_executor.py -k "code_gen or over_gen" -v`

**Estimated scope:** S (<15 lines)

---

## Phase 11 — RAG Zero-Vector Poisoning Guard (9.5)

**Why:** Report §9.5 (confirmed). `rag.py:167-169` pads embedding failures with zero vectors. These zero-vector chunks pollute query results because cosine similarity with a zero vector is undefined/0, but LanceDB may still return them.

**What:** `tools/rag.py` — either filter at query time or at index time.

**Pre-implementation step (REQUIRED):** Determine the actual `_distance` value LanceDB returns for zero-vector chunks. The value could be `NaN`, `inf`, `0.0`, `2.0`, or something else — this depends on LanceDB's internal cosine distance implementation and how it handles zero-magnitude vectors.

```python
# Quick test script — run manually before implementing:
import lancedb, pyarrow as pa
db = lancedb.connect("/tmp/test_zero_vec")
schema = pa.schema([
    pa.field("text", pa.string()),
    pa.field("vector", pa.list_(pa.float32(), 4)),
])
tbl = db.create_table("test", schema=schema)
tbl.add([
    {"text": "real", "vector": [0.1, 0.2, 0.3, 0.4]},
    {"text": "zero", "vector": [0.0, 0.0, 0.0, 0.0]},
])
results = tbl.search([0.1, 0.2, 0.3, 0.4]).limit(2).to_list()
for r in results:
    print(f"{r['text']}: _distance={r['_distance']}")
```

**How (choose based on test result):**

**Option A — Query-time filter (if `_distance` is a predictable value for zero vectors):**
```python
# After LanceDB query returns results, filter by the observed threshold:
results = [r for r in results if r.get("_distance", 0) < THRESHOLD]
```

**Option B — Index-time filter (if `_distance` is unpredictable, e.g., `NaN`):**
Skip storing zero-vector chunks during `build_index()`. In the embedding function, return `None` for failed batches instead of zero vectors, and filter them out before inserting into LanceDB:
```python
# In build_index(), after embedding:
valid_entries = [
    (chunk, emb) for chunk, emb in zip(chunks, embeddings)
    if any(v != 0.0 for v in emb)
]
```
This requires re-indexing but is more robust.

**What NOT to do:**
- Do NOT hardcode a distance threshold without testing — the actual value is unknown
- Do NOT add embedding validation/retry — embedding failures are rare and the fallback handles them
- Do NOT use a different distance metric — cosine is correct for nomic-embed-text

**Tests to add:**
- `test_rag_query_excludes_zero_vector_chunks` — build index with one real embedding and one zero vector, query → only the real chunk is returned
- `test_rag_index_with_all_valid_embeddings` — normal case, no zero vectors → all chunks returned
- **Test file:** `tests/test_rag.py` (existing)

**Acceptance criteria:**
- RAG queries never return chunks that had embedding failures
- Valid chunks are still returned correctly
- The chosen threshold (or filtering approach) is documented in a code comment with the test result

**Verify:** `pytest tests/test_rag.py -k "zero_vector or query" -v`

**Estimated scope:** S (<15 lines)

---

## Phase 12 — Task Completion Log Summary (4.4)

**Why:** Report §4.4. 88% of log lines are idle `getUpdates` polling. No single log line summarises a completed task's timings, verdict, and cost.

**What:** `brain/graph.py`, function `run_task()` — after pipeline completes, log a structured summary line.

**How:**

At the end of `run_task()`, after the pipeline finishes, add:

```python
# After pipeline completes, log summary:
timings = state.get("stage_timings", [])
timing_str = " ".join(
    f"{t['name']}:{t['duration_ms']/1000:.1f}s"
    for t in timings
)
total_s = sum(t["duration_ms"] for t in timings) / 1000 if timings else 0
verdict = state.get("audit_verdict", "unknown")
logger.info(
    "Task %s completed in %.1fs [%s] verdict=%s",
    state.get("task_id", "?")[:8], total_s, timing_str, verdict,
)
```

**What NOT to do:**
- Do NOT add cost to the log line — `run_task()` doesn't have access to cost data, and adding it would require threading cost tracking through the pipeline
- Do NOT suppress `getUpdates` logging — that's controlled by the telegram library's log level, not AgentSutra code
- Do NOT add structured logging (JSON format) — plain text matches existing patterns

**Tests to add:**
- `test_run_task_logs_completion_summary` — mock a pipeline run → verify `caplog` contains "completed in" with timing and verdict
- **Test file:** `tests/test_graph.py` (existing)

**Acceptance criteria:**
- Every completed task produces one INFO line with: task ID prefix, total time, per-stage timings, and audit verdict
- Failed tasks also get a summary line

**Verify:** `pytest tests/test_graph.py -k "completion" -v`

**Estimated scope:** S (<15 lines)

---

## Phase 13 — Timeout Progress Feedback (3.4)

**Why:** Report §3.4. 5 tasks timed out at 900s, 3 shell commands timed out at 300–600s. Users watched with no feedback, sending follow-up messages 16–35s after timeouts.

**What:** `bot/handlers.py`, task processing handler (around line 818–900, where `run_task` is awaited).

**How:**

The pipeline runs in `asyncio.to_thread()` (sync nodes in a thread). The handler already `await`s the result with `asyncio.wait_for(task_future, timeout=config.LONG_TIMEOUT)`. Add a simple fire-and-forget timer that edits the status message at the 5-minute mark:

```python
# After line 816 (status_msg = ...), before the try block:
async def _send_progress():
    await asyncio.sleep(300)  # 5 minutes
    try:
        stage = get_stage(task_id) or "processing"
        await status_msg.edit_text(
            f"Still working... (task {task_id[:8]}, stage: {stage})"
        )
    except Exception:
        pass  # Message may have been edited already

progress_task = asyncio.create_task(_send_progress())

# After the pipeline completes (in the try/except/finally around line 900):
# In the finally block, cancel the timer:
progress_task.cancel()
```

This uses `asyncio.create_task` — no architectural changes needed. The timer is a simple coroutine that sleeps 5 minutes, edits the status message once, and gets cancelled when the pipeline finishes (whether by success, failure, or timeout). `get_stage()` already exists in `brain/graph.py` and returns the current pipeline stage.

**What NOT to do:**
- Do NOT add periodic polling or multiple progress updates — one update at 5 minutes is enough
- Do NOT try to edit the message during sync pipeline nodes — the timer runs independently in the event loop
- Do NOT add progress bars or percentage tracking — the pipeline stages don't have predictable durations
- Do NOT change the timeout value — this is about feedback, not timeout policy

**Tests to add:**
- `test_progress_message_sent_after_5_minutes` — mock `asyncio.sleep`, `get_stage`, and `status_msg.edit_text`, verify the progress message is sent with the current stage
- `test_progress_timer_cancelled_on_completion` — verify the timer task is cancelled when the pipeline finishes before 5 minutes
- **Test file:** `tests/test_handlers.py` (existing)

**Acceptance criteria:**
- Tasks running longer than 5 minutes get a single progress update showing the current stage
- The timer is cancelled on task completion (no stale edits)
- Fast tasks (<5 min) never see a progress message

**Verify:** `pytest tests/test_handlers.py -k "progress" -v`

**Estimated scope:** S (<15 lines)

---

## Items NOT in This Plan

These items from the report are excluded because they are operational/infra tasks (not code changes), already fixed, or out of scope:

| Item | Reason Excluded |
|------|----------------|
| 1.1 Preview server `--bind 127.0.0.1` | Already fixed in current codebase (sandbox.py:115-116 shows the fix) |
| 1.3 Code scanner bypass | Fixed v8.7.0 (AST constant folding + written-file scanning) |
| 1.4 Firebase PATH | Ops task — fix PATH in launchd plist, not a code change |
| 4.2 RAG context layer | Implemented v8.7.0 |
| 4.5 Docker sandbox image | Ops task — run `./scripts/build_sandbox.sh` |
| 3.3 Simple question fast path | Conflicts with invariant #1 (5-stage pipeline is fixed). Report §7 explicitly warns against this. |
| 3.6 Cost monitoring/alerts | Low priority, no code change needed (existing `/cost` command works) |
| Stop hook target fix | Trivial config change, not a code change: update `.claude/settings.json` to target `SESSION_LOG.md` |
| 9.9 Retry loop learning | Report §9.9 lists this as a "Limitation Discovered", not a prioritized fix. Audit feedback goes to executor but not planner — planner re-plans from scratch. Fixing this requires threading audit feedback into the planner's prompt on retry, which changes the retry contract across 3 nodes. Significant scope for uncertain benefit (planner may still re-plan differently). Defer to v8.9.0 after Phase 0a eliminates the F-1 cascade that made this visible. |
| Complexity ceiling (under-generation) | Not in the report's priority matrix. Test suite tests 16.1–16.3 expose this but the report only covers over-generation (§4.3, addressed in Phase 10). Under-generation (task exceeds single-generation capacity) is a fundamental model limitation, not a code fix. |

---

## Test Suite Alignment Note

`Ultimate_Test_Suite.md` labels tests 17.7–17.10 as `[NEW v8.7]` but their corresponding features are planned for v8.8.0:

| Test | Label | Actual Status | Plan Phase |
|------|-------|---------------|------------|
| 17.7 (timeout progress) | [NEW v8.7] | **Not implemented** — will fail on v8.7.0 | Phase 13 |
| 17.8 (path sanitisation) | [NEW v8.7] | Partially implemented — `/Users/` works, `/home/` doesn't | Phase 9 |
| 17.9 (anti-fabrication) | [NEW v8.7] | Partially implemented — warning exists but is weak | Phase 2 |
| 17.10 (credential filter) | [NEW v8.7] | Partially implemented — ghp_, AKIA work; Anthropic/Slack/Telegram don't | Phase 1 |

**Action:** After v8.8.0 implementation, update `Ultimate_Test_Suite.md` to relabel these as `[NEW v8.8]` or `[UPDATED v8.8]`.

**Pre-implementation verification for Phase 6:** Confirm that test 17.6 (chain `BLOCKED:` detection) passes on current v8.7.0 code. That test covers the *scanner-level* block case (where `BLOCKED:` is set in `execution_result`). Phase 6 fixes the *different* case: planner-level polite refusals that bypass all three gate conditions. Both cases need to work after Phase 6.

---

## Rollback Notes

**If a phase breaks something:**

1. **Each phase is independently revertable.** Changes are scoped to 1–3 files per phase. `git revert` the phase's commit.

2. **Phase 0a is the riskiest.** Shell scripts without shebangs (invoked via `bash script.sh`) will skip the if/fi truncation check. Mitigation: paren/bracket/brace/string checks still catch most truncation regardless of language. See "Known gap" note in Phase 0a.

3. **Phase 6 touches 3 files** (`brain/state.py`, `brain/nodes/planner.py`, `bot/handlers.py`). If the planner prefix-matching produces false positives on legitimate plans, revert the planner change and fall back to a simpler approach (e.g., check if `artifacts` is empty AND the task was a known-dangerous pattern).

4. **Phase 8 has a required pre-implementation step.** Must reproduce the false positives before writing code. If reproduction shows the blocks are in the planner/auditor (not the code scanner), the fix changes entirely — prompt change instead of scanner change.

5. **Phase 11 has a required pre-implementation step.** Must test LanceDB's actual `_distance` value for zero vectors before choosing the filtering approach. See test script in Phase 11.

6. **Phase 2 is low-confidence.** Both changes are prompt-based. If fabrication persists after implementation, consider a structural fix in v8.9.0 (e.g., executor-level detection of "code that only prints text and creates no real output").

7. **Pre-flight for every phase:** Run `just test-quick` before and after. If any existing test breaks, fix or revert before proceeding to the next phase.

8. **Commit strategy:** One commit per phase with conventional commit format (`fix:`, `feat:`). This allows clean `git revert` per phase.

---

*Plan ends here. No code changes have been made.*
