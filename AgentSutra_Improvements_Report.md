# AgentSutra — Improvements Report

*Comprehensive review of quality-of-life and architectural improvements, ordered by impact.*

---

## Part 1: Claude Code Workflow Improvements (Developer Experience)

These changes improve how Claude Code (and you) interact with AgentSutra during development sessions. None touch runtime code.

### 1.1 Create a `Justfile` — HIGH IMPACT, LOW EFFORT

**Problem:** Every test run, lint check, or deployment step requires remembering exact commands. Claude Code also has to rediscover them each session.

**Recommendation:** Create a `Justfile` at project root:

```just
# Development
test *ARGS:        python3 -m pytest tests/ -v {{ARGS}}
test-quick:        python3 -m pytest tests/ -v -k "not docker" -x
test-security:     python3 -m pytest tests/test_sandbox.py tests/test_stress_v8.py tests/test_stress_v8_audit2.py -v
lint:              ruff check .
format:            ruff format .
typecheck:         mypy --ignore-missing-imports brain/ tools/ storage/ bot/

# Operations
run:               python3 main.py
cost:              sqlite3 agentsutra.db "SELECT date(timestamp), SUM(input_tokens), SUM(output_tokens) FROM api_usage GROUP BY date(timestamp) ORDER BY date(timestamp) DESC LIMIT 7;"
health:            curl -s http://localhost:8443/health || echo "Bot not running"
backup:            ./scripts/secure_deploy.sh backup
```

**Why it matters:** Claude Code can run `just test` without reading CLAUDE.md every time. You get muscle-memory commands. ~30 minutes to create.

---

### 1.2 Add `.claude/commands/` That Actually Work — MEDIUM IMPACT

**Problem:** The 4 existing commands (`audit-node.md`, `fix-issue.md`, `implement.md`, `review-pr.md`) are prompt templates but don't enforce any workflow. Claude Code doesn't know about test requirements, security invariants, or the session log without re-reading CLAUDE.md.

**Recommendation:** Enhance each command with concrete steps:

- `/implement` should end with `just test-quick` and a session log entry
- `/audit-node` should run `just test-security` after any sandbox changes
- `/fix-issue` should reference the Known Issues table in CLAUDE.md and update it when done
- Add `/review-security` — runs the 51 code scanner patterns test + checks for new bypass vectors

**Why it matters:** Encodes institutional knowledge into reusable workflows instead of relying on CLAUDE.md being fully read.

---

### 1.3 Pre-Commit Hooks — MEDIUM IMPACT

**Problem:** No pre-commit config exists. Protected files (`.env`, `agentsutra.db`, `projects_macmini.yaml`) rely solely on `.gitignore` and Claude Code hooks in `settings.json`. A manual `git add -A` could leak secrets.

**Recommendation:** Add `.pre-commit-config.yaml`:

```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.4.0
    hooks:
      - id: ruff
      - id: ruff-format
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v4.5.0
    hooks:
      - id: check-added-large-files
        args: ['--maxkb=500']
      - id: detect-private-key
      - id: check-yaml
```

**Why it matters:** Defence in depth. The Claude Code hooks protect during sessions, pre-commit protects during manual git operations.

---

### 1.4 GitHub Actions CI — MEDIUM IMPACT, MEDIUM EFFORT

**Problem:** No automated testing on push. You already have CI on `igaming-intelligence-dashboard` and `sensispend-v2` — AgentSutra is the most complex project without it.

**Recommendation:** `.github/workflows/ci.yml` running:
- `pytest tests/ -v -k "not docker"` (625 tests, ~2 min)
- `ruff check .`
- Triggered on push to `main` and PRs

**Why it matters:** Catches regressions before they hit the Mac Mini. Especially important with 661 tests and security-critical code.

---

### 1.5 Smarter CLAUDE.md Session Log — LOW EFFORT REFINEMENT

**Problem:** The session log as currently designed will grow unbounded in a file that's already 230 lines and loaded into every Claude Code context window.

