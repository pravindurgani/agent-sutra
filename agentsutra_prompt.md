# AgentSutra — Stabilisation, Rename & Enhancement Prompt

> **Target:** Claude Code operating on the repository at `~/Desktop/AgentCore/`
> **Codebase:** Python 3.11, ~7,800 LOC, 35 modules, 326 tests (336 after recent additions)
> **Runtime:** Mac Mini M2 16GB, launchd daemon, Telegram bot interface

---

## Context for Claude Code

You are working on a personal AI agent system currently named "AgentCore" that is being renamed to **AgentSutra**. It is a Telegram-driven LangGraph pipeline (Classify → Plan → Execute → Audit → Deliver) running on a Mac Mini M2 as a background daemon. The codebase has gone through multiple fix cycles (v6.8 → v6.12) and the latest code already contains critical fixes for `stdin=subprocess.DEVNULL` on all 11 subprocess calls, environment error short-circuiting in the auditor, project dependency bootstrapping, and unified PIP_NAME_MAP.

The work in this prompt has **three phases**, to be done **in this order**:
1. Fix the remaining production bug ("Claude returned no text content")
2. Rename everything from AgentCore to AgentSutra
3. Apply targeted refinements

**Read the full prompt before starting any work.** The "What NOT To Do" section is as important as the fixes.

---

## PHASE 1 — Fix: "Claude returned no text content" (CRITICAL)

### Evidence

Production logs from 2026-02-21/22 show **4 complete task failures** caused by this error:

| Time (UTC) | Task | Stage when it failed |
|---|---|---|
| 2026-02-22 00:02 | PulseMetrics landing page (ui_design) | After "Creating execution plan..." |
| 2026-02-22 00:35 | PyPI scraper (code) | After "Generating and running code..." |
| 2026-02-22 01:09 | Tutorial generation (code) | After "Generating and running code..." |
| 2026-02-22 09:18 | Lisbon weather (code) | After "Generating and running code..." |

Each time, the user saw `Task failed: Claude returned no text content` and the entire task was lost. No retry. No partial result. The user had to manually re-send the prompt.

### Root Cause

In `tools/claude_client.py`, the `call()` method (lines ~167–236) has a retry loop that catches `RateLimitError`, `APITimeoutError`, and `APIError`. But when Claude returns a response containing only thinking blocks and no text blocks (or an empty content array), the code raises `RuntimeError` — which is **not caught** by any of the `except` blocks. The error propagates immediately through the entire pipeline, through `graph.py`, and into `handlers.py` line 687 where it becomes a user-visible "Task failed" message.

```python
# CURRENT CODE — RuntimeError escapes the retry loop
if not response.content:
    raise RuntimeError("Claude returned empty response")    # NOT CAUGHT

text_parts = []
for block in response.content:
    if block.type == "text":
        text_parts.append(block.text)
if not text_parts:
    raise RuntimeError("Claude returned no text content")   # NOT CAUGHT
```

The three `except` blocks only catch Anthropic SDK exceptions (`RateLimitError`, `APITimeoutError`, `APIError`). The `RuntimeError` flies straight past all of them.

### Fix

**File:** `tools/claude_client.py`, inside the `call()` method's retry loop.

Add a new except block **after** the existing `APIError` handler, **before** the end of the for loop, that catches these specific RuntimeErrors and retries them:

```python
        except APIError as e:
            if attempt == config.API_MAX_RETRIES - 1:
                logger.error("Claude API error after %d attempts: %s", config.API_MAX_RETRIES, e)
                raise
            wait = 2 ** attempt
            logger.warning("API error: %s, retrying in %ds", e, wait)
            time.sleep(wait)

        # — NEW: Retry on empty/thinking-only responses —
        except RuntimeError as e:
            if "no text content" in str(e) or "empty response" in str(e):
                if attempt == config.API_MAX_RETRIES - 1:
                    logger.error("Claude returned no usable text after %d attempts", config.API_MAX_RETRIES)
                    raise
                wait = 2 ** (attempt + 1)
                logger.warning("Empty/thinking-only response, retrying in %ds (attempt %d)", wait, attempt + 1)
                time.sleep(wait)
            else:
                raise  # Don't swallow unrelated RuntimeErrors
```

### Why this works

Thinking-only responses are transient. The model sometimes produces an extended thinking block but stops generation before emitting text — usually due to server-side load or context length pressure. A retry with the same prompt almost always succeeds. This is exactly the same retry pattern used for `RateLimitError` and `APITimeoutError`.

