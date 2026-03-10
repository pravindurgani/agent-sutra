# AgentSutra v8.8.0 — Project Context for Claude Code

Single-user, self-hosted AI agent. Telegram-controlled. Mac Mini M2 (16GB).
Fixed 5-stage LangGraph pipeline: Classify → Plan → Execute → Audit → Deliver.
Cross-model adversarial auditing: Sonnet generates, Opus reviews.
~7,876 LOC across 21 source files. ~11,000 LOC tests across 29 files. 840 test functions (804 passing, 36 skipped).

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
| `main.py` | 240 | Entry point: env validation, DB init, crash recovery, Ollama startup test, SIGTERM handler, bot start |
| `config.py` | 144 | All constants, paths, model names, timeouts, budget caps, crash-safe env parsing, RAG config |
| `brain/state.py` | 64 | `AgentState` TypedDict — 25 fields flowing through pipeline |
| `brain/graph.py` | 167 | LangGraph wiring, `run_task()`, stage tracking, node timing, duplicate error detection, task completion summary |
| `brain/nodes/classifier.py` | 99 | Fast path (trigger match) → slow path (Claude/Ollama classify), word-boundary fallback |
| `brain/nodes/planner.py` | 428 | Task-type prompts, standards/memory/ARCHITECTURE.md/RAG-first file injection, 7 templates, refusal detection, file selector retry, legacy fallback |
| `brain/nodes/executor.py` | 860 | Code gen + sandbox execution, project commands, auto-install, truncation detection (shebang-gated + HTML), file ref validation, over-gen limits, was_refused guard |
| `brain/nodes/auditor.py` | 328 | Opus adversarial review, env error short-circuit, visual check, fabrication detection (strengthened), data sanity checks, task-type criteria expansion, XML-wrapped prompts |
| `brain/nodes/deliverer.py` | 438 | Response formatting, memory extraction, temporal mining, debug sidecar, credential filter (expanded), path sanitisation (Linux+macOS), ARCHITECTURE.md suggestion |
| `tools/sandbox.py` | 1684 | Execution sandbox: AST scanner, smart subprocess, importlib allowlist, shutil.rmtree hardening, written-file scanning, Docker, streaming, server mgmt |
| `tools/rag.py` | 324 | RAG context layer: LanceDB index, Ollama embeddings, AST-based Python chunking, query/build, zero-vector filtering |
| `tools/model_router.py` | 246 | Claude/Ollama routing by purpose, complexity, RAM, budget, empty response retry, unclosed think-block handling, Ollama reliability stats |
| `tools/claude_client.py` | 425 | Anthropic API wrapper: retries, cost tracking, streaming, midnight-based budget, daily breakdown, budget remaining |
| `tools/file_manager.py` | 157 | Upload handling, metadata extraction, content reading, JSON size cap |
| `tools/deployer.py` | 235 | Static deployment: GitHub Pages, Vercel, Firebase, credential-safe subprocess |
| `tools/visual_check.py` | 90 | Playwright headless Chromium: page load, console errors, screenshot, SSRF guard |
| `tools/projects.py` | 128 | Project registry loader, trigger matcher, word-boundary regex, mention-context exclusion, run_instructions injection |
| `storage/db.py` | 468 | SQLite ops: async (bot) + sync (pipeline), 5 tables, WAL mode, history FIFO cap, partial state persistence |
| `scheduler/cron.py` | 67 | APScheduler with SQLite persistence, prefix-length validation |
| `bot/telegram_bot.py` | 69 | Bot factory, 19 command registrations |
| `bot/handlers.py` | 1488 | All Telegram command handlers, auth, message processing, chain (refusal tracking), retry, setup, reindex, cost analytics, timeout progress feedback, Ollama health stats |

## Pipeline Flow

| Stage | Model | Router? | Key Function | Output |
|-------|-------|:---:|--------------|--------|
| Classify | Sonnet or Ollama | Yes (`complexity="low"`) | `classify()` | `task_type`, `project_name` |
| Plan | Sonnet or Ollama | Yes (frontend/ui/data=high, others=low) | `plan()` | `plan` string |
| Plan (file select) | Ollama embed or Sonnet | No | `_inject_project_files()` | RAG chunks or selected files in prompt |
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

## AgentState TypedDict (25 fields)

`task_id`, `user_id`, `message`, `files`, `task_type`, `project_name`, `project_config`,
`plan`, `code`, `execution_result`, `audit_verdict`, `audit_feedback`, `retry_count`,
`stage`, `extracted_params`, `working_dir`, `conversation_context`,
`auto_installed_packages`, `stage_timings` (v8), `server_url` (v8.2), `deploy_url` (v8.1),
`was_refused` (v8.8), `previous_audit_feedback` (v9), `final_response`, `artifacts`

## Telegram Commands (19)

`/start`, `/status`, `/history`, `/usage`, `/cost`, `/health`, `/exec`, `/context`,
`/cancel`, `/retry` (v8.6), `/projects`, `/schedule`, `/chain` (v8), `/debug` (v8), `/deploy` (v8.1),
`/servers` (v8.2), `/stopserver` (v8.2), `/setup` (v8.6), `/reindex` (v8.7)