**Recommendation:**
- Cap the Session Log to the last 10 entries in CLAUDE.md
- Add a `.claude/commands/rotate-log.md` command that archives older entries to `docs/session_history.md`
- Or: move the session log to a separate `SESSION_LOG.md` file and reference it from CLAUDE.md

**Why it matters:** CLAUDE.md is loaded into context every session. Keeping it lean means more context budget for actual work.

---

## Part 2: Architectural Changes (Runtime Impact)

These change how AgentSutra behaves in production. Ordered by impact-to-effort ratio.

### 2.1 RAG Context Layer — CRITICAL IMPACT, HIGH EFFORT

**The single highest-impact change possible.**

**Problem:** The planner currently injects up to 50 files (capped by `MAX_FILE_INJECT_COUNT` in config.py) into the planning prompt. For projects with 100+ files, this means:
- Random sampling misses critical files
- No architectural understanding
- Context window wasted on irrelevant files
- Known limitation documented in CLAUDE.md: "No codebase understanding — samples 3-5 files, no architectural model"

**Current flow** (planner.py `_inject_project_files()`):
```
project dir → enumerate files → sort by relevance heuristic → inject first 50 → hope for the best
```

**Proposed flow:**
```
project dir → index with nomic-embed-text via Ollama → store in LanceDB
task message → embed query → retrieve top-k relevant chunks → inject into planner
```

**Implementation sketch:**
1. New module: `tools/rag.py` (~200 LOC)
   - `index_project(project_path)` — walks files, chunks, embeds via Ollama, stores in LanceDB
   - `query_context(project_name, query, top_k=10)` — retrieves relevant chunks
   - `should_use_rag(project_path)` — returns True if >100 files OR >8000 tokens in file set
2. Planner integration: Replace `_inject_project_files()` with `_inject_rag_context()` when `should_use_rag()` is True, fall back to current method for small projects
3. Index storage: `storage/rag_indexes/{project_name}/` — LanceDB tables
4. Re-indexing: On-demand via `/reindex` command or when file count changes >20%

**Dependencies:** `lancedb`, `ollama` (already available), `nomic-embed-text` model (small, runs on M2)

**Why it matters:** This is the difference between AgentSutra understanding a 200-file project vs. guessing from 5 random files. Every task on a large project benefits.

**Effort:** ~2 days. Already on roadmap as item #3.

---

### 2.2 Partial Result Preservation — HIGH IMPACT, MEDIUM EFFORT

**Problem:** If the pipeline fails at Execute or Audit, everything before that stage is lost. The plan, classified task type, and extracted params are in-memory only (`AgentState` TypedDict). The user gets a generic "task failed" message and must start over.

**Current state:** `storage/db.py` stores task status but NOT intermediate results. Only `final_response` is persisted on success.

**Recommendation:**
1. Add a `task_state` TEXT column to the `tasks` table (JSON-serialized `AgentState`)
2. After each pipeline node completes, persist the current state: `db.update_task_state(task_id, state)`
3. On failure, the `/status <task_id>` command shows what was completed (plan, generated code, etc.)
4. Future: Add `/retry <task_id>` that resumes from the last successful stage

**Schema change:**
```sql
ALTER TABLE tasks ADD COLUMN task_state TEXT DEFAULT '{}';
ALTER TABLE tasks ADD COLUMN last_completed_stage TEXT DEFAULT '';
```

**Why it matters:** Users lose significant context on failures. With 3 retry cycles, a task might generate 3 plans before the final one sticks — but if all 3 fail, the user sees nothing. This is especially painful for complex multi-step tasks.

**Effort:** ~4 hours. Touch points: `storage/db.py`, `brain/graph.py`, `bot/handlers.py` (status command).

---

### 2.3 Cost Analytics in `/cost` Command — HIGH IMPACT, LOW EFFORT

**Problem:** `tools/claude_client.py` tracks every API call's token usage via `_persist_usage()` (cost per call stored in DB), but the `/cost` command in `handlers.py` only shows a single daily total. Users have no visibility into:
- Which task types cost the most
- Opus audit cost vs. Sonnet generation cost
- Cost trends over time
- Whether Ollama offloading is actually saving money

**Recommendation:** Enhance `/cost` output:

