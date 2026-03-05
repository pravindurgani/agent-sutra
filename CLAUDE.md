# AgentSutra v8.0.0 — Project Context for Claude Code

Single-user, self-hosted AI agent. Telegram-controlled. Mac Mini M2 (16GB).
Fixed 5-stage LangGraph pipeline: Classify → Plan → Execute → Audit → Deliver.
Cross-model adversarial auditing: Sonnet generates, Opus reviews.
~5,000 LOC across 16 source files. ~7,000 LOC tests across 18 files. 548 tests (36 skip for Docker).

## Architecture

```
[Telegram] → bot/handlers.py → brain/graph.py (LangGraph StateGraph)
                                    ↓
              classify → plan → execute → audit → deliver
                                  ↑         |
                                  +- retry --+ (max 3)
```

### File Map
| File | Lines | Purpose |
|------|------:|---------|
| `main.py` | 142 | Entry point: env validation, DB init, crash recovery, bot start |
| `config.py` | 95 | All constants, paths, model names, timeouts, budget caps |
| `brain/state.py` | 52 | `AgentState` TypedDict — 21 fields flowing through pipeline |
| `brain/graph.py` | 134 | LangGraph wiring, `run_task()`, stage tracking, node timing |
| `brain/nodes/classifier.py` | 90 | Fast path (trigger match) → slow path (Claude/Ollama classify) |
| `brain/nodes/planner.py` | 359 | Task-type prompts, standards/memory/file injection, 7 templates |
| `brain/nodes/executor.py` | 604 | Code gen + sandbox execution, project commands, auto-install |
| `brain/nodes/auditor.py` | 274 | Opus adversarial review, env error short-circuit, JSON parsing |
| `brain/nodes/deliverer.py` | 327 | Response formatting, memory extraction, temporal mining, debug sidecar |
| `tools/sandbox.py` | 1034 | Execution sandbox: blocklist, code scanner, Docker, live streaming |
| `tools/model_router.py` | 160 | Claude/Ollama routing by purpose, complexity, RAM, budget |
| `tools/claude_client.py` | 332 | Anthropic API wrapper: retries, cost tracking, streaming, budget |
| `tools/file_manager.py` | 154 | Upload handling, metadata extraction, content reading |
| `tools/projects.py` | 100 | Project registry loader, trigger matcher |
| `storage/db.py` | 369 | SQLite ops: async (bot) + sync (pipeline), 4 tables, WAL mode |
| `scheduler/cron.py` | 66 | APScheduler with SQLite persistence |
| `bot/telegram_bot.py` | 57 | Bot factory, command registration |

## Pipeline Flow

| Stage | Model | Router? | Key Function | Output |
|-------|-------|:---:|--------------|--------|
| Classify | Sonnet or Ollama | Yes (`complexity="low"`) | `classify()` | `task_type`, `project_name` |
| Plan | Sonnet or Ollama | Yes (project=low, others=high) | `plan()` | `plan` string |
| Plan (file select) | Sonnet | No (direct) | `_inject_project_files()` | Files in prompt |
| Execute (params) | Sonnet | No | `_extract_params()` | `extracted_params` |
| Execute (code gen) | Sonnet | No | `_generate_code()` | `code` |
| Execute (run) | — | — | `run_code()` / `run_shell()` | `execution_result` |
| Audit | **Opus always** | No | `audit()` | `audit_verdict`, `audit_feedback` |
| Deliver | Sonnet | No | `deliver()` | `final_response`, `artifacts` |

## Database Schema (SQLite WAL)

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `tasks` | Task lifecycle | id, user_id, status, task_type, result, created_at |
| `conversation_context` | Key-value per user | user_id, key, value |
| `conversation_history` | Message log per user | user_id, role, content, timestamp |
| `project_memory` (v8) | Success/failure patterns | project_name, memory_type, content, task_id |

## AgentState TypedDict (21 fields)

`task_id`, `user_id`, `message`, `files`, `task_type`, `project_name`, `project_config`,
`plan`, `code`, `execution_result`, `audit_verdict`, `audit_feedback`, `retry_count`,
`stage`, `extracted_params`, `working_dir`, `conversation_context`,
`auto_installed_packages`, `stage_timings` (v8), `final_response`, `artifacts`