## Security Layers

- **Tier 1** — 39 blocked patterns: `rm -rf`, `sudo`, `curl|sh`, `chmod 777`, `mkfs`, fork bombs, etc. Always blocked.
- **Tier 1+ (v8.4.1)** — Full Python code text scanned against Tier 1 blocklist (catches shell patterns in strings/comments). Script file content scanned when `bash/sh` executes a file.
- **Tier 3** — 12 audit-logged patterns: `rm`, `chmod`, `git push`, `curl`, `python3 -c`. Allowed but logged.
- **Tier 4 (v8.5.2, hardened v8.7.0, v9.0.0)** — 50 code scanner patterns: credential reads, dangerous system calls, filesystem wipes, reverse shells, config imports, os.popen, dynamic code, getattr(os), base64 decode, ctypes, chr-chain obfuscation. Smart subprocess allowlist (AST-based). Smart importlib allowlist (AST-based, stdlib-only — blocks config/dotenv/dynamic args). Scans Python content.
- **Tier 5** — JS code scanner patterns for frontend tasks.
- **Credential stripping** — `_filter_env()` removes API keys/tokens/secrets from subprocess env via exact match + substring. Applied to server processes too (v8.5.2).
- **Docker isolation** — Optional container execution. Only `workspace/` mounted. All caps dropped, PIDs limited to 256.
- **Opus audit gate** — Every output reviewed by a different model before delivery. XML-delimited prompts resist injection (v8.5.2).
- **Visual verification** — Optional Playwright check for frontend tasks: page loads, console errors, screenshot (feeds into audit prompt). Localhost-only SSRF guard (v8.5.2).
- **Fabrication detection (v8.4.1, v9.0.0)** — Auditor checks if agent substituted libraries, faked data, or rewrote the task. Data sanity checks catch mathematically impossible metrics (e.g., 0 impressions with non-zero clicks). Deliverer enforces honest failure reporting.
- **Chain strict-AND gate (v8.4.1)** — Exit-code based halting (Claude can't fake exit codes). Literal execution prefix prevents graceful rewriting.
- **AST constant folding (v8.7.0)** — Resolves string concatenation (`"su" + "do"` → `"sudo"`) in Python AST to catch obfuscation bypasses.
- **Written-file scanning (v8.7.0)** — Post-execution scan of newly created .sh/.py/.js files against existing blocklists.
- **Truncation detection (v8.4.1, extended v8.7.0, fixed v8.8.0, v9.0.0)** — Detects code cut off by max_tokens (unclosed parens/brackets/strings, shell if/fi do/done, HTML html/script/style tags). Shell checks gated by shebang detection. HTML checks gated by DOCTYPE/html detection. Auto-retries with shorter prompt.
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
| `LONG_TIMEOUT` | 1800s | Full pipeline timeout |
| `MAX_RETRIES` | 3 | Audit-retry cycles |
| `MAX_CONCURRENT_TASKS` | 3 | Parallel pipeline limit |
| `RAM_THRESHOLD_PERCENT` | 90 | Reject tasks above this |
| `MAX_FILE_INJECT_COUNT` | 50 | Max project files for dynamic injection |
| `OLLAMA_DEFAULT_MODEL` | `deepseek-r1:14b` | Local model for planning offload |
| `OLLAMA_CLASSIFY_MODEL` | `qwen2.5:7b` | Local model for classification (lighter, faster) |
| `SERVER_START_TIMEOUT` | 30s | Max wait for server HTTP response |
| `SERVER_MAX_LIFETIME` | 300s | Auto-kill servers after this |
| `SERVER_PORT_RANGE_START` | 8100 | Port range for dev servers |
| `SERVER_PORT_RANGE_END` | 8120 | Port range upper bound |
| `DEPLOY_FIREBASE_PROJECT` | (empty) | Firebase project ID for hosting |
| `DEPLOY_FIREBASE_TOKEN` | (empty) | Firebase CI token (credential-stripped) |
| `VISUAL_CHECK_ENABLED` | false | Enable Playwright visual verification |
| `VISUAL_CHECK_TIMEOUT` | 15s | Navigation timeout for visual checks |
| `RAG_ENABLED` | true | Enable RAG-based file injection in planner |
| `RAG_EMBED_MODEL` | `nomic-embed-text` | Ollama embedding model for RAG |
| `RAG_INDEX_DIR` | `~/.agentsutra/rag_indexes` | LanceDB index storage |
| `RAG_TOP_K` | 8 | Chunks to retrieve per query |
| `RAG_STALE_HOURS` | 24 | Re-index if older than this |
| `ENABLE_THINKING` | true | Adaptive thinking for Sonnet/Opus |
| `DAILY_BUDGET_USD` | 0 (unlimited) | Daily API spend cap with midnight cutoff |
| `MONTHLY_BUDGET_USD` | 0 (unlimited) | Monthly API spend cap |
| `API_MAX_RETRIES` | 5 | Claude API call retries (rate limit, timeout) |

## Environment Variables (from .env)

**Required:** `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `ALLOWED_USER_IDS`
**Optional:** `DAILY_BUDGET_USD`, `MONTHLY_BUDGET_USD`, `DOCKER_ENABLED`, `DOCKER_NETWORK`,
`OLLAMA_BASE_URL`, `OLLAMA_DEFAULT_MODEL`, `OLLAMA_CLASSIFY_MODEL`, `EXECUTION_TIMEOUT`, `MAX_CONCURRENT_TASKS`,
`DEPLOY_ENABLED`, `DEPLOY_PROVIDER`, `DEPLOY_FIREBASE_PROJECT`, `DEPLOY_FIREBASE_TOKEN`,
`ENABLE_THINKING`, `DEFAULT_MODEL`, `COMPLEX_MODEL`, `RAG_ENABLED`, `RAG_EMBED_MODEL`

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
just test-quick                           # skip Docker, stop on first failure
just test-security                        # security-critical tests only
pytest tests/ -v                          # all 840 tests (804 pass, 36 skip)
pytest tests/ -v -k "not docker"          # skip Docker-required tests
pytest tests/test_sandbox.py -v           # sandbox + AST scanner + written-file scanning
pytest tests/test_rag.py -v              # RAG context layer (22 tests)
pytest tests/test_v8_remediation.py -v    # v8.4.1+ remediation tests
pytest tests/test_stress_v8.py -v         # adversarial stress tests (64 tests)
pytest tests/test_stress_v8_audit2.py -v  # stress round 2 (80 tests)
```

## File Reading Order (start here)

`config.py` → `brain/state.py` → `brain/graph.py` → `brain/nodes/classifier.py`
→ `brain/nodes/planner.py` → `brain/nodes/executor.py` → `brain/nodes/auditor.py`
→ `brain/nodes/deliverer.py` → `tools/sandbox.py` → `tools/rag.py`
→ `tools/model_router.py` → `tools/claude_client.py` → `storage/db.py` → `bot/handlers.py`

## Known Issues (audits: v8 24 Feb, v8.4.1 6 Mar, v8.5.1 7 Mar, v8.7.0 8 Mar, v8.8.0 9 Mar 2026)

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
| F-1 | Critical | brain/nodes/executor.py | **FIXED v8.8.0** | Shell truncation `\bif\b` regex false-positived on Python code. Shebang-gated now. |
| F-3 | Medium | tools/claude_client.py | **FIXED v8.8.0** | `/cost` model name displayed "6" instead of "sonnet-4". |
| F-5 | Medium | tools/model_router.py | **FIXED v8.8.0** | Budget escalation routed high-complexity tasks to Ollama. Guarded by `complexity != "high"`. |

## Test Coverage Gaps (from audit)

- ~~No tests for `/chain` command~~ — **FIXED v8.4.1**: 7 tests for strict-AND gate and literal execution prefix
- ~~No tests for `_call_ollama()` directly~~ — **FIXED v8.4.1**: 4 tests for /api/chat endpoint, 404 fallback, legacy
- ~~No path traversal test for `_inject_project_files()`~~ — Partially covered by A-8 working_dir validation test
- No test for `_get_today_spend()` cost calculation
- No end-to-end test for `task_id` flow: handler → executor → sandbox → live output → polling

## Current Priorities / Roadmap

1. ~~**Fix open audit issues**~~ — All fixed in v8.0.2 (M-1, M-2, M-3 verified, m-2, m-3, m-4, 3 security bypasses)
2. ~~**Deployment capability**~~ — GitHub Pages, Vercel, Firebase deploy. Local server management. `/deploy`, `/servers`, `/stopserver`. (v8.1.0–v8.4.0)
3. ~~**RAG context layer**~~ — LanceDB + nomic-embed-text via Ollama. AST-based Python chunking, semantic file injection replaces 50-file-cap lottery. `/reindex` command. (v8.7.0)
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
14. ~~**Post-production hardening (v8.8.0)**~~ — Shell truncation false-positive fix (F-1), credential filter expansion, budget escalation guard, Ollama think-block handling, chain refusal tracking, RAG zero-vector guard, over-generation limits, timeout progress feedback. 13/14 phases. See `IMPLEMENTATION_SUMMARY.md`.
15. **Per-task cost tracking** — Add `task_id` to `api_usage` table for "this task cost $X" in delivery.
16. **Ollama health check before routing** — Verify Ollama is running before routing tasks to it.
17. **Audit feedback loop** — Pass `audit_feedback` to executor on retry instead of blind regeneration.

## Known Limitations

- Codebase understanding limited — RAG injects semantically relevant chunks (v8.7.0), but no full architectural model. Projects with >500 files skip indexing.
- Context evaporates between sessions — shallow conversation history and project memory.
- Memory cap — `project_memory` capped at 50 rows per project via FIFO (M-1 fixed).
- No audit feedback loop — audit retry regenerates blind (executor doesn't receive `audit_feedback`).
- No per-task cost attribution — cost tracked globally, not per-task.
- Single-model generation — no fallback if Sonnet produces bad code (relies on retry loop).

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