```
💰 Cost Summary (Last 7 Days)
─────────────────────────────
Today:     $1.23  (12 tasks)
Yesterday: $0.87  (8 tasks)
This week: $5.41  (47 tasks)
This month: $18.92

📊 Breakdown (Today)
Opus (audit):   $0.89 (72%)
Sonnet (gen):   $0.31 (25%)
Ollama:         $0.00 (3 tasks offloaded)

🔥 Costliest Task Types
code:     $0.65/avg
project:  $0.43/avg
data:     $0.12/avg
```

**Implementation:** Add SQL aggregation queries to `storage/db.py`, format in `/cost` handler. The data already exists — it's just not surfaced.

**Effort:** ~2 hours. Pure SQL + formatting.

---

### 2.4 `/setup` Onboarding Command — MEDIUM IMPACT, MEDIUM EFFORT

**Problem:** New installations require manual `.env` creation, project YAML editing, Ollama verification, and budget configuration. No validation happens until the first task fails. Already on the roadmap as item #5.

**Recommendation:** Interactive `/setup` command that:
1. Checks `.env` for required keys (ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, ALLOWED_USER_IDS)
2. Validates Ollama connectivity and model availability
3. Validates registered projects (paths exist, commands resolve)
4. Runs a smoke test (classify a dummy task)
5. Reports budget configuration and daily/monthly limits
6. Outputs a health report

**Why it matters:** Reduces time-to-first-task from ~30 minutes to ~5 minutes. Catches misconfigurations before they cause cryptic failures.

**Effort:** ~4 hours. New handler + validation functions.

---

### 2.5 Temporal Window Expansion (2-Hour) — MEDIUM IMPACT, LOW EFFORT

**Problem:** The deliverer's `_mine_temporal_sequences()` uses a 30-minute window to detect patterns. CLAUDE.md documents that a 2-hour window would capture ~40% more patterns.

**Recommendation:** Change the temporal window constant from 30 minutes to 2 hours. This is likely a single constant change in `brain/nodes/deliverer.py`.

**Risk:** Minimal. Wider window = more patterns found = richer project memory. The FIFO cap (50 rows) prevents unbounded growth.

**Effort:** ~15 minutes. One constant change + test verification.

---

### 2.6 Structured Stage Timing Exposure — MEDIUM IMPACT, LOW EFFORT

**Problem:** `brain/graph.py` collects `stage_timings` (a dict of stage → duration) for every task, but this data is never shown to users or logged in aggregate. It goes into `AgentState` and then disappears.

**Recommendation:**
1. Include stage timings in the `/debug` sidecar output (already exists in deliverer)
2. Persist timings in the `tasks` table (add `stage_timings` TEXT column)
3. Add to `/health` output: average pipeline duration, slowest stage, p95 latency

**Why it matters:** Identifies bottlenecks. If audit consistently takes 8s while classify takes 0.5s, you know where to optimize. If execution suddenly spikes to 60s, something changed.

**Effort:** ~2 hours.

---

### 2.7 Graceful Budget Degradation — MEDIUM IMPACT, MEDIUM EFFORT

**Problem:** When daily budget is exceeded, `claude_client.py` raises a hard error and the task fails completely. No fallback.

**Recommendation:** Tiered degradation:
1. **Normal mode** — Use Sonnet for generation, Opus for audit (current)
2. **Budget-conscious mode** (>80% daily budget) — Offload classify + plan to Ollama, keep Opus audit
3. **Budget-exceeded mode** (>100%) — Ollama-only for all stages except audit, which is skipped with a warning

This respects the invariant "Opus ALWAYS audits" in normal operation but allows degraded-but-functional mode when budget is exhausted. User gets a clear warning: "⚠️ Budget exceeded — running in degraded mode (no Opus audit)."

**Effort:** ~4 hours. Touch points: `tools/model_router.py`, `tools/claude_client.py`, `brain/graph.py`.

---

### 2.8 Launchd Service for Mac Mini — MEDIUM IMPACT, LOW EFFORT

**Problem:** AgentSutra runs ad-hoc on the Mac Mini. If the machine restarts or the process crashes, someone must manually restart it. No process supervision.

