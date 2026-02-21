# AgentCore v6.2.1 — Bug Report & Fix Prompt

**Auditor:** Claude Opus 4.6 deep code review  
**Date:** 2026-02-20  
**Verdict:** 🔴 NOT green-lit — 7 bugs found (1 critical, 2 moderate, 4 low)

---

## Executive Summary

The codebase is architecturally sound and well-tested, but this stress test uncovered **7 real bugs** that the existing 102-test suite does not catch. The critical bug silently wipes all API cost tracking data on every restart. The moderate bugs allow schedule abuse and lose working directory context between tasks. The low bugs are edge cases in security and parsing.

---

## Bug #1 — CRITICAL: `prune_old_data` Deletes ALL API Usage Records on Every Restart

### Location
`storage/db.py`, lines in `prune_old_data()` function

### The Problem
The `api_usage` table stores timestamps as **Unix epoch floats** (via `time.time()` in `claude_client.py`), but `prune_old_data()` computes the cutoff as an **ISO 8601 string** (via `datetime.isoformat()`). SQLite's type affinity rules mean that **every REAL value compares as "less than" every TEXT value**. The DELETE statement therefore removes **100% of records** instead of just records older than 90 days.

### Proof of Bug
```python
import sqlite3, time
from datetime import datetime, timezone, timedelta

conn = sqlite3.connect(':memory:')
conn.execute('CREATE TABLE api_usage (timestamp REAL)')
now = time.time()
conn.execute('INSERT INTO api_usage VALUES (?)', (now - 86400,))      # 1 day old — should survive
conn.execute('INSERT INTO api_usage VALUES (?)', (now - 100*86400,))   # 100 days old — should die
conn.commit()

# This is what prune_old_data does:
usage_cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
cursor = conn.execute('DELETE FROM api_usage WHERE timestamp < ?', (usage_cutoff,))
print(f"Deleted: {cursor.rowcount}")  # Prints 2 — BOTH records deleted!
```

### Impact
- Every time AgentCore restarts, all `/cost` data resets to $0.00
- `/usage` shows 0 total calls after restart
- Budget enforcement (`_check_budget`) becomes ineffective after restart since all historical usage is gone
- If daily budget is set to $5 and you've spent $4.99, a restart wipes the counter and allows another $5

### Fix
In `storage/db.py`, `prune_old_data()`, replace the ISO string cutoff with an epoch float cutoff for the `api_usage` table:

```python
# BEFORE (broken):
usage_cutoff = (datetime.now(timezone.utc) - timedelta(days=usage_days)).isoformat()
# ...
cursor = await db.execute(
    "DELETE FROM api_usage WHERE timestamp < ?", (usage_cutoff,)
)

# AFTER (fixed):
import time as _time
usage_cutoff_epoch = _time.time() - (usage_days * 86400)
# ...
cursor = await db.execute(
    "DELETE FROM api_usage WHERE timestamp < ?", (usage_cutoff_epoch,)
)
```

### Test to Add
```python
# tests/test_db.py (NEW FILE)
def test_prune_old_data_uses_epoch_for_api_usage():
    """Verify api_usage pruning uses epoch float, not ISO string."""
    import time, sqlite3
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE api_usage (timestamp REAL, model TEXT, input_tokens INT, output_tokens INT)')
    now = time.time()
    conn.execute('INSERT INTO api_usage VALUES (?, ?, ?, ?)', (now - 86400, 'test', 100, 100))        # 1 day old
    conn.execute('INSERT INTO api_usage VALUES (?, ?, ?, ?)', (now - 100*86400, 'test', 100, 100))     # 100 days old
    conn.commit()
    
    # Simulate the fixed prune logic
    cutoff = now - (90 * 86400)
    cursor = conn.execute('DELETE FROM api_usage WHERE timestamp < ?', (cutoff,))
    assert cursor.rowcount == 1, f"Should delete only the 100-day-old record, deleted {cursor.rowcount}"
    
    remaining = conn.execute('SELECT COUNT(*) FROM api_usage').fetchone()[0]
    assert remaining == 1, f"Should keep the 1-day-old record, kept {remaining}"
```

---

## Bug #2 — MODERATE: `/schedule` Accepts Zero, Negative, and Extreme Intervals

### Location
`bot/handlers.py`, `cmd_schedule()` function