## Telegram Commands (13)

`/start`, `/status`, `/history`, `/usage`, `/cost`, `/health`, `/exec`, `/context`,
`/cancel`, `/projects`, `/schedule`, `/chain` (v8), `/debug` (v8)

## Security Layers

- **Tier 1** — 39 blocked patterns: `rm -rf`, `sudo`, `curl|sh`, `chmod 777`, `mkfs`, fork bombs, etc. Always blocked.
- **Tier 3** — 12 audit-logged patterns: `rm`, `chmod`, `git push`, `curl`, `python3 -c`. Allowed but logged.
- **Tier 4** — 8 code scanner patterns: credential reads, `os.system()`, `shutil.rmtree(/)`, reverse shells. Scans Python content.
- **Credential stripping** — `_filter_env()` removes API keys/tokens/secrets from subprocess env via exact match + substring.
- **Docker isolation** — Optional container execution. Only `workspace/` mounted. All caps dropped, PIDs limited to 256.
- **Opus audit gate** — Every output reviewed by a different model before delivery.
- **Budget enforcement** — Daily/monthly caps checked before every Claude API call.
- **RAM guard** — Rejects tasks above 90% memory usage.

## Key Config Constants (config.py)

| Constant | Default | Purpose |
|----------|---------|---------|
| `DEFAULT_MODEL` | `claude-sonnet-4-6` | Classify, plan, execute, deliver |
| `COMPLEX_MODEL` | `claude-opus-4-6` | Audit (adversarial review) |
| `EXECUTION_TIMEOUT` | 120s | Single code execution |
| `MAX_CODE_EXECUTION_TIMEOUT` | 600s | Hard cap on execution |
| `LONG_TIMEOUT` | 900s | Full pipeline timeout |
| `MAX_RETRIES` | 3 | Audit-retry cycles |
| `MAX_CONCURRENT_TASKS` | 3 | Parallel pipeline limit |
| `RAM_THRESHOLD_PERCENT` | 90 | Reject tasks above this |
| `MAX_FILE_INJECT_COUNT` | 50 | Max project files for dynamic injection |
| `OLLAMA_DEFAULT_MODEL` | `llama3.1:8b` | Local model for low-complexity offload |

## Environment Variables (from .env)