### Why not just wrap the whole thing in a broader catch

The retry loop is designed so that only known-transient errors are retried. A generic `except Exception` would mask real bugs (bad prompts, auth failures, schema errors). The `RuntimeError` catch is scoped tightly to the two specific messages that indicate transient API behaviour.

### Tests to add

**File:** `tests/test_claude_client.py` (create if it doesn't exist, or add to existing)

```python
class TestCallRetryOnEmptyResponse:
    """call() should retry when Claude returns no text content."""

    def test_retries_on_no_text_content(self):
        """Thinking-only response on first attempt, normal response on second."""
        # Mock the Anthropic client to return thinking-only first, then text
        ...

    def test_retries_on_empty_response(self):
        """Empty content array on first attempt, normal response on second."""
        ...

    def test_gives_up_after_max_retries(self):
        """Raises RuntimeError after API_MAX_RETRIES attempts of empty responses."""
        ...

    def test_does_not_catch_unrelated_runtime_errors(self):
        """RuntimeError with different message propagates immediately."""
        ...
```

Use `unittest.mock.patch` to mock `_get_client().messages.create` with appropriate return values. The mock response for a thinking-only case should have `content=[ThinkingBlock(type="thinking", thinking="...")]` with no text blocks.

### Verification after fix

Run: `python -m pytest tests/test_claude_client.py -v`

Then re-run Test 1.4 (PulseMetrics landing page) from the test suite. It should no longer fail with "no text content" — either it succeeds or it retries and then succeeds.

---

## PHASE 2 — Rename: AgentCore → AgentSutra

### Naming Convention

| Context | Old | New |
|---|---|---|
| Human-readable brand | AgentCore | AgentSutra |
| Python identifiers | `agentcore` | `agentsutra` |
| UPPER_CASE constants | `AGENTCORE` | `AGENTSUTRA` |
| Docker image | `agentcore-sandbox` | `agentsutra-sandbox` |
| Container names | `agentcore-{uuid}` | `agentsutra-{uuid}` |
| Database file | `agentcore.db` | `agentsutra.db` |
| Log file | `agentcore.log` | `agentsutra.log` |
| Heredoc delimiters | `AGENTCORE_EOF_` | `AGENTSUTRA_EOF_` |
| GitHub repo | `AgentCore` | `agent-sutra` |
| Directory name | `AgentCore/` | `AgentSutra/` |

### Files requiring changes (exhaustive list)

**Python source (17 references across 7 files):**

| File | Line(s) | What to change |
|---|---|---|
| `config.py:13` | `"agentcore.db"` | → `"agentsutra.db"` |
| `config.py:43` | Comment: "AgentCore's own credentials" | → "AgentSutra's own credentials" |
| `config.py:80` | `"agentcore-sandbox"` | → `"agentsutra-sandbox"` |
| `main.py:19` | `"agentcore.log"` | → `"agentsutra.log"` |
| `main.py:25` | `logging.getLogger("agentcore")` | → `logging.getLogger("agentsutra")` |
| `main.py:47` | `"AgentCore starting up"` | → `"AgentSutra starting up"` |
| `main.py:84` | `"AgentCore stopped"` | → `"AgentSutra stopped"` |
| `bot/handlers.py:93` | `"AgentCore v6 is online."` | → `"AgentSutra v7 is online."` |
| `brain/nodes/executor.py:299` | `AGENTCORE_EOF_` | → `AGENTSUTRA_EOF_` |
| `tools/sandbox.py:299` | `f"agentcore-{uuid...}"` | → `f"agentsutra-{uuid...}"` |
| `tools/sandbox.py:379` | `f"agentcore-pip-{uuid...}"` | → `f"agentsutra-pip-{uuid...}"` |
| `tools/sandbox.py:819` | Comment: "AgentCore's own credentials" | → "AgentSutra's own credentials" |
| `tools/claude_client.py:29` | Comment: "shares agentcore.db" | → "shares agentsutra.db" |

**Test files (4 references):**

| File | Line(s) | What to change |
|---|---|---|
| `tests/test_docker_sandbox.py:4-5` | Comments: `agentcore-sandbox` | → `agentsutra-sandbox` |
| `tests/test_docker_sandbox.py:44` | `"agentcore-sandbox"` string | → `"agentsutra-sandbox"` |
| `tests/test_docker_sandbox.py:56` | `"agentcore-sandbox"` string | → `"agentsutra-sandbox"` |

**Shell scripts (34 references across 3 files):**

| File | What to change |
|---|---|
| `scripts/build_sandbox.sh` | All `agentcore-sandbox` → `agentsutra-sandbox`, all `AGENTCORE_DIR` → `AGENTSUTRA_DIR`, all `AgentCore` in comments → `AgentSutra` |
| `scripts/secure_deploy.sh` | All `AGENTCORE_DIR` → `AGENTSUTRA_DIR`, all `AgentCore` in comments → `AgentSutra` |
| `scripts/monthly_maintenance.sh` | All `AGENTCORE_DIR` → `AGENTSUTRA_DIR`, `agentcore.db` → `agentsutra.db`, all `AgentCore` in comments → `AgentSutra` |

**Configuration files:**

| File | What to change |
|---|---|
| `Dockerfile` | Comments: `AgentCore Sandbox` → `AgentSutra Sandbox`, `agentcore-sandbox` → `agentsutra-sandbox` |
| `projects.yaml:1` | Comment: `# AgentCore Project Registry` → `# AgentSutra Project Registry` |
| `projects_macmini.yaml:1` | Comment: `# AgentCore Project Registry` → `# AgentSutra Project Registry` |
| `.claude/settings.local.json` | All `/AgentCore` paths → `/AgentSutra` |
| `prompt.md` | All `AgentCore` → `AgentSutra`, `agentcore` → `agentsutra` |

**Documentation (116 references across 3 files):**

| File | Action |
|---|---|
| `README.md` | Replace all `AgentCore` → `AgentSutra`, update GitHub URL to `agent-sutra`, update any `agentcore` → `agentsutra` |
| `AGENTCORE.md` | Rename file to `AGENTSUTRA.md`, replace all `AgentCore` → `AgentSutra`, all `agentcore` → `agentsutra` |
| `USECASES.md` | Replace all `AgentCore` → `AgentSutra` |

### Execution order for the rename

1. **Python source files first** — these are what the running system imports
2. **Test files** — so tests still pass after the rename
3. **Shell scripts and Dockerfile**
4. **Configuration files** (projects.yaml, .env.example, etc.)
5. **Documentation** (README.md, AGENTCORE.md → AGENTSUTRA.md, USECASES.md)
6. **Run the full test suite** to verify nothing broke: `python -m pytest tests/ -v`

### Database migration

The database file changes from `storage/agentcore.db` to `storage/agentsutra.db`. On the Mac Mini:

```bash
# One-time migration (run BEFORE starting the renamed service)
cp storage/agentcore.db storage/agentsutra.db
# Keep the old file as backup for 30 days, then delete
```

The scheduler database (`storage/scheduler.db`) does NOT need renaming — it has no `agentcore` references in its name.

**SQL sanity check:** After the copy, grep all `.py` files for any SQL strings that might contain hardcoded `agentcore` references (table names, comments inside queries, or string literals used in WHERE clauses). This is unlikely given the codebase uses generic table names, but verify with:

```bash
grep -rn "agentcore" --include="*.py" | grep -i "sql\|query\|SELECT\|INSERT\|CREATE\|TABLE"
# Expected: 0 results (all references should be in config/filenames, not SQL)
```

### Docker image rebuild

After renaming the Dockerfile references:

```bash
# Build new image
docker build -t agentsutra-sandbox .

# Optionally remove old image
docker rmi agentcore-sandbox
```

### Git operations

```bash
# 1. Rename local directory
cd ~/Desktop
mv AgentCore AgentSutra

# 2. Create new GitHub repo: pravindurgani/agent-sutra
# Do this in the GitHub web UI first

# 3. Update remote
cd ~/Desktop/AgentSutra
git remote set-url origin https://github.com/pravindurgani/agent-sutra.git

# 4. Commit the rename
git add -A
git commit -m "Rename AgentCore → AgentSutra

- All Python source: config, main, handlers, executor, sandbox, claude_client
- Docker image: agentcore-sandbox → agentsutra-sandbox
- Database: agentcore.db → agentsutra.db
- Log: agentcore.log → agentsutra.log
- All shell scripts, tests, and documentation
- GitHub repo: AgentCore → agent-sutra"

git push -u origin main

# 5. Archive old repo
# In GitHub: pravindurgani/AgentCore → Settings → Archive
```

---

## PHASE 3 — Targeted Refinements

These are smaller improvements that should be done **after** Phase 1 and Phase 2 are complete and tested.

### 3A. Explicit artifact declaration in code generation prompts

**Problem:** Artifact detection relies on filesystem mtime comparison before/after execution. This is fragile — it can miss files, pick up venv noise, and fails entirely when Docker is used with different mount semantics.

**Fix — two parts (both required):**

**Part 1 — Tell the model to declare artifacts.** In `_execute_code()`, find where the system prompt is constructed and sent to Claude for code generation. Append this instruction so the model knows it must emit the declaration line in every script it writes:

```python
# After the system prompt is built, before calling Claude:
system += "\n\nAt the very end of your script, print exactly one line: ARTIFACTS: followed by a JSON array of output filenames your script created, e.g.:\nprint('ARTIFACTS:', json.dumps(['output.csv', 'chart.png']))"
```

Verify this instruction actually reaches the `messages.create()` call — trace the `system` variable through to the Claude API call to confirm it is not overwritten or truncated before use. The model cannot declare artifacts if it never sees the instruction.

**Part 2 — Parse the declaration from stdout.** In `_format_result()` (or wherever artifacts are collected after execution), add a parser that looks for the line the model was told to emit:

```python
import json, re

def _extract_declared_artifacts(stdout: str, working_dir: Path) -> list[str]:
    """Extract artifacts declared by the script via ARTIFACTS: line."""
    match = re.search(r'^ARTIFACTS:\s*(\[.*\])\s*$', stdout, re.MULTILINE)
    if not match:
        return []
    try:
        names = json.loads(match.group(1))
        return [str(working_dir / n) for n in names if (working_dir / n).exists()]
    except (json.JSONDecodeError, TypeError):
        return []
```

Use declared artifacts as primary source. Fall back to mtime scanning only when no declaration is found.

**Do NOT** remove the existing mtime-based scanner — it's the fallback for project tasks, shell tasks, and any code that doesn't include the declaration line.

### 3B. Version string

Add a `__version__` to the project so the `/start` command and logs show which version is running.

**File:** `config.py`, add at top:

```python
VERSION = "7.0.0"
```

**File:** `bot/handlers.py`, update the `/start` response:

```python
f"AgentSutra v{config.VERSION} is online.\n\n"
```

**File:** `main.py`, update startup log:

```python
logger.info("AgentSutra v%s starting up", config.VERSION)
```

### 3C. Healthcheck for project venvs

Add a lightweight venv check to `/health` that verifies each registered project's venv exists and its Python is executable:

```python
# In the /health handler
for name, proj in projects.items():
    venv = proj.get("venv")
    if venv:
        python_bin = Path(venv) / "bin" / "python3"
        if not python_bin.exists():
            health_issues.append(f"Project '{name}': venv python not found at {python_bin}")
```

This catches dependency rot before it causes task failures.

---

## What NOT To Do

These constraints are **hard rules**, not suggestions. Violating them will make the system worse.

### Do NOT over-engineer

- **No plugin system.** The project registry (`projects.yaml`) is the extension mechanism. Adding plugin discovery, lifecycle hooks, or dynamic loading creates maintenance burden with zero user benefit for a single-user system.
- **No multi-user support.** The entire architecture assumes one user (ALLOWED_USER_IDS). Do not add authentication, tenant isolation, per-user sandboxes, or per-user budgets. If someone else wants AgentSutra, they deploy their own instance.
- **No MCP support.** AgentSutra's power comes from unrestricted code execution. MCP solves the problem of agents that can't execute code — AgentSutra doesn't have that problem. The project registry provides the same tool-advertisement capability.
- **No model-agnostic abstraction.** Do not add OpenAI, Gemini, or Llama support. The cross-model audit (Sonnet executor + Opus auditor) is specifically tuned to Anthropic's model family. A multi-provider abstraction layer creates a testing matrix nightmare.
- **No custom UI.** Telegram is the interface. Do not build a React dashboard, Electron app, or web frontend. A CLI companion (Phase 4a from the roadmap) is acceptable later, but not now.
- **No abstract base classes, protocols, or type hierarchies.** The codebase uses simple types (dicts, strings, lists) and compensates with 326+ tests. This is correct for a single-developer project. Do not add ABC, Protocol, Generic, or TypeVar abstractions.

### Do NOT weaken security

- **Do not remove or weaken the command blocklist** in `sandbox.py`. The 34-pattern blocklist prevents `rm -rf ~`, `sudo`, fork bombs, and other catastrophic commands. Keep it intact.
- **Do not remove code content scanning.** The `_scan_code_content()` function checks for dangerous patterns before subprocess execution. Keep it.
- **Do not allow Docker `--privileged` mode.** The Docker sandbox deliberately restricts capabilities.
- **Do not remove `_filter_env()`** or add credentials back to subprocess environments. The env filter strips ANTHROPIC_API_KEY and TELEGRAM_BOT_TOKEN from child processes.
- **Do not remove ALLOWED_USER_IDS checking.** This is the authentication layer. It must remain.

### Do NOT change working systems

- **Do not refactor the LangGraph state machine.** The 5-node graph (classify → plan → execute → audit → deliver) with conditional retry is correct and battle-tested. Do not add nodes, split nodes, or change the flow.
- **Do not change the cross-model audit architecture.** Sonnet executor + Opus auditor is the system's primary differentiator. Do not make them use the same model.
- **Do not change the 7 task types.** code, data, file, automation, ui_design, frontend, and project cover all current use cases. Do not add, remove, or merge types.
- **Do not change the retry logic in graph.py.** `should_retry()` checks `audit_verdict == "pass"` and `retry_count >= MAX_RETRIES`. This is correct. Do not add complexity.
- **Do not change how `projects.yaml` works.** The flat YAML list with name, path, commands, venv, timeout, and triggers is the right abstraction level. Do not add inheritance, templating, or conditional logic.

### Do NOT introduce new dependencies

- **Do not add a web framework** (FastAPI, Flask, etc.) unless explicitly requested for the CLI companion.
- **Do not add an ORM.** The raw `aiosqlite` queries are correct for this use case.
- **Do not replace APScheduler** with Celery, RQ, or any task queue. The single-process scheduler is correct for a single-user system.
- **Do not add type-checking tools** (mypy, pyright) to the CI pipeline. The test suite is the correctness mechanism.

### Formatting and style

- **Preserve the existing code style.** The codebase uses: 4-space indentation, double quotes for strings, type hints on function signatures, `from __future__ import annotations`, docstrings on public functions, `logger = logging.getLogger(__name__)` per module.
- **Do not add trailing commas** where they don't exist.
- **Do not reorganise imports** unless a file is being edited for another reason.
- **Do not add `# type: ignore` comments.** If a type issue exists, fix the code, don't suppress the warning.

---

## Verification Checklist

After all three phases are complete, run these checks:

```bash
# 1. Full test suite
python -m pytest tests/ -v
# Expected: 330+ passed (existing 326 + new claude_client tests), 10 skipped

# 2. Grep for any remaining "AgentCore" references in code (not docs)
grep -rn "AgentCore\|agentcore\|AGENTCORE" --include="*.py" --include="*.yaml" --include="*.sh" --include="Dockerfile" | grep -v __pycache__
# Expected: 0 results

# 3. Verify database filename
grep "agentsutra.db" config.py
# Expected: 1 match

# 4. Verify Docker image name
grep "agentsutra-sandbox" config.py tools/sandbox.py Dockerfile scripts/build_sandbox.sh
# Expected: matches in all 4 files

# 5. Verify log filename
grep "agentsutra.log" main.py
# Expected: 1 match

# 6. Verify the no-text-content fix
grep "no text content" tools/claude_client.py
# Expected: 2 matches — the raise AND the new retry catch

# 7. Start the bot and send /start
# Expected: "AgentSutra v7.0.0 is online."

# 8. Re-run Test 1.1 (prime numbers) to confirm stdin=DEVNULL works
# Expected: Execution succeeds, primes.txt delivered, ALL ASSERTIONS PASSED

# 9. Re-run Test 1.4 (PulseMetrics) to confirm no-text-content retry works
# Expected: Either first-attempt success or retry-then-success, NOT "Task failed"
```

---

## Summary of changes

| Phase | What | Files | Priority |
|---|---|---|---|
| 1 | Fix "no text content" retry | `tools/claude_client.py` + new tests | CRITICAL |
| 2 | Rename AgentCore → AgentSutra | 17+ source files, 3 scripts, Dockerfile, 3 docs, tests | CRITICAL |
| 3A | Artifact declaration in prompts | `brain/nodes/executor.py` | MEDIUM |
| 3B | Version string | `config.py`, `main.py`, `bot/handlers.py` | LOW |
| 3C | Venv healthcheck | `bot/handlers.py` | LOW |

Total estimated effort: 2–3 hours including testing.
