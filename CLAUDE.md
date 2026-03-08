# AgentSutra v8.6.0 — Project Context for Claude Code

Single-user, self-hosted AI agent. Telegram-controlled. Mac Mini M2 (16GB).
Fixed 5-stage LangGraph pipeline: Classify → Plan → Execute → Audit → Deliver.
Cross-model adversarial auditing: Sonnet generates, Opus reviews.
~7,050 LOC across 20 source files. ~9,000 LOC tests across 24 files. 661 test functions (625 passing, 36 skipped/Docker).

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
| `main.py` | 212 | Entry point: env validation, DB init, crash recovery, Ollama check, SIGTERM handler, bot start |
| `config.py` | 133 | All constants, paths, model names, timeouts, budget caps, crash-safe env parsing |
| `brain/state.py` | 58 | `AgentState` TypedDict — 23 fields flowing through pipeline |
| `brain/graph.py` | 143 | LangGraph wiring, `run_task()`, stage tracking, node timing, state persistence |
| `brain/nodes/classifier.py` | 100 | Fast path (trigger match) → slow path (Claude/Ollama classify), word-boundary fallback |
| `brain/nodes/planner.py` | 380 | Task-type prompts, standards/memory/file injection, 7 templates, symlink-safe enumeration |
| `brain/nodes/executor.py` | 743 | Code gen + sandbox execution, project commands, auto-install, truncation detection, path validation |
| `brain/nodes/auditor.py` | 305 | Opus adversarial review, env error short-circuit, visual check, fabrication detection, XML-wrapped prompts |
| `brain/nodes/deliverer.py` | 376 | Response formatting, memory extraction, temporal mining, debug sidecar, security blocking |
| `tools/sandbox.py` | 1401 | Execution sandbox: blocklist, 51-pattern code scanner, script scanning, Docker, streaming, server mgmt |
| `tools/model_router.py` | 202 | Claude/Ollama routing by purpose, complexity, RAM, budget, /api/chat migration |
| `tools/claude_client.py` | 425 | Anthropic API wrapper: retries, cost tracking, streaming, midnight-based budget, daily breakdown, budget remaining |
| `tools/file_manager.py` | 157 | Upload handling, metadata extraction, content reading, JSON size cap |
| `tools/deployer.py` | 235 | Static deployment: GitHub Pages, Vercel, Firebase, credential-safe subprocess |
| `tools/visual_check.py` | 90 | Playwright headless Chromium: page load, console errors, screenshot, SSRF guard |
| `tools/projects.py` | 105 | Project registry loader, trigger matcher, word-boundary regex |
| `storage/db.py` | 468 | SQLite ops: async (bot) + sync (pipeline), 5 tables, WAL mode, history FIFO cap, partial state persistence |
| `scheduler/cron.py` | 67 | APScheduler with SQLite persistence, prefix-length validation |
| `bot/telegram_bot.py` | 67 | Bot factory, 18 command registrations |
| `bot/handlers.py` | 1381 | All Telegram command handlers, auth, message processing, chain, retry, setup, cost analytics |

## Pipeline Flow

| Stage | Model | Router? | Key Function | Output |
|-------|-------|:---:|--------------|--------|
| Classify | Sonnet or Ollama | Yes (`complexity="low"`) | `classify()` | `task_type`, `project_name` |
| Plan | Sonnet or Ollama | Yes (project=low, others=high) | `plan()` | `plan` string |
| Plan (file select) | Sonnet | No (direct) | `_inject_project_files()` | Files in prompt |
| Execute (params) | Sonnet | No | `_extract_params()` | `extracted_params` |
| Execute (code gen) | Sonnet | No | `_generate_code()` | `code` |
| Execute (run) | — | — | `run_code()` / `run_shell()` | `execution_result` |
| Audit | **Opus always** | No | `audit()` | `audit_verdict`, `audit_feedback` (+ visual check context for frontend) |
| Deliver | Sonnet | No | `deliver()` | `final_response`, `artifacts`, `deploy_url` |

## Database Schema (SQLite WAL, 5 tables)

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `tasks` | Task lifecycle | id, user_id, status, task_type, result, task_state, last_completed_stage, created_at |
| `conversation_context` | Key-value per user | user_id, key, value |
| `conversation_history` | Message log per user | user_id, role, content, timestamp |
| `project_memory` (v8) | Success/failure patterns | project_name, memory_type, content, task_id |
| `api_usage` | API cost tracking | model, input_tokens, output_tokens, thinking_tokens, timestamp |

## AgentState TypedDict (23 fields)