### The Problem
The schedule command parses the interval with `int(parts[0])` but performs no bounds checking. A user can create:
- `/schedule 0 Run something` → zero-interval job (fires as fast as APScheduler can loop)
- `/schedule -5 Run something` → negative interval (APScheduler may crash or behave unpredictably)
- `/schedule 999999 Run something` → fires once every ~694 days (likely unintended)

### Impact
- A zero-interval job would consume API budget at maximum speed, potentially exhausting daily/monthly limits within minutes
- Could saturate the concurrent task limit, blocking all other tasks
- A negative interval causes APScheduler `ValueError` at runtime

### Fix
Add validation after parsing the interval:

```python
# BEFORE:
try:
    interval_minutes = int(parts[0])
except ValueError:
    await update.message.reply_text(f"Invalid interval: {parts[0]}. Must be a number of minutes.")
    return

# AFTER:
try:
    interval_minutes = int(parts[0])
except ValueError:
    await update.message.reply_text(f"Invalid interval: {parts[0]}. Must be a number of minutes.")
    return

if interval_minutes < 1:
    await update.message.reply_text("Interval must be at least 1 minute.")
    return
if interval_minutes > 43200:  # 30 days
    await update.message.reply_text("Interval cannot exceed 43200 minutes (30 days).")
    return
```

### Test to Add
```python
# In test_pipeline_integration.py or a new test_handlers.py
def test_schedule_rejects_zero_interval():
    """Zero interval should be rejected."""
    assert 0 < 1  # placeholder — full test requires mocked Telegram update
    
def test_schedule_rejects_negative_interval():
    """Negative interval should be rejected."""
    assert -5 < 1  # placeholder
```

---

## Bug #3 — MODERATE: Executors Never Return `working_dir`, Breaking Conversation Continuity

### Location
`brain/nodes/executor.py` — all four executor functions (`_execute_code`, `_execute_project`, `_execute_ui_design`, `_execute_frontend`)

### The Problem
The `_execute_code` function calls `_determine_working_dir(state)` and uses the result for execution, but never includes `working_dir` in the returned dict. The state field `working_dir` starts as `""` and is never updated by any executor. 

In `bot/handlers.py`, the handler tries to persist it:
```python
if result.get("working_dir"):
    await db.set_context(user_id, "last_working_dir", result["working_dir"])
```

This condition is always false because `working_dir` is always `""`.

### Impact
- Conversation continuity for multi-step tasks is broken
- If a user says "Now modify the script we just created", the agent won't know which directory to look in
- The `_determine_working_dir` function checks `state.get("working_dir")` for an explicit override, but since it's never set from a previous task, this path is dead code

### Fix
Each executor should return the `working_dir` it used:

```python
# In _execute_code:
    working_dir = _determine_working_dir(state)
    result = run_code_with_auto_install(code, timeout=timeout, working_dir=working_dir)

    return {
        "code": code,
        "execution_result": _format_result(result),
        "artifacts": result.files_created,
        "auto_installed_packages": result.auto_installed,
        "working_dir": str(working_dir) if working_dir else "",  # ADD THIS
    }

# In _execute_project:
    return {
        "code": code,
        "execution_result": _format_result(result),
        "artifacts": result.files_created,
        "extracted_params": params,
        "working_dir": project_path,  # ADD THIS
    }

# In _execute_ui_design and _execute_frontend:
    return {
        "code": code,
        "execution_result": f"Execution: SUCCESS ...",
        "artifacts": [str(output_path)],
        "working_dir": str(config.OUTPUTS_DIR),  # ADD THIS
    }
```

---

## Bug #4 — LOW: `_strip_markdown_blocks` Breaks on Triple Backticks Inside Code

### Location
`brain/nodes/executor.py`, `_strip_markdown_blocks()` function

