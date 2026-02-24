# AgentSutra v8.0.0 - Complete Codebase Reference

Every folder, file, and configuration in the AgentSutra project — what it is, what it does, and why it was built this way.

**Generated:** 2026-02-24

---

## Table of Contents

1. [Project Overview](#project-overview)
2. [Root Directory Files](#root-directory-files)
3. [bot/ — Telegram Interface Layer](#bot--telegram-interface-layer)
4. [brain/ — Agent Pipeline Core](#brain--agent-pipeline-core)
5. [brain/nodes/ — Pipeline Stage Implementations](#brainnodes--pipeline-stage-implementations)
6. [tools/ — Shared Utilities](#tools--shared-utilities)
7. [storage/ — Persistence Layer](#storage--persistence-layer)
8. [scheduler/ — Task Scheduling](#scheduler--task-scheduling)
9. [tests/ — Test Suite](#tests--test-suite)
10. [scripts/ — Deployment & Maintenance](#scripts--deployment--maintenance)
11. [workspace/ — Runtime Artifacts](#workspace--runtime-artifacts)
12. [.agentsutra/ — Project Standards](#agentsutra--project-standards)
13. [Configuration & Environment](#configuration--environment)
14. [Docker Files](#docker-files)
15. [Documentation Files](#documentation-files)
16. [External Reference Files](#external-reference-files)
17. [Architecture Decisions](#architecture-decisions)

---

## Project Overview

AgentSutra is a self-hosted Telegram bot that receives natural language tasks, processes them through a 5-stage LangGraph pipeline (Classify → Plan → Execute → Audit → Deliver), and returns results. It runs on a Mac Mini M2 (16GB) for a single authenticated user.

**Key numbers (v8.0.0):**
- ~5,000 lines of application code across 16 source files
- ~6,500 lines of test code across 18 test files
- 527 automated tests (527 pass, 11 skip — Docker required)
- 13 Telegram commands
- 7 task types
- 39 blocked command patterns (Tier 1 security)
- 5-stage pipeline with cross-model auditing (Sonnet writes, Opus reviews)

---

## Root Directory Files

### `main.py` (142 lines, 4.5 KB)
**Purpose:** Application entry point and boot sequence.

**What it does:**
- Imports `config` first to ensure `.env` is loaded before any other module
- Configures logging to both console and `agentsutra.log` via `RotatingFileHandler` (10 MB max, 3 backups)
- Validates required environment variables (`ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `ALLOWED_USER_IDS`)
- Initialises the SQLite database (`init_db()`) and recovers stale tasks from previous crashes
- Bootstraps the shared project venv at `workspace/project_venv/` with a smoke test (`pip --version`)
- Loads the project registry from `projects.yaml`
- Starts the APScheduler, then runs the Telegram bot via `application.run_polling()`

**Why this way:** Single entry point that fails fast on misconfiguration. The shared venv bootstrap was added in v7 to eliminate manual venv creation per project. Crash recovery marks orphaned "running" tasks as "crashed" so `/history` shows the real reason.

---

### `config.py` (95 lines, 3.6 KB)
**Purpose:** Centralised configuration loaded from `.env` with sensible defaults.

**What it does:**
- Defines all path constants (`BASE_DIR`, `WORKSPACE_DIR`, `UPLOADS_DIR`, `OUTPUTS_DIR`, `PROJECTS_DIR`, `DB_PATH`)
- Parses environment variables for API keys, model names, timeouts, budget limits, Docker settings
- Creates required directories on import (`mkdir(parents=True, exist_ok=True)`)
- Defines `PROTECTED_ENV_KEYS` and `PROTECTED_ENV_SUBSTRINGS` for credential stripping from subprocess environments
- `VERSION = "8.0.0"` — single source of truth for the version string

**Why this way:** All settings in one module means any file can `import config` and access everything. No scattered `os.getenv()` calls. The `_parse_user_ids()` helper handles malformed comma-separated IDs gracefully.

**Key constants:**
| Constant | Default | Purpose |
|----------|---------|---------|
| `DEFAULT_MODEL` | `claude-sonnet-4-6` | Classify, plan, execute |
| `COMPLEX_MODEL` | `claude-opus-4-6` | Audit (cross-model review) |
| `EXECUTION_TIMEOUT` | 120s | Single code execution |
| `MAX_CODE_EXECUTION_TIMEOUT` | 600s | Hard cap on execution |
| `LONG_TIMEOUT` | 900s | Full pipeline timeout |
| `MAX_RETRIES` | 3 | Audit-retry cycles |
| `DAILY_BUDGET_USD` | 0 (unlimited) | Daily API spend cap |
| `MONTHLY_BUDGET_USD` | 0 (unlimited) | Monthly API spend cap |
| `MAX_CONCURRENT_TASKS` | 3 | Parallel pipeline limit |
| `RAM_THRESHOLD_PERCENT` | 90 | Reject tasks above this |
| `MAX_FILE_INJECT_COUNT` | 50 | Max project source files for dynamic injection |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Local Ollama endpoint |
| `OLLAMA_DEFAULT_MODEL` | `llama3.1:8b` | Default local model |

---

### `requirements.txt` (28 lines, 580 B)
**Purpose:** Python package dependencies with minimum version constraints.

**Key dependencies:**
| Package | Purpose |
|---------|---------|
| `anthropic>=0.50.0` | Claude API client (supports streaming, thinking) |
| `langgraph>=0.2.0` | Agent workflow state machine |
| `langchain-core>=0.3.0` | Required by LangGraph |
| `python-telegram-bot>=21.0` | Telegram bot framework (async) |
| `aiosqlite>=0.20.0` | Async SQLite for bot handlers |
| `apscheduler>=3.10.0` | Cron-like task scheduling |
| `pandas>=2.0.0` | Data analysis |
| `duckdb>=1.0.0` | Memory-efficient big data queries |
| `matplotlib`, `seaborn` | Chart generation |
| `openpyxl` | Excel file support |
| `beautifulsoup4`, `requests` | Web scraping and HTTP |
| `duckduckgo-search` | Web search capability |
| `python-dotenv` | `.env` file loading |
| `psutil` | RAM/disk monitoring |
| `pyyaml` | Projects registry parsing |

**Why this way:** Minimum versions (not pinned) for flexibility during development. `requirements.lock` provides pinned versions for reproducible production deploys.

---

### `requirements.lock` (80 lines, 2.2 KB)
**Purpose:** Pinned transitive dependency versions for reproducible builds.

**Why this way:** Generated by `pip freeze` after a working install. Ensures the Mac Mini production environment matches development exactly. Updated when dependencies change.

---

### `projects.yaml` (varies)
**Purpose:** Live project registry defining registered projects, their commands, triggers, and directories.

**Format:**
```yaml
project_name:
  path: /absolute/path/to/project
  description: What this project does
  commands:
    run: "python main.py --client {client}"
  triggers:
    - "run the scraper"
    - "scrape jobs"
  requires_file: false
  timeout: 300
  venv: /path/to/project/venv  # optional
```

**Why this way:** YAML is human-editable and version-controllable. Trigger keywords enable natural language matching without Claude API calls (free fast path). The `{client}`, `{file}` placeholders are extracted by the executor from the user's message.

---

### `projects.yaml.example` (78 lines, 2.5 KB)
**Purpose:** Documented template showing project registry structure with 3 realistic examples.

**Why it exists:** Users need a reference for the YAML schema. Checked into git (unlike `projects.yaml` which may contain local paths).

---

### `projects_macmini.yaml` (varies, ~12 KB)
**Purpose:** Machine-specific project configuration for the production Mac Mini.

**Why it exists:** The Mac Mini has different project paths than the development machine. This file is the live configuration used in production. Not version-controlled (contains local paths).

---

### `LICENSE` (1.0 KB)
**Purpose:** MIT License. Permissive open-source license.

---

## bot/ — Telegram Interface Layer

The bot module handles all user interaction through Telegram. It is the only external-facing interface.

### `bot/__init__.py` (0 lines)
Package marker. Empty.

### `bot/telegram_bot.py` (57 lines, 1.9 KB)
**Purpose:** Bot application factory and command registration.

**What it does:**
- Creates the Telegram bot application using `ApplicationBuilder`
- Registers all 13 command handlers (`/start`, `/status`, `/history`, `/usage`, `/cost`, `/health`, `/exec`, `/context`, `/cancel`, `/projects`, `/schedule`, `/chain`, `/debug`)
- Registers file handlers (documents, photos) and the catch-all text message handler
- Returns the configured application for `main.py` to start

**Why this way:** Separated from `handlers.py` to keep the wiring clean. The factory pattern makes it easy to see all registered handlers at a glance. File handlers are registered before the text handler because Telegram dispatches to the first matching handler.

### `bot/handlers.py` (~942 lines, 35 KB)
**Purpose:** All Telegram command handlers, message processing, authentication, and resource management.

**What it does:**
- `auth_required` decorator — checks `ALLOWED_USER_IDS` before every handler
- `_check_resources()` — rejects tasks if RAM > threshold or concurrent tasks >= limit
- `handle_message()` — the main entry point for user messages:
  - Saves pending files, creates DB task record
  - Sends "Processing..." status message
  - Launches pipeline in `asyncio.to_thread()` with `asyncio.wait_for(timeout)`
  - Streams stage updates with hash-gated Telegram edits (v8)
  - Enriches execution stage with live stdout tail (v8)
  - Delivers result text + artifact files
- `cmd_chain()` — strict-AND task chaining with `{output}` artifact passing (v8)
- `cmd_debug()` — reads `.debug.json` sidecar for a given task ID (v8)
- `handle_document()` / `handle_photo()` — file upload handlers
- `_send_long_message()` — splits messages exceeding Telegram's 4096 char limit
- `_sanitize_error_for_user()` — strips paths and API key fragments from error messages
- 5-second cooldown per user via `context.user_data` to prevent spam

**Why this way:** All handlers in one file because they share the auth decorator, resource checks, and DB access patterns. The hash-gated edit approach (v8) replaces naive stage comparison — it handles both stage changes AND live stdout updates without redundant Telegram API calls. The 3-second polling interval stays well within Telegram's ~30 edits/minute rate limit.

---

## brain/ — Agent Pipeline Core

The brain module implements the 5-stage LangGraph pipeline: Classify → Plan → Execute → Audit → Deliver.

### `brain/__init__.py` (0 lines)
Package marker.

### `brain/state.py` (52 lines, 1.4 KB)
**Purpose:** Defines `AgentState` TypedDict — the single data structure that flows through the entire pipeline.

**Fields (21 total):**
| Field | Type | Set By |
|-------|------|--------|
| `task_id` | `str` | Handler |
| `user_id` | `int` | Handler |
| `message` | `str` | Handler |
| `files` | `list[str]` | Handler |
| `task_type` | `str` | Classifier |
| `project_name` | `str` | Classifier |
| `project_config` | `dict` | Classifier |
| `plan` | `str` | Planner |
| `code` | `str` | Executor |
| `execution_result` | `str` | Executor |
| `audit_verdict` | `str` | Auditor |
| `audit_feedback` | `str` | Auditor |
| `retry_count` | `int` | Auditor |
| `stage` | `str` | Graph |
| `extracted_params` | `dict` | Executor |
| `working_dir` | `str` | Executor |
| `conversation_context` | `str` | Handler |
| `auto_installed_packages` | `list[str]` | Executor |
| `stage_timings` | `list[dict]` | Graph (v8) |
| `final_response` | `str` | Deliverer |
| `artifacts` | `list[str]` | Deliverer |

**Why TypedDict:** LangGraph requires a typed state dict. TypedDict gives type checker support without runtime overhead. Each node function returns a partial dict that LangGraph merges into the state.

### `brain/graph.py` (134 lines, 4.1 KB)
**Purpose:** Wires the 5-stage pipeline using LangGraph's StateGraph and provides the `run_task()` entry point.

**What it does:**
- `build_graph()` — creates the StateGraph with 5 nodes and edges, including the conditional retry loop from audit back to plan
- `_wrap_node()` — wraps each node function to update stage tracking (for Telegram streaming) and record per-node timing in `stage_timings` (v8)
- `should_retry()` — the conditional edge function: returns `"deliver"` on pass or max retries, `"plan"` to retry
- `set_stage()` / `get_stage()` / `clear_stage()` — thread-safe stage tracking dict for streaming status to Telegram
- `run_task()` — the main entry point called by handlers. Creates initial state dict, invokes the compiled graph, clears stage tracking on completion

**Why this way:** LangGraph provides a compile-time verified graph — invalid edges are caught before runtime. The singleton `agent_graph = build_graph()` is compiled once at import time. The `_wrap_node` pattern cleanly separates stage tracking and timing from business logic.

---

## brain/nodes/ — Pipeline Stage Implementations

### `brain/nodes/__init__.py` (0 lines)
Package marker.

### `brain/nodes/classifier.py` (90 lines, 3.5 KB)
**Purpose:** Routes incoming tasks to 1 of 7 task types.

**What it does:**
- **Fast path:** Checks project triggers first via `match_project()` — zero API cost
- **Slow path:** If no trigger match, calls Claude (via model router, v8) to classify the task
- Parses Claude's JSON response (`{"task_type": "...", "reason": "..."}`)
- Fallback: if JSON parsing fails, scans response text for type keywords in priority order
- Safety net: if Claude says "project" but no trigger matches, falls back to "code" to avoid guaranteed failure loop

**Task types:** `project`, `frontend`, `ui_design`, `automation`, `data`, `file`, `code`

**Why this way:** The two-tier approach (triggers first, then LLM) saves $0.01-0.03 per project task. The `_FALLBACK_ORDER` list is imported by tests to stay in sync. The classifier uses `route_and_call(purpose="classify", complexity="low")` (v8) so low-complexity classification can route to Ollama.

### `brain/nodes/planner.py` (359 lines, 15 KB)
**Purpose:** Generates an execution plan based on task type and context.

**What it does:**
- Selects a task-type-specific system prompt (7 templates: PROJECT, CODE, DATA, FILE, AUTOMATION, UI_DESIGN, FRONTEND)
- Each prompt includes `CAPABILITIES_BLOCK` (internet, pip install, Ollama, filesystem, shell) and `TDD_INSTRUCTION` (assert statements)
- **Standards injection (v8):** Reads `.agentsutra/standards.md` and appends to system prompt for code-generating tasks (not project tasks). Truncated at 2000 chars.
- **Memory injection (v8):** For project tasks, queries `project_memory` table and injects "LESSONS LEARNED" from previous runs
- **Dynamic file injection (v8):** For project tasks with <50 source files, asks Claude to pick 3-5 relevant files and injects their content (3000 chars/file cap). Costs ~$0.02 per project task.
- Includes conversation context, file metadata, and retry feedback in the prompt
- Uses `route_and_call(purpose="plan", complexity=...)` (v8) — project plans route as "low" complexity

**Why this way:** Task-type-specific prompts outperform a single generic prompt because each type has different audit criteria and output expectations. The memory injection enables cross-task learning: if the same project failed last time because of a missing venv, the planner knows to activate it this time.

### `brain/nodes/executor.py` (604 lines, 23 KB)
**Purpose:** Generates code from the plan and executes it in a sandbox.

**What it does:**
- For **project tasks** (`_execute_project`):
  - Validates project config exists with a path and commands
  - Bootstraps dependencies from `requirements.txt`
  - Asks Claude to generate a shell script using ONLY the project's registered commands
  - Extracts parameters (`{client}`, `{file}`) from the user's message
  - Executes via `run_shell()` in the project directory with optional venv
  - Auto-install loop: if execution fails with ImportError, installs the missing module and retries (up to 5 times)
- For **code tasks**:
  - Asks Claude to generate Python/JS/Bash code from the plan
  - Executes via `run_code_with_auto_install()` with timeout estimation
  - Extracts declared artifacts from stdout
- Passes `task_id` to sandbox functions for live output streaming (v8)

**Why this way:** The executor is the most complex node because it handles two fundamentally different execution modes (project commands vs. generated code). The auto-install loop for projects prevents burning pipeline retries on missing dependencies. The `_estimate_timeout()` function scales timeout based on task type and data size.

### `brain/nodes/auditor.py` (274 lines, 10 KB)
**Purpose:** Cross-model adversarial quality review of execution output.

**What it does:**
- **Environment error short-circuit:** Checks for infrastructure failures (timeout, disk full, DNS failure, bad file descriptor) that code retries cannot fix. Forces skip to delivery.
- Selects task-type-specific audit criteria (7 sets matching the planner's task types)
- Sends the original task, plan, code, and execution result to Claude Opus for review
- Parses the JSON verdict (`{"verdict": "pass"/"fail", "feedback": "..."}`)
- Robust parsing: tries JSON first, then balanced-brace extraction, then keyword search
- Increments `retry_count` on failure for the conditional retry edge

**Environment error patterns (v8 additions):**
- `"Timed out after"` — from `run_shell()` format
- `"timed out after"` — from `run_code()` format (lowercase "t" in "Execution timed out")
- `"killed process group"` — process killed due to timeout

**Why this way:** Using a DIFFERENT model (Opus) than the executor (Sonnet) is the core safety mechanism. The same model approving its own work creates an echo chamber. The balanced-brace JSON extractor handles cases where Claude wraps JSON in explanation text. The environment error short-circuit saves 9 API calls on unrecoverable failures.

### `brain/nodes/deliverer.py` (327 lines, 12 KB)
**Purpose:** Formats the final response for delivery back to the user via Telegram.

**What it does:**
- Strips artifacts from failed tasks (they may be wrong or incomplete)
- Saves generated code as a `.py` file attachment for successful tasks
- Builds a detailed prompt with execution output, code description, file list, and failure context
- Calls Claude to generate a polished summary (under 1800 chars for Telegram)
- Falls back to a template-based response if Claude summary generation fails
- **Memory extraction (v8):** Stores success/failure patterns to `project_memory` table
- **Temporal mining (v8):** `_suggest_next_step()` queries task history for follow-up patterns. If the same follow-up has occurred 2+ times within 30 minutes, appends a suggestion.
- **Debug sidecar (v8):** `_write_debug_sidecar()` writes a `.debug.json` file with stage timings, verdict, retry count

**Critical rule in system prompt:** "If the status says FAILED, you MUST clearly state the task failed. NEVER fabricate success." — Added in v7 after discovering the deliverer told users "HTML file created successfully" when no file existed.

**Why this way:** The deliverer uses a separate Claude call to format results because raw execution output is often too technical or verbose for a Telegram message. The fallback response ensures delivery even if the formatting call fails. The "honest failure" constraint was learned from a real production bug.

---

## tools/ — Shared Utilities

### `tools/__init__.py` (0 lines)
Package marker.

### `tools/claude_client.py` (332 lines, 13 KB)
**Purpose:** Anthropic API wrapper with retry logic, cost tracking, and streaming support.

**What it does:**
- `call()` — main entry point for all Claude API calls. Handles:
  - Model selection and token limits
  - Extended thinking mode (streaming via `messages.stream()` when thinking enabled)
  - 128k max_tokens floor for thinking calls (prevents token budget exhaustion)
  - Exponential backoff retry (up to `API_MAX_RETRIES`) for rate limits and overload
  - Budget enforcement: checks daily/monthly spend before each call
  - Usage tracking: persists token counts to `api_usage` SQLite table
- `get_usage_summary()` / `get_cost_summary()` — query the usage table
- `_persist_usage()` — synchronous SQLite write (runs in same thread as pipeline)

**Why this way:** Centralising all Claude calls in one module ensures consistent retry handling, budget enforcement, and cost tracking. The streaming mode for thinking calls was added in v7 to fix Anthropic's 10-minute hard timeout on non-streaming requests. The synchronous usage persistence follows the same pattern as pipeline nodes (they run in `asyncio.to_thread()`).

### `tools/sandbox.py` (1034 lines, 37 KB)
**Purpose:** Code execution sandbox — the largest and most security-critical module.

**What it does:**
- **Live output registry (v8):** Thread-safe dict holding per-task stdout lines. `get_live_output(task_id, tail=3)` returns the last N lines for Telegram streaming. Bounded to 50 lines per task.
- **Tiered command safety:**
  - Tier 1 (`_BLOCKED_PATTERNS`, 39 patterns): Always blocked. `rm -rf /`, `sudo`, `curl|sh`, `chmod 777`, `mkfs`, `cat|bash`, fork bombs, etc.
  - Tier 3 (`_LOGGED_PATTERNS`, 12 patterns): Allowed but logged. `rm`, `chmod`, `git push`, `curl`, `python3 -c`, etc.
  - Tier 4 (`_CODE_BLOCKED_PATTERNS`, 8 patterns): Scans Python code content for credential reads, `os.system()`, `shutil.rmtree(/)`, reverse shells.
- **Credential stripping:** `_filter_env()` removes API keys, tokens, secrets from subprocess environment via exact match and substring matching
- **Docker execution:** `_run_code_docker()` executes code in an isolated container with only `workspace/` mounted read-write. Drops all capabilities, sets `no-new-privileges`, limits PIDs to 256.
- **Subprocess execution:** `run_code()` and `run_shell()` use threaded Popen reading (v8 refactor) for live stdout streaming. Manual timeout with `os.killpg()` for process group kill.
- **Auto-install:** `run_code_with_auto_install()` detects `ImportError`, installs the missing module, and retries (up to 2 attempts). Maps common import→pip mismatches (PIL→Pillow, cv2→opencv-python, etc.)
- **Artifact detection:** Snapshot-based mtime comparison before and after execution. Falls back to stdout path extraction. Sanity check filters excessive results (venv/package leak protection).

**Why this way:** The threaded Popen refactor (v8) replaced `proc.communicate(timeout)` to enable line-by-line stdout capture for live streaming. The tiered security model is defense-in-depth: Tier 1 is the hard block, Tier 3 provides audit trail, Tier 4 is defense-in-depth for the subprocess path. Docker provides the actual filesystem isolation boundary.

### `tools/model_router.py` (160 lines, 5.4 KB) — NEW in v8
**Purpose:** Routes LLM calls to Claude or Ollama based on purpose, complexity, RAM, and budget.

**What it does:**
- `route_and_call()` — drop-in replacement for `claude_client.call()` with routing intelligence
- `_select_model()` — applies routing rules:
  - `purpose="audit"` → always Claude Opus (non-negotiable cross-model invariant)
  - `purpose="code_gen"` → always Claude Sonnet (quality-critical)
  - `purpose="classify"/"plan"` + `complexity="low"` → Ollama if available + RAM < 75%
  - Budget escalation: if daily spend > 70% of `DAILY_BUDGET_USD` → Ollama
  - Default: Claude Sonnet
- `_ollama_available()` — 2-second timeout check against Ollama API
- `_ram_below_threshold()` — uses psutil to check memory usage
- `_get_today_spend()` — queries `api_usage` table with UTC midnight cutoff
- `_call_ollama()` — calls Ollama's `/api/generate` endpoint with 60s timeout

**Why this way:** The router is a pre-step, not a replacement — it delegates to `claude_client.call()` for Claude calls and handles Ollama directly. Audit NEVER routes to Ollama because cross-model adversarial review is a core safety invariant. The Ollama fallback on failure ensures no silent degradation.

### `tools/file_manager.py` (154 lines, 5.1 KB)
**Purpose:** File operations for uploads, metadata extraction, and content reading.

**What it does:**
- `save_upload()` — saves uploaded files with UUID prefix for deduplication
- `get_file_content()` — reads file content with configurable char limit truncation
- `get_file_metadata()` — extracts structured metadata from data files (CSV, Excel, JSON, Parquet): columns, row counts, size, sample rows
- `format_file_metadata_for_prompt()` — formats metadata for injection into planner prompts

**Why this way:** Metadata-only extraction for large data files prevents loading gigabytes into Claude's context. The planner sees column names and sample rows, then writes code to process the full file locally.

### `tools/projects.py` (100 lines, 3.2 KB)
**Purpose:** Project registry loader and trigger matcher.

**What it does:**
- `load_projects()` — reads `projects.yaml` and returns the project dict
- `get_projects()` — cached accessor
- `match_project()` — matches user messages against project triggers via keyword substring matching
- `get_project_context()` — formats project info (name, path, commands, timeout) for Claude prompts
- `get_all_projects_summary()` — brief summary of all projects for classifier context

**Why this way:** Keyword trigger matching is the "fast path" in the classifier — it avoids a Claude API call entirely for project tasks. The trigger system is intentionally simple (substring match) because false positives are harmless (the planner will handle the actual command selection).

---

## storage/ — Persistence Layer

### `storage/__init__.py` (0 lines)
Package marker.

### `storage/db.py` (369 lines, 14 KB)
**Purpose:** SQLite database operations — async for bot handlers, synchronous for pipeline nodes.

**Tables:**
| Table | Purpose |
|-------|---------|
| `tasks` | Task records with status, result, timing |
| `conversation_context` | Key-value context per user |
| `conversation_history` | Message log per user (for context injection) |
| `project_memory` (v8) | Success/failure patterns per project |

**What it does:**
- `init_db()` — creates all tables, enables WAL mode for concurrent write safety, creates indexes
- `create_task()` / `update_task()` / `get_task()` / `list_tasks()` — task CRUD
- `set_context()` / `get_context()` / `get_all_context()` / `clear_context()` — conversation key-value store
- `add_history()` / `get_recent_history()` / `build_conversation_context()` — message history for context injection
- `sync_write_project_memory()` / `sync_query_project_memories()` (v8) — synchronous SQLite helpers with `threading.Lock` for pipeline nodes that run in `asyncio.to_thread()`
- `recover_stale_tasks()` — marks orphaned "running"/"pending" tasks as "crashed" on startup
- `prune_old_data()` — removes old history, usage records, and completed tasks (configurable retention)
- `cleanup_workspace_files()` — removes output/upload files older than 7 days

**Why two database patterns:** Bot handlers run in the async event loop and use `aiosqlite`. Pipeline nodes run synchronously in `asyncio.to_thread()` and cannot use aiosqlite — they use synchronous `sqlite3` with `threading.Lock`, matching the pattern established by `claude_client._persist_usage()`.

### `storage/agentsutra.db` (SQLite file)
**Purpose:** The live SQLite database. Auto-created by `init_db()`. WAL mode enabled.

### `storage/scheduler.db` (SQLite file)
**Purpose:** APScheduler job store. Persists scheduled jobs across reboots.

---

## scheduler/ — Task Scheduling

### `scheduler/__init__.py` (0 lines)
Package marker.

### `scheduler/cron.py` (66 lines, 2.2 KB)
**Purpose:** APScheduler integration with SQLite persistence.

**What it does:**
- `start_scheduler()` — initialises `BackgroundScheduler` with SQLite job store
- `add_interval_job()` — adds recurring tasks (e.g., `/schedule 1440 Daily briefing`)
- `list_jobs()` / `remove_job()` — job management

**Why APScheduler over Celery:** Celery requires Redis/RabbitMQ infrastructure. APScheduler with SQLite persistence gives cron-like scheduling with zero external dependencies. Appropriate for a single-machine system running 5-30 tasks/day.

---

## tests/ — Test Suite

527 tests across 18 files. 527 pass, 11 skip (require Docker Desktop).

### Existing test files (pre-v8):

| File | Tests | Coverage |
|------|-------|----------|
| `test_sandbox.py` | 174 | Blocked patterns (30), allowed commands (13), working dir (4), pip mapping (6), import parsing (7), interpreter blocking (7), find blocking (5), encoding bypass (3), home move blocking (4), dotfile protection (9), env filtering (6), traceback extraction (4), pipe-to-shell (6), eval (2), bash string splitting (4), code scanner (14), file detection (6), artifact filter (26), walk artifacts (6), artifact sanity check (3), stdout fallback (7), stdin devnull (2) |
| `test_executor.py` | 34 | Markdown extraction (10), timeout estimation (4), param extraction (4), import error parsing (12), dependency bootstrapping (4) |
| `test_docker_sandbox.py` | 28 | Docker availability (8), command building (7), routing (3), execution integration (6), security integration (4) |
| `test_handlers.py` | 27 | Auth (4), resource guards (5), message splitting (5), file upload (4), artifact delivery (2), scheduled timeout (1), error sanitisation (5), rate limit retry (1) |
| `test_auditor.py` | 22 | JSON extraction (12), environment error detection (10) |
| `test_budget.py` | 13 | Budget enforcement (2), model costs (3), thinking tokens (5), API retries (3) |
| `test_file_manager.py` | 12 | CSV metadata, empty files, truncation, UUID uploads |
| `test_db.py` | 8 | Prune epoch handling (3), crash recovery (3), task pruning (2) |
| `test_e2e_artifact_delivery.py` | 8 | Realistic project execution with artifact detection |
| `test_classifier.py` | 5 | Fallback ordering (imports `_FALLBACK_ORDER` from source) |
| `test_pipeline_integration.py` | 5 | Full pipeline with mocked Claude API |
| `test_claude_client.py` | 4 | API client behaviour |

### v8 test files:

| File | Tests | Coverage |
|------|-------|----------|
| `test_v8_foundation.py` | 17 | Timeout detection (7), blocklist audit (6), coding standards injection (4) |
| `test_v8_context.py` | 13 | Project memory DB operations (4), deliverer memory extraction (3), planner memory injection (2), dynamic file injection (4) |
| `test_v8_routing.py` | 12 | Model router selection (8), route-and-call integration (1), temporal sequence mining (3) |
| `test_v8_ux.py` | 11 | Live output registry (4), hash-gated edits (3), debug sidecar (2), stage timing collection (2) |
| `test_stress_v8.py` | 64 | Adversarial stress round 1: code scanner evasion (15), shell blocklist evasion (10), path traversal (6), concurrency/deadlock (2), output registry (3), privacy (2), memory poisoning (3), dedup/redundancy (3), magic numbers (4), Ollama fallback (3), routing invariants (5), budget escalation (4), edge cases (4) |
| `test_stress_v8_audit2.py` | 80 | Adversarial stress round 2: 81 tests across expanded security boundary, concurrency contention, logic saturation, and resource routing scenarios |

**Why so many sandbox tests:** The sandbox is the most security-critical module. Every blocked pattern has a test. Every allowed pattern has a test. Every edge case (split flags, long flags, mixed case) is covered. This prevents regressions when adding new patterns.

---

## scripts/ — Deployment & Maintenance

### `scripts/build_sandbox.sh` (48 lines, 1.5 KB)
**Purpose:** Builds the Docker sandbox image.

**What it does:** Validates Docker daemon is running, builds the image from `Dockerfile`, sets up pip cache directory. Run once during setup, then again when updating the Dockerfile.

### `scripts/secure_deploy.sh` (58 lines, 1.8 KB)
**Purpose:** Hardens file permissions for production.

**What it does:** Sets `.env` and `projects.yaml` to read-only (600), ensures workspace directories exist with correct permissions, installs a daily backup cron job with 7-day retention.

### `scripts/monthly_maintenance.sh` (35 lines, 1.1 KB)
**Purpose:** Monthly disk space recovery.

**What it does:** VACUUMs SQLite databases (recovers freed pages), cleans Docker pip cache (removes packages unused 30+ days), runs Docker system prune. Designed to be run via cron or manually.

---

## workspace/ — Runtime Artifacts

```
workspace/
├── uploads/         # Files received from Telegram (UUID-prefixed)
├── outputs/         # Generated code, charts, reports, debug sidecars
│   ├── *.py         # Generated Python scripts
│   ├── *.html       # Generated web pages
│   ├── *.debug.json # Per-task debug sidecars (v8)
│   └── ...
├── projects/        # Persistent project subdirectories
└── .pip-cache/      # Shared pip cache for Docker containers
```

**Why this structure:** Separating uploads from outputs prevents user files from being confused with generated artifacts. The `.pip-cache` directory is mounted into Docker containers so `pip install` doesn't re-download packages between runs. The `outputs/` directory is cleaned by `cleanup_workspace_files()` (default: 7 days).

---

## .agentsutra/ — Project Standards

### `.agentsutra/standards.md` (15 lines, 495 B) — NEW in v8
**Purpose:** Coding standards injected into planner prompts for code-generating tasks.

**Content:** Python standards (pathlib, logging, type hints, no bare except, assert, f-strings, with statements) and Shell standards (set -euo pipefail, quote variables, absolute paths).

**Why this way:** Externalised from planner.py so users can edit their standards without modifying source code. Only injected for code-generating task types (not project tasks — they run existing commands). Truncated at 2000 chars to avoid ballooning the system prompt.

---

## Configuration & Environment

### `.env` (not committed)
**Purpose:** Live environment variables. Contains API keys, bot token, user IDs. Never committed to git.

### `.env.example` (42 lines, 1.3 KB)
**Purpose:** Template showing all available environment variables with comments.

### `.gitignore` (60 lines, 1.2 KB)
**Purpose:** Git exclusion rules.

**Excludes:** `.env`, `*.db`, `__pycache__/`, `*.pyc`, `agentsutra.log`, `workspace/uploads/`, `workspace/outputs/`, `projects.yaml` (contains local paths), `.DS_Store`, `venv/`, `*.egg-info/`.

---

## Docker Files

### `Dockerfile` (46 lines, 1.5 KB)
**Purpose:** Sandbox container image based on `python:3.11-slim`.

**What it installs:**
- System: `gcc`, `g++`, `libffi-dev` (for compiled packages)
- Python: `pandas`, `numpy`, `matplotlib`, `seaborn`, `openpyxl`, `requests`, `beautifulsoup4`, `duckdb`, `Pillow`, `pyyaml`

**Why pre-install:** Auto-installing packages at runtime is slow and unreliable. Pre-installing the most common data science stack in the image eliminates 90% of auto-install triggers.

### `.dockerignore` (11 lines, 300 B)
**Purpose:** Excludes `.env`, databases, workspace, and cache from Docker build context to minimise image size and prevent secret leakage.

---

## Documentation Files

### `README.md` (349 lines, 15 KB)
**Purpose:** Project overview, quick start guide, architecture summary, capabilities showcase. The public-facing document.

### `AGENTSUTRA.md` (2055+ lines, 136 KB)
**Purpose:** Comprehensive technical documentation. Architecture deep-dive, every file explained, security threat model, deployment guides (launchd/systemd), model provider swapping, design philosophy, benchmarks, and full changelog from v1 through v8.

### `USECASES.md` (654 lines, 28 KB)
**Purpose:** Capabilities guide with 50+ real-world examples across all 7 task types. Demonstrates what AgentSutra can actually do.

### `CODEBASE_REFERENCE.md` (this file)
**Purpose:** Complete inventory of every file, folder, and configuration with rationale.

---

## External Reference Files

Located at `/Users/confusemouse/Desktop/prompt_instructions/` (not in git):

| File | Purpose |
|------|---------|
| `prompt-suite-v2.md` (1103 lines) | The v8 implementation prompt — 4 phases with exact code, schemas, tests |
| `AgentSutra_Roadmap_v2_1.md` (906 lines) | Feature roadmap with architectural analysis |
| `context_aware_pipeline_v2_1.mermaid` | Mermaid diagram: context-aware pipeline flow |
| `resource_management_flow.mermaid` | Mermaid diagram: model routing decision tree |
| `chain_execution_strict_and.mermaid` | Mermaid diagram: strict-AND chain execution |

---

## Architecture Decisions

### Why LangGraph, not a custom loop?
LangGraph provides compile-time graph validation, typed state management, and conditional edges. A custom loop would require reimplementing state merging, retry logic, and graph validation. The overhead is minimal (one dependency) and the payoff is significant (verified pipeline structure).

### Why SQLite, not PostgreSQL?
Single user on one machine. SQLite handles thousands of writes/second with WAL mode. No connection pooling needed. Connection-per-call with 20s timeout is simpler and correct for this workload.

### Why synchronous pipeline in asyncio.to_thread()?
The bot handles 5-30 tasks/day. Each pipeline run occupies one thread for 15-60 seconds. With `MAX_CONCURRENT_TASKS=3`, this uses 3 threads at peak. An async pipeline would add complexity for no measurable benefit. The `to_thread()` approach keeps the async event loop responsive while the pipeline blocks on Claude API calls.

### Why two models (Sonnet + Opus)?
The same model approving its own work creates an echo chamber. Sonnet generates code. Opus reviews it. Different model families have different failure modes and blind spots. This is the single most important safety mechanism in the system.

### Why Ollama routing is optional, not default?
Ollama quality varies by model and task. The router only sends low-complexity tasks (classification, project planning) to Ollama when it's available and RAM allows. High-complexity tasks and all code generation stay on Claude. Audit always stays on Opus. This preserves quality while reducing costs for routine tasks.

### Why the blocklist is Tier 1 + Tier 3, not a capability sandbox?
The threat model is LLM hallucination, not adversarial users. The blocklist catches accidental destructive commands (`rm -rf ~`). Docker isolates filesystem access. Opus audits output quality. These three layers are sufficient for a system where the only user is the owner. A capability-based sandbox (seccomp, AppArmor) would be over-engineering.

### Why project memory uses synchronous sqlite3, not aiosqlite?
Pipeline nodes run synchronously inside `asyncio.to_thread()`. They cannot use `async/await`. The synchronous sqlite3 + `threading.Lock` pattern matches what `claude_client._persist_usage()` already established. Consistency > cleverness.

### Why the .agentsutra/standards.md file keeps disappearing
The file is in `.agentsutra/` which may be cleaned by macOS or other tooling. It's committed to git and recreated if missing. The planner gracefully skips injection if the file doesn't exist (no error).