`task_id`, `user_id`, `message`, `files`, `task_type`, `project_name`, `project_config`,
`plan`, `code`, `execution_result`, `audit_verdict`, `audit_feedback`, `retry_count`,
`stage`, `extracted_params`, `working_dir`, `conversation_context`,
`auto_installed_packages`, `stage_timings` (v8), `server_url` (v8.2), `deploy_url` (v8.1), `final_response`, `artifacts`

## Telegram Commands (18)

`/start`, `/status`, `/history`, `/usage`, `/cost`, `/health`, `/exec`, `/context`,
`/cancel`, `/retry` (v8.6), `/projects`, `/schedule`, `/chain` (v8), `/debug` (v8), `/deploy` (v8.1),
`/servers` (v8.2), `/stopserver` (v8.2), `/setup` (v8.6)

## Security Layers

- **Tier 1** — 39 blocked patterns: `rm -rf`, `sudo`, `curl|sh`, `chmod 777`, `mkfs`, fork bombs, etc. Always blocked.
- **Tier 1+ (v8.4.1)** — Full Python code text scanned against Tier 1 blocklist (catches shell patterns in strings/comments). Script file content scanned when `bash/sh` executes a file.
- **Tier 3** — 12 audit-logged patterns: `rm`, `chmod`, `git push`, `curl`, `python3 -c`. Allowed but logged.
- **Tier 4 (v8.5.2)** — 51 code scanner patterns: credential reads, dangerous system calls, filesystem wipes, reverse shells, config imports, os.popen, dynamic code, subprocess, getattr(os), base64 decode, ctypes, chr-chain obfuscation. Scans Python content.
- **Tier 5** — JS code scanner patterns for frontend tasks.
- **Credential stripping** — `_filter_env()` removes API keys/tokens/secrets from subprocess env via exact match + substring. Applied to server processes too (v8.5.2).
- **Docker isolation** — Optional container execution. Only `workspace/` mounted. All caps dropped, PIDs limited to 256.
- **Opus audit gate** — Every output reviewed by a different model before delivery. XML-delimited prompts resist injection (v8.5.2).
- **Visual verification** — Optional Playwright check for frontend tasks: page loads, console errors, screenshot (feeds into audit prompt). Localhost-only SSRF guard (v8.5.2).
- **Fabrication detection (v8.4.1)** — Auditor checks if agent substituted libraries, faked data, or rewrote the task. Deliverer enforces honest failure reporting.
- **Chain strict-AND gate (v8.4.1)** — Exit-code based halting (Claude can't fake exit codes). Literal execution prefix prevents graceful rewriting.
- **Truncation detection (v8.4.1)** — Detects code cut off by max_tokens (unclosed parens/brackets/strings). Auto-retries with shorter prompt.
- **Budget enforcement** — Daily/monthly caps with midnight-based cutoffs (v8.5.2) checked before every Claude API call.
- **RAM guard** — Rejects tasks above 90% memory usage.
- **Input validation (v8.5.2)** — Crash-safe env parsing, hex-validated task IDs, file upload caps (10), JSON size caps (10MB), working directory path validation.

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
| `SERVER_START_TIMEOUT` | 30s | Max wait for server HTTP response |
| `SERVER_MAX_LIFETIME` | 300s | Auto-kill servers after this |
| `SERVER_PORT_RANGE_START` | 8100 | Port range for dev servers |
| `SERVER_PORT_RANGE_END` | 8120 | Port range upper bound |
| `DEPLOY_FIREBASE_PROJECT` | (empty) | Firebase project ID for hosting |
| `DEPLOY_FIREBASE_TOKEN` | (empty) | Firebase CI token (credential-stripped) |
| `VISUAL_CHECK_ENABLED` | false | Enable Playwright visual verification |
| `VISUAL_CHECK_TIMEOUT` | 15s | Navigation timeout for visual checks |
| `ENABLE_THINKING` | true | Adaptive thinking for Sonnet/Opus |
| `DAILY_BUDGET_USD` | 0 (unlimited) | Daily API spend cap with midnight cutoff |
| `MONTHLY_BUDGET_USD` | 0 (unlimited) | Monthly API spend cap |
| `API_MAX_RETRIES` | 5 | Claude API call retries (rate limit, timeout) |

## Environment Variables (from .env)

**Required:** `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `ALLOWED_USER_IDS`
**Optional:** `DAILY_BUDGET_USD`, `MONTHLY_BUDGET_USD`, `DOCKER_ENABLED`, `DOCKER_NETWORK`,
`OLLAMA_BASE_URL`, `OLLAMA_DEFAULT_MODEL`, `EXECUTION_TIMEOUT`, `MAX_CONCURRENT_TASKS`,
`DEPLOY_ENABLED`, `DEPLOY_PROVIDER`, `DEPLOY_FIREBASE_PROJECT`, `DEPLOY_FIREBASE_TOKEN`,
`ENABLE_THINKING`, `DEFAULT_MODEL`, `COMPLEX_MODEL`

## Project Registry (projects_macmini.yaml — 12 registered)

Production Mac Mini has 12 projects registered with triggers and commands:
Affiliate Job Scraper, Jobs Analysis Pipeline v4, iGaming Intelligence Dashboard,
Work Reports Generator, Domain Categorisation, Industry Voices Benchmarks,
Suppliers Database, Newsletter Benchmarks, Job Auto Apply, SensiSpend V2,
Commercial Content Tracker. All paths: `/Users/agentruntime1/Desktop/`.
Dev machine uses `projects.yaml` with different local paths.

## Existing .claude/ Config

- `.claude/settings.local.json` — permissions whitelist (python3, git, docker, pip, gh, sqlite3, bash commands)
- `.claude/commands/` — 4 custom commands: audit-node, fix-issue, implement, review-pr (enhanced v8.6.0)
- `Justfile` — dev commands: test, test-quick, test-security, lint, format, run
- `.pre-commit-config.yaml` — ruff lint/format, large file check, private key detection, yaml check
- `.github/workflows/ci.yml` — GitHub Actions: lint + test on push/PR to main
- `SESSION_LOG.md` — separate file for session log (moved from CLAUDE.md in v8.6.0)
- `scripts/` — `com.agentsutra.bot.plist` (launchd) + `install_service.sh` for Mac Mini deployment

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
just test-quick                           # skip Docker, stop on first failure (625 pass)
just test-security                        # security-critical tests only
pytest tests/ -v                          # all 661 tests
pytest tests/ -v -k "not docker"          # skip Docker-required tests (625 pass)
pytest tests/test_sandbox.py -v           # specific module (incl. Tier 1+ scanning, 51 code scanner patterns)
pytest tests/test_v8_foundation.py -v     # v8 features only
pytest tests/test_v8_remediation.py -v    # v8.4.1 remediation tests (28 tests)
pytest tests/test_stress_v8.py -v         # adversarial stress tests (64 tests)
pytest tests/test_stress_v8_audit2.py -v  # stress round 2 (80 tests)
```

## File Reading Order (start here)

`config.py` → `brain/state.py` → `brain/graph.py` → `brain/nodes/classifier.py`
→ `brain/nodes/planner.py` → `brain/nodes/executor.py` → `brain/nodes/auditor.py`
→ `brain/nodes/deliverer.py` → `tools/sandbox.py` → `tools/model_router.py`
→ `tools/claude_client.py` → `storage/db.py` → `bot/handlers.py`

## Known Issues (audits: v8 24 Feb, v8.4.1 6 Mar, v8.5.1 7 Mar 2026)

| ID | Severity | File | Status | Issue |
|----|----------|------|--------|-------|
| M-1 | Moderate | storage/db.py | **FIXED v8.0.2** | FIFO cap (50 rows/project) on `project_memory`. |
| M-2 | Moderate | brain/nodes/planner.py | **FIXED v8.0.2** | File injection paths validated with `.resolve() + startswith()`. |
| M-3 | Moderate | bot/handlers.py | **FIXED** (verified) | All file handles use context managers — no leak found. |
| m-1 | Minor | storage/db.py | OPEN | UNIQUE constraint misses semantic duplicate memories. |
| m-2 | Minor | tools/model_router.py | **FIXED v8.0.2** | Ollama uses native `system` parameter. |
| m-3 | Minor | tools/model_router.py | **FIXED v8.0.2** | `max_tokens` passed to Ollama via `options.num_predict`. |
| m-4 | Minor | tools/model_router.py | **FIXED v8.0.2** | `MODEL_COSTS` imported from `claude_client`. |
| SEC-1 | Security | tools/sandbox.py | **FIXED v8.0.2** | Shell script content scanned against Tier 1 blocklist. |
| SEC-2 | Security | brain/nodes/planner.py | **FIXED v8.0.2** | Planner refuses system credential file tasks. |
| R.1 | Critical | tools/sandbox.py | **FIXED v8.4.1** | Python-embedded shell patterns scanned. Script file content scanned on bash/sh. |
| R.2 | High | bot/handlers.py | **FIXED v8.4.1** | Chain strict-AND gate uses exit codes. |
| R.3 | Medium | brain/nodes/executor.py | **FIXED v8.4.1** | Truncation detection with automatic shorter re-generation. |
| R.4 | Medium | tools/model_router.py | **FIXED v8.4.1** | Ollama migrated to /api/chat endpoint. |
| R.5 | Medium | brain/nodes/auditor.py | **FIXED v8.4.1** | Fabrication detection in auditor. |
| A-1 | Critical | tools/sandbox.py | **FIXED v8.5.2** | start_server bypassed blocklist + leaked credentials. Now safety-checked + env-filtered. |
| A-2–6 | Critical/High | tools/sandbox.py | **FIXED v8.5.2** | Code scanner gaps: config import, os.popen, dynamic code, subprocess, obfuscation. 12 new patterns. |
| A-7–8 | High | brain/nodes/executor.py | **FIXED v8.5.2** | LLM output trust: shlex.quote on pip paths, working_dir validated under workspace. |
| A-9,20,21 | High/Med | brain/nodes/auditor.py | **FIXED v8.5.2** | Audit prompt injection: XML-delimited, tail-truncated, pass-fallback removed. |
| A-14–17 | Medium | bot/handlers.py | **FIXED v8.5.2** | Handler validation: hex task IDs, chain resource check, file upload cap. |
| A-22–37 | Med/Low | multiple | **FIXED v8.5.2** | Resource housekeeping: safe env parsing, midnight budgets, FIFO history, SIGTERM handler. |

## Test Coverage Gaps (from audit)

- ~~No tests for `/chain` command~~ — **FIXED v8.4.1**: 7 tests for strict-AND gate and literal execution prefix
- ~~No tests for `_call_ollama()` directly~~ — **FIXED v8.4.1**: 4 tests for /api/chat endpoint, 404 fallback, legacy
- ~~No path traversal test for `_inject_project_files()`~~ — Partially covered by A-8 working_dir validation test
- No test for `_get_today_spend()` cost calculation
- No end-to-end test for `task_id` flow: handler → executor → sandbox → live output → polling

## Current Priorities / Roadmap

1. ~~**Fix open audit issues**~~ — All fixed in v8.0.2 (M-1, M-2, M-3 verified, m-2, m-3, m-4, 3 security bypasses)
2. ~~**Deployment capability**~~ — GitHub Pages, Vercel, Firebase deploy. Local server management. `/deploy`, `/servers`, `/stopserver`. (v8.1.0–v8.4.0)
3. **RAG context layer** — LanceDB + nomic-embed-text via Ollama. Replace 50-file-cap injection. Trigger: >100 files OR >8000 tokens.
4. ~~**Visual feedback loop**~~ — Playwright headless Chromium checks: page load, console errors, screenshot. Feeds into audit prompt. (v8.3.0)
5. ~~**Guided onboarding**~~ — `/setup` command validates env, Ollama, projects, DB, budget. (v8.6.0)
6. ~~**Dedicated agent identity**~~ — Firebase Hosting with dedicated deploy account. (v8.4.0)
7. ~~**Third-pass security hardening**~~ — 37 findings across 6 root causes, all remediated. (v8.5.2)
8. ~~**DX improvements**~~ — Justfile, pre-commit hooks, GitHub Actions CI, enhanced Claude commands, session log rotation. (v8.6.0)
9. ~~**Partial result preservation**~~ — Pipeline state persisted after each node. `/status <task_id>` shows plan, audit, timings. (v8.6.0)
10. ~~**Cost analytics**~~ — `/cost` shows 7-day breakdown by day and model, budget remaining. (v8.6.0)
11. ~~**`/retry` command**~~ — Re-run failed tasks with same input. (v8.6.0)
12. ~~**Temporal window expansion**~~ — 30min → 2hr. Captures ~40% more follow-up patterns. (v8.6.0)
13. ~~**Launchd service**~~ — Auto-start/restart on Mac Mini. (v8.6.0)

## Known Limitations

- No codebase understanding — samples 3-5 files, no architectural model. RAG will help but not fully solve.
- Context evaporates between sessions — shallow conversation history and project memory.
- Memory cap — `project_memory` capped at 50 rows per project via FIFO (M-1 fixed).

---

## Session Log

Session log lives in `SESSION_LOG.md`. Append entries there, not here.

Format:
```
### YYYY-MM-DD — <one-line task summary>
- **Done**: what was completed
- **Decisions**: architectural or technical choices made, and why
- **Next**: open items or follow-ups
```

<!-- session ended: 2026-03-08 00:40 -->

<!-- session ended: 2026-03-08 00:47 -->

<!-- session ended: 2026-03-08 00:49 -->
