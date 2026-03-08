# AgentSutra Improvements — Implementation Plan

Based on thorough review of `AgentSutra_Improvements_Report.md` and codebase analysis.
Ordered by execution sequence: quick wins first, then increasing complexity.
Each item includes the exact touch points, implementation approach, gotchas, and what NOT to do.

---

## Phase 1: Quick Wins (< 1 hour each)

### 1A. Temporal Window Expansion (15 min)

**File:** `brain/nodes/deliverer.py:246`

**Current:** `julianday(t2.created_at) - julianday(t1.completed_at) < 0.0208` (30 min)

**Change to:** `< 0.0833` (2 hours = 2/24 = 0.0833 days)

**Why this value:** CLAUDE.md documents "2-hour window captures ~40% more patterns." The FIFO cap on `project_memory` (50 rows/project, M-1 fix) prevents unbounded growth, so widening the window is safe.

**Verification:** `pytest tests/ -v -k "temporal or suggest_next"` — check no tests assert the old 0.0208 value.

**What NOT to do:**
- Don't make this configurable via config.py or env var. It's a heuristic, not user-facing. One constant in one place.
- Don't change the query structure — just the threshold value.

---

### 1B. Justfile (30 min)

**File:** New file `Justfile` at project root.

```just
# AgentSutra development commands

# Run all tests (625 pass without Docker)
test *ARGS:
    python3 -m pytest tests/ -v {{ARGS}}

# Quick tests — skip Docker, stop on first failure
test-quick:
    python3 -m pytest tests/ -v -k "not docker" -x

# Security-critical tests only
test-security:
    python3 -m pytest tests/test_sandbox.py tests/test_stress_v8.py tests/test_stress_v8_audit2.py tests/test_v8_remediation.py -v

# Lint with ruff
lint:
    ruff check .

# Format with ruff
format:
    ruff format .

# Run the bot
run:
    python3 main.py

# Show recent cost data from API usage DB
cost:
    @python3 -c "from tools.claude_client import get_cost_summary; import json; print(json.dumps(get_cost_summary(), indent=2))"
```

**Why this approach:**
- `cost` recipe uses the existing `get_cost_summary()` function rather than raw SQL, so it stays consistent with bot output.
- No `typecheck` recipe — mypy isn't configured for this project and adding it is out of scope.
- No `health` or `backup` recipes — no HTTP health endpoint exists yet, no backup script exists.

**What NOT to do:**
- Don't add recipes for things that don't exist yet (health endpoint, deploy scripts).
- Don't add a `deploy` recipe — deployment is triggered via Telegram `/deploy`, not CLI.
- Don't duplicate the test commands differently from what CLAUDE.md documents.

---

### 1C. Session Log Rotation (30 min)

**Problem:** CLAUDE.md session log grows unbounded inside a file loaded into every context window.

**Approach:** Move session log to a separate file.

**Changes:**
1. Create `SESSION_LOG.md` — move any existing session log entries from CLAUDE.md into it.
2. In CLAUDE.md, replace the "Session Log" section with:
   ```
   ## Session Log
   Session log lives in `SESSION_LOG.md`. Append entries there, not here.
   ```
3. Update the "Session Log Instructions" to reference `SESSION_LOG.md`.

**Why this over "cap at 10 entries":** Caps require manual rotation commands and discipline. A separate file is zero-maintenance — CLAUDE.md stays lean, full history is preserved, and Claude Code can read `SESSION_LOG.md` on demand.

**What NOT to do:**
- Don't create a rotation command — overengineering for a file that grows by ~5 lines per session.
- Don't delete old entries — they have value for continuity across sessions.

---

## Phase 2: Developer Infrastructure (1-2 hours each)

### 2A. Pre-Commit Hooks (1 hour)

**File:** New `.pre-commit-config.yaml` at project root.

```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.9.7
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v5.0.0
    hooks:
      - id: check-added-large-files
        args: ['--maxkb=500']
      - id: detect-private-key
      - id: check-yaml
      - id: end-of-file-fixer
      - id: trailing-whitespace
```

**Installation:** `pip install pre-commit && pre-commit install` (in project venv).

**Pin versions to latest stable** at time of creation, not the outdated versions in the report (v0.4.0, v4.5.0).

**What NOT to do:**
- Don't add `mypy` as a pre-commit hook — it's not configured for this project and will fail.
- Don't add `detect-aws-credentials` — no AWS in use, false positives from .env parsing.
- Don't install pre-commit globally — use the project venv per global CLAUDE.md rules.