**Recommendation:** Create `scripts/com.agentsutra.bot.plist` for launchd:
- Auto-start on boot
- Auto-restart on crash (with 30s backoff)
- Log stdout/stderr to `~/Library/Logs/agentsutra/`
- `KeepAlive: true`

**Why it matters:** Production reliability. The Mac Mini runs 24/7 — the bot should too.

**Effort:** ~1 hour. Single plist file + install script.

---

### 2.9 `/retry` Command — LOW-MEDIUM IMPACT, LOW EFFORT

**Problem:** When a task fails, the user must re-type or re-send the entire message. No way to retry with the same input.

**Recommendation:** `/retry [task_id]` that:
- Defaults to last failed task if no ID given
- Re-runs the pipeline with the same message and files
- Optionally accepts `/retry <task_id> --from plan` to skip classify and resume from a specific stage (requires 2.2 partial result preservation)

**Effort:** ~2 hours without stage resume, ~4 hours with it.

---

### 2.10 Health Check Endpoint — LOW IMPACT (BUT ENABLES MONITORING)

**Problem:** No way to externally verify the bot is alive and healthy. The Mac Mini could be running a zombie process.

**Recommendation:** Lightweight HTTP health endpoint (separate from Telegram webhook):
- `/health` returns JSON: `{"status": "ok", "uptime": "4d 12h", "tasks_today": 12, "budget_remaining": "$3.21"}`
- Run on a non-Telegram port (e.g., 9090)
- Can be monitored by uptime services, cron-based alerts, or a simple `curl` check

**Effort:** ~2 hours. Small Flask/aiohttp server alongside the Telegram bot.

---

## Part 3: What NOT to Do

Based on the CLAUDE.md invariants and project philosophy, these are tempting but wrong:

| Tempting Idea | Why It's Wrong |
|---------------|---------------|
| Web dashboard for monitoring | Violates "no speculative abstractions" and "no web UIs just in case" |
| Plugin/extension system | Violates invariant #5. AgentSutra is a single-user tool, not a platform |
| Dynamic pipeline stages | Violates invariant #1. The 5-stage pipeline is fixed by design |
| Replace SQLite with Postgres | Over-engineering for single-user. SQLite WAL handles the concurrency |
| Abstract model provider layer | Violates invariant #5. Only two providers exist (Claude + Ollama) |
| Microservice decomposition | Single process is simpler, more reliable, easier to debug |

---

## Priority Matrix

| # | Change | Impact | Effort | Category |
|---|--------|--------|--------|----------|
| 2.1 | RAG context layer | 🔴 Critical | 2 days | Architecture |
| 2.2 | Partial result preservation | 🟠 High | 4 hours | Architecture |
| 2.3 | Cost analytics in `/cost` | 🟠 High | 2 hours | Architecture |
| 1.1 | Justfile | 🟠 High | 30 min | DX |
| 2.5 | Temporal window expansion | 🟡 Medium | 15 min | Architecture |
| 2.8 | Launchd service | 🟡 Medium | 1 hour | Operations |
| 1.3 | Pre-commit hooks | 🟡 Medium | 1 hour | DX |
| 1.4 | GitHub Actions CI | 🟡 Medium | 2 hours | DX |
| 2.4 | `/setup` command | 🟡 Medium | 4 hours | Architecture |
| 2.6 | Stage timing exposure | 🟡 Medium | 2 hours | Architecture |
| 2.9 | `/retry` command | 🟢 Low-Med | 2 hours | Architecture |
| 1.2 | Enhanced Claude commands | 🟢 Low-Med | 2 hours | DX |
| 2.7 | Budget degradation | 🟡 Medium | 4 hours | Architecture |
| 2.10 | Health endpoint | 🟢 Low | 2 hours | Operations |
| 1.5 | Session log rotation | 🟢 Low | 30 min | DX |

**Recommended order:** 1.1 → 2.5 → 2.3 → 2.8 → 2.2 → 1.3 → 1.4 → 2.1 (RAG last because it's the biggest, but the most impactful)