**Required:** `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `ALLOWED_USER_IDS`
**Optional:** `DAILY_BUDGET_USD`, `MONTHLY_BUDGET_USD`, `DOCKER_ENABLED`, `DOCKER_NETWORK`,
`OLLAMA_BASE_URL`, `OLLAMA_DEFAULT_MODEL`, `EXECUTION_TIMEOUT`, `MAX_CONCURRENT_TASKS`

## Project Registry (projects_macmini.yaml — 12 registered)

Production Mac Mini has 12 projects registered with triggers and commands:
Affiliate Job Scraper, Jobs Analysis Pipeline v4, iGaming Intelligence Dashboard,
Work Reports Generator, Domain Categorisation, Industry Voices Benchmarks,
Suppliers Database, Newsletter Benchmarks, Job Auto Apply, SensiSpend V2,
Commercial Content Tracker. All paths: `/Users/agentruntime1/Desktop/`.
Dev machine uses `projects.yaml` with different local paths.

## Existing .claude/ Config

- `.claude/settings.local.json` — permissions whitelist (python3, git, docker, pip, gh, sqlite3, bash commands)
- `.claude/worktrees/zen-shaw/` — stale worktree snapshot, can likely be cleaned up
- No custom commands or skills configured yet

## INVARIANTS — DO NOT VIOLATE

1. **5-stage pipeline is FIXED.** Never add/remove stages or make the graph dynamic. Predictability > autonomy.
2. **Opus ALWAYS audits.** Cross-model adversarial review is the primary safety gate. Never route audit to Sonnet/Ollama.
3. **Pipeline nodes are synchronous** in `asyncio.to_thread()`. They use `sqlite3 + threading.Lock`, not aiosqlite. Intentional.
4. **Every new feature MUST degrade gracefully.** `try/except → log warning → continue`. No feature can crash delivery.
5. **No speculative abstractions.** No plugin systems, provider layers, or web UIs "just in case."
6. **Test everything security-critical.** Every blocked pattern has a test. Every allowed pattern has a test. Maintain this.
7. **Threat model = LLM hallucination, not adversarial users.** Single user is system owner.
8. **Deliverer must never fabricate success.** If status is FAILED, say FAILED. (Learned from real production bug.)

## Build & Test

```bash
pytest tests/ -v                          # all 548 tests
pytest tests/ -v -k "not docker"          # skip Docker-required tests
pytest tests/test_sandbox.py -v           # specific module
pytest tests/test_v8_foundation.py -v     # v8 features only
pytest tests/test_stress_v8.py -v         # adversarial stress tests (64 tests)
pytest tests/test_stress_v8_audit2.py -v  # stress round 2 (80 tests)
```

## File Reading Order (start here)

`config.py` → `brain/state.py` → `brain/graph.py` → `brain/nodes/classifier.py`
→ `brain/nodes/planner.py` → `brain/nodes/executor.py` → `brain/nodes/auditor.py`
→ `brain/nodes/deliverer.py` → `tools/sandbox.py` → `tools/model_router.py`
→ `tools/claude_client.py` → `storage/db.py` → `bot/handlers.py`

## Known Issues (from v8 audit, 24 Feb 2026 — validated 5 Mar 2026)

| ID | Severity | File | Status | Issue |
|----|----------|------|--------|-------|
| M-1 | Moderate | storage/db.py | **FIXED v8.0.2** | FIFO cap (50 rows/project) on `project_memory`. |
| M-2 | Moderate | brain/nodes/planner.py:341 | **FIXED v8.0.2** | File injection paths validated with `.resolve() + startswith()`. |
| M-3 | Moderate | bot/handlers.py:872 | **FIXED** (verified) | All file handles use context managers — no leak found. |
| m-1 | Minor | storage/db.py | OPEN | UNIQUE constraint misses semantic duplicate memories. |
| m-2 | Minor | tools/model_router.py | **FIXED v8.0.2** | Ollama uses native `system` parameter. |
| m-3 | Minor | tools/model_router.py | **FIXED v8.0.2** | `max_tokens` passed to Ollama via `options.num_predict`. |
| m-4 | Minor | tools/model_router.py | **FIXED v8.0.2** | `MODEL_COSTS` imported from `claude_client`. |
| SEC-1 | Security | tools/sandbox.py | **FIXED v8.0.2** | Shell script content now scanned against Tier 1 blocklist (cat\|bash, sudo bypasses). |
| SEC-2 | Security | brain/nodes/planner.py | **FIXED v8.0.2** | Planner refuses system credential file tasks (synthetic /etc/shadow bypass). |

## Test Coverage Gaps (from audit)

- No tests for `/chain` command (strict-AND gate, `{output}` replacement, DB lifecycle)
- No tests for `_call_ollama()` directly
- No path traversal test for `_inject_project_files()`
- No test for `_get_today_spend()` cost calculation
- No end-to-end test for `task_id` flow: handler → executor → sandbox → live output → polling

## Current Priorities / Roadmap

1. ~~**Fix open audit issues**~~ — All fixed in v8.0.2 (M-1, M-2, M-3 verified, m-2, m-3, m-4, 3 security bypasses)
2. **Deployment capability** — Start/stop local servers, deploy to Firebase/Vercel/Fly.io. Requires dedicated Google account.
3. **RAG context layer** — LanceDB + nomic-embed-text via Ollama. Replace 50-file-cap injection. Trigger: >100 files OR >8000 tokens.
4. **Visual feedback loop** — Headless Playwright checks for generated web apps (server starts? 200 OK? console errors?).
5. **Guided onboarding** — `/setup` command for new users (project registration, Ollama, budget, smoke test).
6. **Dedicated agent identity** — Separate Google account (Drive, Firebase, Gmail), Proton email, optional phone number.

## Known Limitations

- No codebase understanding — samples 3-5 files, no architectural model. RAG will help but not fully solve.
- No deployment — builds frontends/backends but can't host, serve, or deploy them.
- No visual evaluation — quality gate is "code ran without errors" only.
- Context evaporates between sessions — shallow conversation history and project memory.
- 30-min temporal window — misses longer natural gaps. 2-hour window captures ~40% more patterns.
- Memory cap — `project_memory` capped at 50 rows per project via FIFO (M-1 fixed).