---

### 2B. GitHub Actions CI (1-2 hours)

**File:** `.github/workflows/ci.yml`

```yaml
name: CI
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
          cache: 'pip'
      - run: pip install -r requirements.txt
      - run: pip install pytest ruff
      - name: Lint
        run: ruff check .
      - name: Test
        run: python -m pytest tests/ -v -k "not docker" --tb=short
```

**Key decisions:**
- Python 3.11 to match dev/prod environment (pyenv 3.11.9).
- `-k "not docker"` skips Docker-dependent tests (36 skipped).
- `--tb=short` for readable CI output without flooding logs.
- `cache: 'pip'` speeds up repeated runs.
- Single job, no matrix — one Python version, one OS. AgentSutra runs on one Mac Mini.

**What NOT to do:**
- Don't add matrix builds (3.11/3.12, ubuntu/macos) — single-target project, wasted CI minutes.
- Don't add coverage reporting — no coverage baseline exists, adding it now creates noise.
- Don't add deployment steps — deployment is Telegram-triggered, not CI-triggered.
- Don't add secrets for ANTHROPIC_API_KEY — tests should mock API calls, not make real ones. If any test requires a real key, mark it with `@pytest.mark.skipif` and handle in CI.

---

### 2C. Enhanced Claude Commands (1-2 hours)

Enhance the 4 existing `.claude/commands/` files. Don't create new command files — enrich existing ones.

**`implement.md`** — Add at the end:
- Step: Run `just test-quick` after implementation
- Step: Append session log entry to `SESSION_LOG.md`

**`audit-node.md`** — Add:
- Step: Run `just test-security` after any sandbox/security changes
- Step: Cross-reference against the Known Issues table in CLAUDE.md

**`fix-issue.md`** — Add:
- Step: Update the Known Issues table status when issue is fixed
- Step: Run relevant test file

**`review-pr.md`** — Add:
- Step: Check for OWASP top-10 patterns in changed files
- Step: Verify no `.env`, `.db`, or credential files in staged changes

**What NOT to do:**
- Don't create a `/review-security` command — `just test-security` already covers this.
- Don't make commands call `just` directly (commands are prompt templates, not scripts).
- Don't rewrite the commands from scratch — append workflow steps to existing content.

---

## Phase 3: High-Impact Runtime Changes (2-4 hours each)

### 3A. Cost Analytics Enhancement (2 hours)

**Touch points:** `tools/claude_client.py`, `bot/handlers.py:358-376`, `storage/db.py`

**Current state:** `/cost` shows lifetime totals and per-model breakdown. No daily/weekly view, no per-task-type breakdown, no Ollama visibility.

**Implementation:**

1. **New function in `claude_client.py`:** `get_daily_cost_summary(days=7)`
   - Query `api_usage` grouped by `date(timestamp, 'unixepoch')` and `model`
   - Return list of `{date, model, calls, input_tokens, output_tokens, cost_usd}`
   - Use existing `MODEL_COSTS` dict for pricing

2. **New function in `db.py`:** `get_task_type_costs(days=7)`
   - JOIN `tasks` with `api_usage` on timestamp range (task created_at to completed_at)
   - Problem: No direct FK between tasks and api_usage. **Workaround:** Aggregate by task_type from tasks table for task counts, keep cost breakdown model-only.
   - Simpler approach: Just count tasks by type from `tasks` table, keep cost breakdown by model from `api_usage`. Don't try to attribute cost to individual tasks — the data model doesn't support it without adding a `task_id` column to `api_usage`.

3. **Update `/cost` handler in `handlers.py`:**
   ```
   Cost Summary (Last 7 Days)
   Today:     $1.23  (12 tasks)
   Yesterday: $0.87  (8 tasks)
   This week: $5.41

   By Model (Today)
   Opus (audit):   $0.89 (72%)
   Sonnet (gen):   $0.31 (25%)

   Budget: $3.21 remaining (daily) / $18.08 remaining (monthly)
   ```

**What NOT to do:**
- Don't add a `task_id` FK to `api_usage` — that's a schema migration affecting `_persist_usage()`, `_check_budget()`, and every test that touches cost. Too much blast radius for a display improvement.
- Don't show "Ollama: $0.00 (N tasks offloaded)" — there's no Ollama usage tracking in the DB. Adding it is a separate feature.
- Don't add charts, graphs, or export functionality. Text output in Telegram is sufficient.

---

### 3B. Partial Result Preservation (4 hours)