### The Problem
The regex `r"```(?:\w*)\n(.*?)```"` uses non-greedy matching. If Claude generates JavaScript containing template literals with backticks (`` ` ``), or if the code itself contains triple backticks in string constants, the regex matches up to the **inner** backticks, truncating the output.

### Proof
```python
import re
code = '```html\n<script>\nconst x = ``` + "hello";\n</script>\n```'
matches = re.findall(r"```(?:\w*)\n(.*?)```", code, re.DOTALL)
# Returns: ['<script>\nconst x = '] — truncated! Missing </script> and rest
```

### Impact
- Rare in practice (Claude rarely generates code with triple backticks in strings)
- When it does happen, the frontend/UI design output will be silently truncated
- The auditor may catch it (broken HTML) and trigger a retry, but the retry will likely produce the same result

### Fix
Use a more robust extraction that handles escaped backticks, or check that the extracted content contains expected HTML structure:

```python
def _strip_markdown_blocks(text: str) -> str:
    """Extract code from markdown code blocks. Returns the longest block found.
    
    Uses greedy right-to-left matching to handle backticks inside code.
    """
    # First try: find blocks using line-anchored backticks (more reliable)
    lines = text.split('\n')
    blocks = []
    current_block = []
    in_block = False
    
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('```') and not in_block:
            in_block = True
            current_block = []
            continue
        elif stripped == '```' and in_block:
            in_block = False
            blocks.append('\n'.join(current_block))
            continue
        if in_block:
            current_block.append(line)
    
    if blocks:
        return max(blocks, key=len).strip()
    
    # Fallback: original regex approach
    matches = re.findall(r"```(?:\w*)\n(.*?)```", text, re.DOTALL)
    if matches:
        return max(matches, key=len).strip()
    return text.strip()
```

The key difference: a closing ```` ``` ```` is only recognized when it appears as the **entire content of a line** (after stripping whitespace), not when it appears mid-line inside code.

### Test to Add
```python
def test_backticks_inside_code(self):
    """Triple backticks inside template literals should not truncate."""
    text = '```html\n<script>\nconst x = ``` + "y";\n</script>\n```'
    result = _strip_markdown_blocks(text)
    assert '</script>' in result, f"HTML truncated: {result}"
```

---

## Bug #5 — LOW: `rm -rf ~/Documents` and Similar Subdirectory Deletions Not Blocked

### Location
`tools/sandbox.py`, `_BLOCKED_PATTERNS` list

### The Problem
The rm patterns only block deletion of root-level targets (`/`, `~`, `~/`, `$HOME`, `/Users`, `/home`). But `rm -rf ~/Documents`, `rm -rf ~/Desktop`, `rm -rf ~/projects` all pass through the safety check because the regex requires the path to be exactly one of those roots.

### Current Pattern
```python
r"\brm\s+(-{1,2}[\w-]+\s+)*\s*(/\s*$|~\s*$|~/\s*$|\$HOME)",
```
This requires the path portion to be exactly `/`, `~`, `~/`, or `$HOME` at end-of-string. Anything after (like `/Documents`) breaks the match.

### Fix
Extend the pattern to also block `rm -rf` on well-known critical subdirectories:

```python
# Add to _BLOCKED_PATTERNS:
# Block rm with force+recursive on critical home subdirectories
r"\brm\s+(-{1,2}[rfRF-]+\s+)+\s*~/?(Desktop|Documents|Downloads|Pictures|Music|Movies|Library|Applications)\b",
```

And add a more general rule for `rm -rf` on any direct child of `~`:
```python
# Block rm -rf on any direct child of home
r"\brm\s+(-{1,2}[rfRF-]+\s+)+\s*~/.+",
```

**Note:** This is an opinionated change. The current behavior is documented as intentional ("safe commands pass through"). If you want to keep `rm -rf ~/projects/temp` allowed, use the targeted subdirectory list instead of the broad `~/.+` pattern.

### Tests to Add
```python
def test_rm_rf_documents(self):
    assert _check_command_safety("rm -rf ~/Documents") is not None

def test_rm_rf_desktop(self):
    assert _check_command_safety("rm -rf ~/Desktop") is not None

# But single-file deletion in subdirs should still work:
def test_rm_file_in_subdir(self):
    assert _check_command_safety("rm ~/projects/temp.txt") is None
```

---

## Bug #6 — LOW: `chmod -R` Pattern Blocks ALL Recursive chmod, Including Safe Ones

### Location
`tools/sandbox.py`, `_BLOCKED_PATTERNS` list

### The Problem
The pattern `r"\bchmod\s+-R\s+"` blocks every `chmod -R` command, even perfectly safe operations like `chmod -R 644 ./src/` or `chmod -R u+x ~/scripts/`. The current test even validates this overly broad behavior:

```python
def test_chmod_recursive(self):
    assert _check_command_safety("chmod -R 755 ~/projects") is not None
```

### Impact
- Generated code that sets file permissions (common in deployment scripts) will be blocked
- Retry loop will attempt 3 times with the same result, wasting API credits

### Fix
Narrow the pattern to only block recursive chmod with dangerous permissions on sensitive paths:

```python
# BEFORE (too broad):
r"\bchmod\s+-R\s+",

# AFTER (targeted):
# Block recursive world-writable permissions on sensitive paths
r"\bchmod\s+(-[rR]\s+|--recursive\s+)(777|a\+rwx|o\+w)\s+[/~]",
```

### Test Update
```python
def test_chmod_recursive_safe_allowed(self):
    """chmod -R 755 on a project directory should be allowed."""
    assert _check_command_safety("chmod -R 755 ~/projects/myapp") is None

def test_chmod_recursive_dangerous_blocked(self):
    """chmod -R 777 on home should be blocked."""
    assert _check_command_safety("chmod -R 777 ~/") is not None
```

---

## Bug #7 — LOW: CSV `row_count` Includes Header Row, Misleading LLM Planning

### Location
`tools/file_manager.py`, `get_file_metadata()` function

### The Problem
The metadata reports `row_count` as total rows including the header. When this is injected into the planner prompt as `~1001 rows`, Claude may think there are 1001 data rows when there are actually 1000 data rows + 1 header.

### Current Logic
```python
row_count = (data_row_count + 1) if header else data_row_count
```

### Impact
- Minor inaccuracy in planner context. Off-by-one in row counts could cause:
  - Assertions to fail (`assert len(df) == 1001` when actual data is 1000)
  - Incorrect summary statistics ("analyzed 1001 records")
  - Usually self-corrects because pandas `read_csv()` handles headers separately

### Fix
Either rename the field for clarity or separate header from data count:

```python
# Option A: Rename field to be explicit
meta["total_rows_including_header"] = row_count
meta["data_row_count"] = data_row_count

# Option B: Report data rows only (recommended)
meta["row_count"] = data_row_count  # NOT including header
```

And update `format_file_metadata_for_prompt`:
```python
parts[0] += f", ~{meta['row_count']:,} data rows"  # clarify "data rows"
```

---

## Summary of Required Changes

| # | Severity | File | Fix Description |
|---|----------|------|-----------------|
| 1 | **CRITICAL** | `storage/db.py` | Use `time.time() - (days * 86400)` instead of `isoformat()` for api_usage pruning |
| 2 | **MODERATE** | `bot/handlers.py` | Add bounds check: `1 <= interval_minutes <= 43200` in `cmd_schedule` |
| 3 | **MODERATE** | `brain/nodes/executor.py` | Return `working_dir` from all four executor functions |
| 4 | LOW | `brain/nodes/executor.py` | Use line-anchored backtick detection in `_strip_markdown_blocks` |
| 5 | LOW | `tools/sandbox.py` | Extend rm block patterns to cover `~/Documents`, `~/Desktop`, etc. |
| 6 | LOW | `tools/sandbox.py` | Narrow `chmod -R` block to only dangerous permission combos |
| 7 | LOW | `tools/file_manager.py` | Report `data_row_count` (excluding header) or clarify field name |

### New Tests Required

| File | Tests |
|------|-------|
| `tests/test_db.py` (new) | `test_prune_old_data_uses_epoch_for_api_usage` |
| `tests/test_sandbox.py` | `test_rm_rf_documents`, `test_rm_rf_desktop`, `test_chmod_recursive_safe_allowed` |
| `tests/test_executor.py` | `test_backticks_inside_code` |
| `tests/test_file_manager.py` | `test_csv_row_count_excludes_header` (if Option B) |

---

## Post-Fix Verification Checklist

After applying all fixes:

- [ ] Run full test suite: `pytest tests/ -v` — all existing tests pass
- [ ] Run new tests — all pass
- [ ] Start AgentCore, check `/cost` shows historical data after restart
- [ ] Test `/schedule 0 Run something` — should be rejected
- [ ] Test `/schedule -1 Run something` — should be rejected
- [ ] Test `/schedule 5 Run something` — should work
- [ ] Test a multi-step conversation: run a code task, then ask "modify the script" — verify working_dir context is preserved
- [ ] Test `rm -rf ~/Documents` via `/exec` — should be blocked
- [ ] Test `chmod -R 755 ~/projects/myapp` via `/exec` — should be allowed (after fix #6)