**Touch points:** `storage/db.py`, `brain/graph.py`, `bot/handlers.py`

**The most architecturally impactful change in this plan.** This is the foundation for `/retry` (3E) and better failure diagnostics.

**Implementation:**

1. **Schema migration in `db.py`:**
   Add two columns to `tasks` table creation (lines 15-28):
   ```python
   task_state TEXT DEFAULT '{}',
   last_completed_stage TEXT DEFAULT ''
   ```
   For existing DBs, add migration logic in `init_db()` — use `PRAGMA table_info(tasks)` to check if columns exist, `ALTER TABLE` if not. This is the same pattern already used in AgentSutra (see db.py for precedent).

2. **New function in `db.py`:** `update_task_state(task_id, state_dict, stage_name)`
   - Serializes relevant AgentState fields to JSON (exclude large blobs like full file contents)
   - Fields to persist: `task_type`, `project_name`, `plan`, `code` (first 5000 chars), `execution_result` (first 5000 chars), `audit_verdict`, `audit_feedback`, `stage_timings`, `extracted_params`, `deploy_url`, `server_url`
   - Updates `task_state` and `last_completed_stage`
   - Uses sync DB (pipeline runs in `asyncio.to_thread`, invariant #3)

3. **Hook into `graph.py` `_wrap_node()`** (line 44-55):
   After recording stage timing, call `update_task_state(state["task_id"], state, stage_name)`.
   Wrap in try/except — state persistence must never crash the pipeline (invariant #4).

4. **Enhance `/status` in `handlers.py`:**
   When showing a failed/completed task, include `last_completed_stage` and key fields from `task_state` (plan summary, audit verdict).

**Serialization strategy:**
- Use `json.dumps()` with a default handler for non-serializable types (Path objects, etc.)
- Truncate large text fields (code, execution_result) to 5000 chars to prevent DB bloat
- Don't serialize `files` (binary content) or `conversation_context` (redundant, stored separately)

**What NOT to do:**
- Don't store the entire AgentState verbatim — it contains file contents, full conversation context, and other large objects that would bloat the DB.
- Don't add `/retry` in this step — that's 3E. This step is purely about persisting and displaying state.
- Don't use a separate table — a JSON column on `tasks` is simpler and keeps the task lifecycle in one row.
- Don't use `aiosqlite` — the pipeline is sync-in-thread by design (invariant #3).

---

### 3C. Stage Timing Exposure (2 hours)

**Touch points:** `storage/db.py`, `bot/handlers.py`, `brain/nodes/deliverer.py`

**Current state:** Stage timings are collected in `graph.py:_wrap_node()` and written to debug sidecar JSON files (`deliverer.py:196-226`). Never shown to users or persisted in DB.

**Implementation:**

1. **Add `stage_timings` TEXT column to `tasks` table** (same migration pattern as 3B).
   Persist as JSON array: `[{"name": "classify", "duration_ms": 450}, ...]`

2. **Persist timings in `_wrap_node()` or at pipeline end:**
   Better approach: persist once at deliver stage (all timings collected by then) rather than after every node. Add to the `update_task_state()` call from 3B — `stage_timings` is already in the state dict.

   If 3B is implemented first, this comes free — `stage_timings` is already part of the persisted state. Just add a dedicated column for fast querying.

3. **Add to `/health` handler:**
   ```
   Pipeline Performance (Last 24h)
   Avg total:   12.3s (N tasks)
   Classify:    0.4s avg
   Plan:        2.1s avg
   Execute:     6.8s avg
   Audit:       2.5s avg
   Deliver:     0.5s avg
   ```
   Query: Parse JSON `stage_timings` from completed tasks in last 24h, compute averages.

4. **Add to `/status <task_id>` for completed tasks:**
   Show per-stage durations inline.

**What NOT to do:**
- Don't add p95/p99 latency calculations — over-engineering for a single-user system with ~10-50 tasks/day.
- Don't create a separate `stage_timings` table — JSON column is fine for display purposes. You're not querying individual stages at scale.
- Don't persist timings after every node — one write at the end of the pipeline is sufficient and avoids 5x DB writes per task.

**Dependency:** Pairs naturally with 3B. If implementing both, share the migration and the `update_task_state()` function.

---

### 3D. Launchd Service (1 hour)

**File:** New `scripts/com.agentsutra.bot.plist`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.agentsutra.bot</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/agentruntime1/.pyenv/versions/3.11.9/bin/python3</string>
        <string>/Users/agentruntime1/Desktop/AgentSutra/main.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/agentruntime1/Desktop/AgentSutra</string>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>StandardOutPath</key>
    <string>/Users/agentruntime1/Library/Logs/agentsutra/stdout.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/agentruntime1/Library/Logs/agentsutra/stderr.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/Users/agentruntime1/.pyenv/versions/3.11.9/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
```

**Install script** (`scripts/install_service.sh`):
```bash
#!/bin/bash
mkdir -p ~/Library/Logs/agentsutra
cp scripts/com.agentsutra.bot.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.agentsutra.bot.plist
echo "AgentSutra service installed. Check: launchctl list | grep agentsutra"
```

**Key decisions:**
- `ThrottleInterval: 30` — 30s backoff between crash restarts (prevents tight restart loops).
- `KeepAlive: true` — restarts on crash, not just on boot.
- Uses absolute pyenv Python path — launchd doesn't inherit shell PATH or pyenv shims.
- Logs to `~/Library/Logs/agentsutra/` — standard macOS location, viewable via Console.app.
- No `RunAtLoad` needed — `KeepAlive` implies it.

**What NOT to do:**
- Don't use `launchctl bootstrap` (modern syntax) — `load`/`unload` is simpler and works on all macOS versions on M2.
- Don't set `LowPriorityIO` or `Nice` — the bot needs responsive I/O for Telegram.
- Don't hardcode `.env` values in the plist — `main.py` already loads `.env` via `python-dotenv`.
- Don't add log rotation in the plist — use a separate newsyslog config or just `> stdout.log` periodically. Logs are small (text only).
- Don't use paths for dev machine (confusemouse) — this is production Mac Mini (agentruntime1). Template the paths.

---

### 3E. /retry Command (2-4 hours)

**Depends on:** 3B (Partial Result Preservation)

**Touch points:** `bot/handlers.py`

**Implementation:**

1. **New handler** `/retry [task_id]`:
   - If no task_id: find the user's most recent failed task from `tasks` table
   - Validate task exists and belongs to the user
   - Retrieve original `message` and `files` from the task row (message is already stored)
   - Re-submit to pipeline via the same path as normal message handling

2. **Phase 1 (simple replay):**
   - Just re-runs the full pipeline with the original message. No stage resume.
   - This is useful on its own — saves the user from re-typing/re-sending.

3. **Phase 2 (stage resume — future, only if 3B is solid):**
   - Accept `--from <stage>` flag: `/retry abc123 --from execute`
   - Load `task_state` from DB, reconstruct partial AgentState
   - Skip stages before the target stage
   - Requires `build_graph()` to accept a start_stage parameter — more invasive.
   - **Recommendation:** Don't implement Phase 2 initially. Phase 1 covers 90% of the value.

**What NOT to do:**
- Don't implement stage resume in the first pass — the graph wiring changes are complex and error-prone.
- Don't allow retrying completed tasks — only failed/crashed tasks. Retrying success is confusing.
- Don't re-use the old task_id — create a new task with a reference to the original (`retry_of` field or just log it).
- Don't bypass the concurrent task limit check — retries count toward `MAX_CONCURRENT_TASKS`.

---

### 3F. /setup Onboarding Command (4 hours)

**Touch points:** `bot/handlers.py`

**Implementation:** New `/setup` handler that runs validation checks and reports results.

```python
async def cmd_setup(update, context):
    checks = []

    # 1. Environment variables
    for key in ["ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "ALLOWED_USER_IDS"]:
        checks.append(("env:" + key, bool(os.getenv(key))))

    # 2. Ollama connectivity
    try:
        resp = httpx.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=5)
        models = [m["name"] for m in resp.json().get("models", [])]
        has_model = any(OLLAMA_DEFAULT_MODEL in m for m in models)
        checks.append(("ollama:connected", True))
        checks.append(("ollama:" + OLLAMA_DEFAULT_MODEL, has_model))
    except Exception:
        checks.append(("ollama:connected", False))

    # 3. Project registry
    from tools.projects import load_projects
    projects = load_projects()
    for p in projects:
        path_ok = Path(p["path"]).is_dir()
        checks.append((f"project:{p['name']}", path_ok))

    # 4. Budget config
    checks.append(("budget:daily", f"${DAILY_BUDGET_USD}" if DAILY_BUDGET_USD else "unlimited"))
    checks.append(("budget:monthly", f"${MONTHLY_BUDGET_USD}" if MONTHLY_BUDGET_USD else "unlimited"))

    # 5. DB writable
    try:
        init_db()
        checks.append(("db:writable", True))
    except Exception:
        checks.append(("db:writable", False))

    # Format and send
    # ... format as pass/fail checklist
```

**What NOT to do:**
- Don't run a smoke test that calls Claude API — that costs money and might fail on budget limits.
- Don't modify `.env` or project YAML — `/setup` is read-only diagnostics, not a wizard.
- Don't make this a required first-run step — existing users shouldn't be blocked by it.

---

## Phase 4: Major Feature — RAG Context Layer (2 days)

### 4A. RAG Implementation

**The single highest-impact change. Do this last because it's the largest and benefits from all prior infrastructure being stable.**

**New file:** `tools/rag.py` (~200-250 LOC)

**Dependencies:** `lancedb`, `ollama` (already installed)
- Add `lancedb` to `requirements.txt`
- Model: `nomic-embed-text` via Ollama (384-dim embeddings, fast on M2)

**Architecture:**

```
tools/rag.py
  index_project(project_path: Path, project_name: str) -> int  # returns chunk count
  query_context(project_name: str, query: str, top_k: int = 10) -> list[dict]
  should_use_rag(project_path: Path) -> bool
  _chunk_file(file_path: Path) -> list[dict]  # splits into ~500-token chunks
  _embed(texts: list[str]) -> list[list[float]]  # batch embed via Ollama

storage/rag_indexes/{project_name}/  # LanceDB tables, gitignored
```

**Chunking strategy:**
- Split by function/class boundaries for Python files (use `ast` module for reliable splitting)
- Fall back to sliding window (500 tokens, 100 overlap) for non-Python files
- Store metadata per chunk: `file_path`, `start_line`, `end_line`, `chunk_type` (function/class/block)
- Skip binary files, node_modules, __pycache__, venv, .git (reuse `_INJECT_EXCLUDE_DIRS` from planner)

**Integration into planner.py:**

```python
# In plan() function, replace _inject_project_files() call:

if project_config and project_config.get("path"):
    project_path = Path(project_config["path"])
    if should_use_rag(project_path):
        context_chunks = query_context(project_name, state["message"], top_k=10)
        file_context = _format_rag_chunks(context_chunks)
    else:
        file_context = _inject_project_files(state, project_path)  # existing method
```

**Indexing triggers:**
- On-demand: New `/reindex [project]` Telegram command
- Auto: In `plan()`, if index is stale (>24h old or file count changed >20%), re-index before querying
- Staleness check: Store `indexed_at` timestamp and `file_count` in a metadata table within LanceDB

**Graceful degradation (invariant #4):**
- If Ollama is down: fall back to `_inject_project_files()`, log warning
- If LanceDB index is corrupted: delete and rebuild, fall back to file injection for current task
- If embedding fails for a chunk: skip it, log warning, continue
- If `lancedb` not installed: fall back to file injection, log warning once

**What NOT to do:**
- Don't replace file injection entirely — keep it as fallback for small projects (<100 files) where full-file context is more useful than chunks.
- Don't embed with Claude API — that's expensive per-task. Ollama embeddings are free and fast locally.
- Don't use LangChain's document loaders or text splitters — direct `ast` module + simple splitting is more predictable and has zero extra dependencies.
- Don't chunk at fixed character boundaries — function-level chunks preserve semantic meaning. Use `ast.parse()` for Python, paragraph boundaries for markdown, line-based for other text.
- Don't store embeddings in SQLite — LanceDB is purpose-built for vector search and handles this efficiently.
- Don't index on every task — check staleness first. Re-indexing a 200-file project takes 30-60 seconds.
- Don't make `top_k` user-configurable — 10 chunks is the right default. The planner prompt has ~100K context; 10 chunks at ~500 tokens each uses ~5K tokens, leaving plenty of room.

---

## Phase 5: Budget Degradation (4 hours) — CAUTION

### 5A. Graceful Budget Degradation

**This is the most architecturally sensitive change. It touches an invariant.**

**The invariant problem:** "Opus ALWAYS audits" (invariant #2). The report suggests skipping Opus audit when budget is exceeded. This directly violates the invariant.

**My recommendation: Implement tiers 1 and 2 only. Do NOT implement tier 3.**

| Tier | Trigger | Behavior | Invariant Impact |
|------|---------|----------|-----------------|
| Normal | <80% daily budget | Current behavior | None |
| Budget-conscious | 80-100% daily budget | Offload classify + plan to Ollama, keep Opus audit | None |
| Budget-exceeded | >100% daily budget | **SKIP** | **Violates invariant #2** |

**Implementation (tiers 1-2 only):**

1. **In `tools/claude_client.py`:** Add `get_budget_utilization() -> float` that returns 0.0-1.0 ratio of today's spend vs daily limit. Returns 0.0 if no limit set.

2. **In `tools/model_router.py`:** Check `get_budget_utilization()`. If >0.8, force `complexity="low"` for classify and plan stages (routes to Ollama). Leave execute and audit unchanged.

3. **In `bot/handlers.py`:** When starting a task with >80% utilization, prepend a warning: "Budget >80% — using local model for classify/plan."

4. **When budget is fully exceeded:** Keep the current hard error. The user chose a budget for a reason. Silently degrading past the limit undermines the budget's purpose.

**What NOT to do:**
- Don't skip Opus audit under any circumstance — it's the primary safety gate.
- Don't silently degrade — always warn the user when switching to budget-conscious mode.
- Don't make the 80% threshold configurable — it's an internal heuristic.
- Don't add an "override budget" command — that defeats the purpose of budget limits.

---

## Phase 6: Health Endpoint (2 hours) — OPTIONAL

### 6A. Lightweight HTTP Health Check

**Only implement if launchd service (3D) is deployed and external monitoring is needed.**

**Approach:** Use `aiohttp` (already a dependency via `python-telegram-bot`) to add a tiny web server alongside the bot.

**In `main.py`:**
```python
from aiohttp import web

async def health_handler(request):
    return web.json_response({
        "status": "ok",
        "uptime_seconds": int(time.time() - _start_time),
        "tasks_today": get_task_count_today(),
        "budget_remaining": get_budget_remaining(),
    })

# Start alongside bot in main()
app = web.Application()
app.router.add_get("/health", health_handler)
runner = web.AppRunner(app)
# ... start on port 9090
```

**What NOT to do:**
- Don't use Flask — aiohttp is already available and fits the async architecture.
- Don't expose on 0.0.0.0 — bind to 127.0.0.1 only. This is a local health check, not a public API.
- Don't add authentication — localhost-only, single-user system.
- Don't add metrics endpoints (Prometheus, StatsD) — over-engineering.

---

## Global "What NOT to Do" Reminders

| Anti-pattern | Why |
|---|---|
| Web dashboard | Violates invariant #5 (no speculative abstractions). Telegram IS the UI. |
| Plugin/extension system | Single-user tool, not a platform. |
| Dynamic pipeline stages | Violates invariant #1. 5 stages are fixed. |
| Replace SQLite with Postgres | Over-engineering. SQLite WAL handles single-user concurrency fine. |
| Abstract model provider layer | Only 2 providers (Claude + Ollama). A provider abstraction adds complexity for no gain. |
| LangChain for RAG | Unnecessary dependency for embedding + vector search. Direct `ollama` + `lancedb` is simpler. |
| Coverage thresholds in CI | No baseline exists. Adding it now blocks all PRs on arbitrary numbers. |
| Type-checking (mypy) in CI | Not configured for this project. Adding it means fixing hundreds of type errors first. |
| Task queue (Celery/RQ) | `asyncio.to_thread` + semaphore handles 3 concurrent tasks fine. |

---

## Execution Order Summary

```
Phase 1 (Day 1 morning — 1.5 hours total):
  1A. Temporal window:  15 min  (one line change)
  1B. Justfile:         30 min  (new file)
  1C. Session log:      30 min  (move section to new file)

Phase 2 (Day 1 afternoon — 4 hours total):
  2A. Pre-commit:       1 hour  (new file + install)
  2B. GitHub Actions:   1 hour  (new file)
  2C. Commands:         1 hour  (enhance 4 existing files)

Phase 3 (Day 2-3 — 15 hours total):
  3A. Cost analytics:   2 hours
  3B. Partial results:  4 hours  ** do before 3C and 3E **
  3C. Stage timings:    2 hours  (builds on 3B)
  3D. Launchd:          1 hour
  3E. /retry:           2 hours  (depends on 3B)
  3F. /setup:           4 hours

Phase 4 (Day 4-5 — 2 days):
  4A. RAG context:      2 days   ** largest single change, highest impact **

Phase 5 (Day 5 — 4 hours):
  5A. Budget degrade:   4 hours  (tiers 1-2 only, skip tier 3)

Phase 6 (Optional):
  6A. Health endpoint:  2 hours  (only if 3D is deployed)
```

**Total estimated effort:** ~5 working days for everything, or ~2 days for Phases 1-3 (which deliver 80% of the value).
