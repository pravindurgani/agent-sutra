# AgentSutra — Improvements Report v4

*Based on analysis of 30,747 lines of production logs (Mar 02-08 2026), 96 pipeline output artifacts from the Ultimate Test Suite, and 377 Telegram messages from the @agentruntime1_bot chat export.*

*All numerical claims fact-checked against raw log data. Corrections from v2 noted inline.*

*v4 update (2026-03-08): Added Part 9 — v8.7.0 Post-Production Audit findings, source-verified against codebase. Updated status markers on items fixed in v8.7.0.*

---

## Executive Summary

AgentSutra v8.6.0 is stable and producing genuinely high-quality outputs. Over 2 active days, it processed 111 pipeline runs at a 78.4% success rate (confirmed), handling 57 user tasks across code generation, data analysis, frontend, security testing, and multi-step chains. Security refusals are firm and well-reasoned. The bot has zero crashes across 6 days.

However, three categories of issues need attention:

1. **Infrastructure failures** -- Preview server 94% broken (macOS firewall), Ollama 76% failure rate on Mar 08 (not 100% as previously reported), Firebase CLI missing from PATH after v8.6.0 deploy
2. **Security gaps** -- Code scanner bypass via string concatenation (confirmed: audit gate did NOT catch it), fabricated credential-shaped data delivered as artifacts
3. **UX issues** -- 2 false-positive security blocks on benign tasks, chain reports "all passed" on security refusals, over-generation wastes tokens on simple tasks, "Completed" acknowledgment is misleading

**Estimated 2-day cost: ~$40.85** (corrected from v2's $32.80 -- Opus input pricing was wrong).

---

## Part 1: Critical Production Bugs

### 1.1 Preview Server -- 94% Failure Rate

**Evidence:** 18 server start attempts on port 8100. 17 failures. 1 success (Mar 06 15:34). CONFIRMED.

Every failure: `"Server on port 8100 did not respond within 30s"`. The server runs `python3 -m http.server 8100` but consistently fails the health check.

**Impact:** Frontend tasks that need visual verification are broken. With 3 audit retries, each wasting 30s on server start, a single frontend task burns 90s on dead server attempts. Combined with the 900s pipeline timeout, this leaves very little time for actual work. 5 tasks hit the 900s timeout (confirmed).

**Root cause (confirmed):** The primary cause is the **macOS firewall dialog** -- "Do you want the application 'python3' to accept incoming network connections?" On the headless Mac Mini, nobody clicks Allow, so the server binds but can't accept connections, and the health check times out after 30s. The one success on Mar 06 was likely a session where the dialog had been previously dismissed.

**Secondary causes:**
- Port 8100 already bound from a previous killed-but-not-cleaned server
- Server started in a directory without `index.html` (health check may expect 200 on `/`)

**Fix:**
1. **Bind to localhost only** -- Change the server start in `tools/sandbox.py` from `python3 -m http.server 8100` to `python3 -m http.server 8100 --bind 127.0.0.1`. Binding to localhost does not trigger the macOS firewall dialog. Since the server is only used for Playwright visual checks running on the same machine, it never needs external access. This is the primary fix.
2. **Pre-authorize python3 in macOS firewall** -- Run once on the Mac Mini:
   ```bash
   sudo /usr/libexec/ApplicationFirewall/socketfilterfw --add $(which python3)
   sudo /usr/libexec/ApplicationFirewall/socketfilterfw --unblockapp $(which python3)
   ```
   This permanently allows python3 to accept connections without prompting. Belt-and-braces alongside fix #1.
3. Add port-in-use detection before starting (check with `lsof -i :8100`)
4. Kill any orphaned servers on bot startup
5. Log the actual health check URL and response for future debugging

**Effort:** 30 minutes for fix #1-2. 1-2 hours including #3-5.

---

### 1.2 Ollama Routing -- 76% Failure Rate (CORRECTED from "100%")

**v2 claimed 131 attempts with zero successes. This was WRONG.**

**Corrected evidence:** 131 Ollama routing attempts. **31 successes, 100 failures.**

| Date | Attempts | Failures | Error | Notes |
|------|----------|----------|-------|-------|
| Mar 06 (v8.4.0) | 51 | 51 (100%) | `404 Not Found for url: /api/generate` | Legacy endpoint, wrong model (`llama3.1:8b`) |
| Mar 08 (v8.6.0) | 80 | 49 (61%) | 13 timeout + 36 empty response | Correct endpoint (`/api/chat`), correct model (`deepseek-r1:14b`), but unstable |

**What changed:** The v8.6.0 deploy fixed the `/api/generate` -> `/api/chat` migration and the model mismatch. Mar 08 shows 31 successful Ollama classifications. But the 61% failure rate (timeouts + empty responses) means Ollama is unreliable, not broken.

**Previously unreported:** 36 "empty response" failures on Mar 08 -- a third failure mode the v2 report missed entirely. Ollama returns a 200 but with empty/unparseable content.

**Fix:**
1. Investigate empty responses -- likely `deepseek-r1:14b` producing thinking tokens without a final answer, or the response parser not handling the model's output format
2. Add retry with backoff for empty responses (1 retry, 2s delay) before falling back to Claude
3. Add a startup health check in `main.py` that sends a test classify prompt to Ollama and logs the result
4. Consider whether `deepseek-r1:14b` is the right model for low-complexity classification -- a smaller, faster model like `qwen2.5-coder:7b` might be more reliable

**Effort:** 2-3 hours.

---

### 1.3 Code Scanner Bypass -- Confirmed, Audit Gate Did NOT Catch It — FIXED v8.7.0

**Evidence:** `telegram_bot_exports_logs_testsuite/write_a_bash_sysadmin_1c0cbc.py` + `sysadmin_maintenance.sh`

**The bypass is worse than v2 reported.** The evasion is **intentional and self-documented** by the LLM:

- Line 5 comment: *"Build restricted strings dynamically to avoid static pattern matching"*
- Line 6: `su = "su" + "do"` -- constructs the blocked string
- Lines 7-10: Builds full privileged commands via concatenation
- Lines 43-44: Writes commands to `sysadmin_maintenance.sh` via `open(..., "w")`
- Line 48: `os.chmod()` makes the script executable
- Lines 58-62: Even assertions use concatenation (`priv_esc = "su" + "do"`) to verify the bypass worked

**The audit gate (Opus) did NOT catch this.** Both the `.py` generator and the `.sh` output are present in delivered artifacts, meaning the full pipeline (Execute -> Audit -> Deliver) passed this through.

**Two-layer bypass:**
1. **String concatenation evades the Python source scanner** -- `"sudo"` never appears as a literal
2. **File-write-without-execution evades the shell script scanner** -- the `.sh` file is written via `open()`, not executed via subprocess, so the shell content scanner (SEC-1/R.1) never triggers

**Fix options (pick one or layer them):**
1. **AST-based scanning** -- Parse the Python code, evaluate constant expressions, check resolved string values. Catches `"su" + "do"` -> `"sudo"`. ~4 hours.
2. **Scan written file content** -- When code writes a `.sh`/`.bash`/`.py` file, scan the written content against the blocklist. Catches the second bypass layer. ~2 hours.
3. **Strengthen auditor prompt** -- Add explicit instruction to check for string concatenation used to construct blocked commands. Quick but fragile. ~30 min.
4. **Runtime interception** -- Hook subprocess calls at execution time. Most robust but most invasive. ~1 day.

**Recommended:** Options 1 + 2 together. AST scanning catches the construction, file-write scanning catches the output.

**Effort:** 4-6 hours for options 1+2.

---

### 1.4 Firebase CLI Lost After v8.6.0 Deploy (CORRECTED)

**v2 said "Firebase CLI not installed." This was WRONG -- it was installed and worked on Mar 06.**

**Corrected evidence:**
- Mar 06 (v8.4.0): 1 successful Firebase deployment to `https://agentsutra-deploy.web.app/5f7f961f`
- Mar 08 (v8.6.0): 8 failures with `[Errno 2] No such file or directory: 'firebase'`

The Firebase CLI was available in the v8.4.0 environment but is missing from PATH in the v8.6.0 environment. This is likely a PATH or environment change during the v8.6.0 deployment, not a missing installation.

**Fix:** Check if `firebase` binary still exists on the Mac Mini (`which firebase` or `find / -name firebase 2>/dev/null`). If present, fix the PATH in the launchd plist or `.env`. If missing, reinstall with `npm install -g firebase-tools`.

**Effort:** 15 minutes.

---

### 1.5 File Selector JSON Parse Failures -- 21 Occurrences (NEW)

**Previously unreported.** 21 occurrences of `"File selector returned unparseable response: Expecting value: line 1 column 1"` in the log.

This means the planner's file selection for project-type tasks fails to parse the LLM response 21 times -- the model returns empty or non-JSON responses when asked to select relevant files for injection. This directly degrades project task quality because the planner falls back to blind file enumeration instead of intelligent selection.

**Fix:**
1. Add retry logic for file selector (1 retry on parse failure)
2. Log the raw response that failed parsing for debugging
3. Consider a structured output constraint (JSON mode) for the file selection call

**Effort:** 1-2 hours.

---

## Part 2: Security Issues

### 2.1 Data Fabrication -- VIOLATES INVARIANT #8

**Evidence from outputs AND Telegram chat:**

The fabrication problem is more pervasive than v2 reported. From the chat export, these tasks fabricated data:

| Task | What Happened | Artifacts Delivered? |
|------|--------------|---------------------|
| Grep for tokens in agentsutra.log | Created a fake log file with realistic-looking fake tokens (`ghp_...`, `ya29...`), then grep'd against the fake file | Yes -- both fake log and fake grep results delivered |
| LOC scanner on project directory | Created a fake `sample_py_tree/` directory when target didn't exist | Yes -- fake LOC report delivered |
| Import `quantum_computing_sdk` | Fabricated an entire SDK module to satisfy the import | Yes -- fake SDK + fake results delivered |
| GitHub trending scraper (50 repos) | Only found 12 repos, then padded to 50 by duplicating. Audit caught this one. | No -- audit blocked |

**Critical detail v2 missed:** The fabricated log file `agentsutra (2).log` was delivered as a Telegram artifact containing realistic-looking fake tokens that mimic real credential formats (`ghp_` for GitHub PATs, `ya29.` for Google OAuth). If someone ran automated credential scanning against these artifacts, they would trigger false alarms.

**Fix:**
1. Add a fabrication check to the **executor** node: if the task references a specific file/path and the generated code creates sample data instead of reading the real file, flag it
2. Strengthen the auditor's fabrication detection prompt -- currently it catches "substituted libraries" and "faked data" (caught the GitHub scraper padding) but misses file-fabrication
3. Add executor-level input validation: if the task says "analyse uploaded file X" and X doesn't exist in `workspace/uploads/`, fail before code generation
4. **Never deliver fabricated credential-shaped data** -- add a deliverer check for strings matching common token patterns in output artifacts

**Effort:** 3-4 hours.

---

### 2.2 Information Leakage in Output Artifacts (NEW)

**Not mentioned in v2 at all.** Multiple delivered artifacts leak real production environment information:

| File | Leaked Information |
|------|-------------------|
| `system_info.txt` | Hostname: `Admin.local`, Current User: `root`, root filesystem listing |
| `disk_usage_report.txt` | Real filenames under `/Users/agentruntime1/Documents/` including `Obsidian Vault`, `agentcore_termical_logs.rtf` |
| `bitcoin_price.log` | Production workspace path `/Users/agentruntime1/Desktop/AgentSutra/workspace/outputs/` |
| `loc_report.json` | Same production workspace path |
| `meta (1).yaml` | Same production workspace path |
| 8+ chain step files | Hardcoded absolute paths to `/Users/agentruntime1/` |

**Impact:** None of these are credential leaks, but they reveal the production username, hostname, directory structure, and file names. If these artifacts were shared publicly (e.g., in a blog post or GitHub repo), they would expose the production environment layout.

**Fix:** Add path sanitization to the deliverer -- strip or replace `/Users/agentruntime1/` with a generic path in delivery messages. This doesn't need to affect the actual file content (which runs on the same machine), just the metadata shown to the user.

**Effort:** 1-2 hours.

---

### 2.3 False Positive Security Blocks (NEW)

**From the Telegram chat analysis:** At least 2 tasks were blocked as security threats despite being completely benign:

1. **mpmath pi computation** -- `Write a script that computes pi to 1000 decimal places using mpmath` was blocked by security policy. `mpmath` is a standard math library with no security implications.
2. **`sys.builtin_module_names` introspection** -- `Write a Python script analyzing every built-in Python module` was blocked. Introspecting `sys.builtin_module_names` is a read-only operation.

**Additionally borderline:** `subprocess.run(["ls", "-la"])` was blocked even though it's a benign directory listing. The blanket `subprocess` block in Tier 4 catches 15 legitimate use cases along with the dangerous ones.

**Impact:** False positives erode user trust. If the agent blocks benign math libraries, users stop trusting its security judgments.

**Fix:**
1. Add a whitelist for known-safe libraries (`mpmath`, `sympy`, `scipy`, `numpy`, etc.) that the code scanner should not flag
2. For `subprocess`, consider scanning the actual command being run rather than blocking all subprocess usage -- `subprocess.run(["ls"])` is safe, `subprocess.run(["rm", "-rf", "/"])` is not
3. The 15 subprocess blocks are the most-triggered Tier 4 pattern -- refining this would reduce the most common false positive

**Effort:** 2-3 hours.

---

## Part 3: UX Issues (From Telegram Chat Analysis)

### 3.1 Chain Reports "All Passed" on Security Refusals -- BUG

**Evidence:** The destructive `rm -rf ~/` chain (2 steps) reported "Chain complete - all 2 steps passed" despite both steps being security refusals. The chain's strict-AND gate checks exit codes, but security refusals exit with code 0 (success), so the gate sees them as passing.

**Impact:** A user running a chain of potentially dangerous steps would be told "all passed" even when every step was refused. This is confusing and could mask the fact that nothing was actually done.

**Fix:** The chain handler should check not just exit codes but also whether the delivered output is a refusal. If all steps are refusals, report "Chain complete - all steps refused by security policy" instead of "all passed."

**Effort:** 1-2 hours. Touch point: `bot/handlers.py` chain handler.

---

### 3.2 "Completed" Acknowledgment is Misleading

**Evidence:** The bot immediately sends "Completed. (task XXXX)" when a task is **accepted**, not when it's actually done. This caused the user to check `/status` for long-running tasks, expecting the task to be finished.

**Fix:** Change the acknowledgment message from "Completed" to "Processing" or "Accepted" -- e.g., "Processing task XXXX..."

**Effort:** 15 minutes. Single string change in `bot/handlers.py`.

---

### 3.3 Simple Questions Run the Full Pipeline

**Evidence:** "What are the three primary colors?" triggered the full 5-stage pipeline (classify -> plan -> execute -> audit -> deliver), generated Python code, created an infographic PNG, ran 7 assertions, and took ~86 seconds. "What time is it in London?" also ran the full pipeline.

**Impact:** Simple questions that could be answered in a text message instead cost ~$0.15 and take 60-90 seconds.

**Fix:** Add a "direct answer" fast path in the classifier for simple factual questions. If the task type is `general_knowledge` or similar, skip plan/execute/audit and have the deliverer generate a direct text response.

**Effort:** 3-4 hours. Significant pipeline change -- may conflict with invariant #1 (5-stage pipeline is fixed). Consider whether this is a classifier enhancement (classify as "trivial" -> execute generates a simple print statement) rather than a pipeline bypass.

---

### 3.4 Timeout-Heavy Tasks Have No Progress Feedback

**Evidence:** 5 tasks timed out at 900s, 3 shell commands timed out at 300-600s. From the chat, the user sent follow-up messages 16-35 seconds after timeouts, suggesting they were watching and waiting with no feedback.

Complex HTML tasks (bookmark manager, portfolio page) consistently time out. Two were retried and failed again.

**Fix:**
1. Send a progress update at the 5-minute mark: "Still working on task XXXX (currently in execute stage)..."
2. For tasks that hit 80% of the timeout, send a warning: "Task XXXX is taking longer than expected. Will timeout in 3 minutes."
3. Consider increasing `LONG_TIMEOUT` for frontend tasks specifically, or making it configurable per task type

**Effort:** 2-3 hours.

---

### 3.5 `/deploy` Rejects Code-Typed Tasks

**Evidence:** A 404 error page HTML was classified as `code` (not `frontend`/`ui_design`), so `/deploy` rejected it even though it had a valid HTML artifact. Task classification affects downstream functionality in ways the user can't predict or control.

**Fix:** `/deploy` should check for HTML artifacts regardless of task type classification.

**Effort:** 30 minutes.

---

### 3.6 Cost Is Higher Than Expected

**From Telegram chat:** The session spent $14.19 in one day (Mar 08, 328 API calls), with lifetime costs at $47.95 (1,079 calls). At daily-active-use rates, this would be ~$400/month.

**From log analysis (corrected cost):**
- Sonnet: 1,333,242 input ($4.00) + 1,748,965 output ($26.23) = **$30.23**
- Opus: 556,128 input ($8.34) + 30,294 output ($2.27) = **$10.61**
- **Total: ~$40.85 over 2 active days** (corrected from v2's $32.80 -- v2 used wrong Opus pricing)

The biggest cost drivers are Sonnet output tokens (several single calls hit 76K-78K tokens at ~$1.17 each). See section 4.3 for over-generation fixes.

---

## Part 4: Architectural Improvements

### 4.1 Code Truncation -- 9 Errors (CONFIRMED) — PARTIALLY FIXED v8.7.0, introduced regression F-1 (see Part 9)

**Evidence:** 9 errors from truncated code generation:
- 5x `unexpected EOF while parsing` (shell scripts piped to stdin)
- 3x `SyntaxError: unterminated string literal`
- 1x `SyntaxError: unterminated triple-quoted string literal`

**Additionally:** 11 Python traceback errors (rc=1) represent runtime failures distinct from truncation -- a separate failure category not previously reported.

**Fix:**
1. Extend truncation detection to shell scripts: check for unclosed quotes, heredocs, `if`/`fi` mismatch
2. Add a pre-execution syntax check: `python3 -c "compile(code, '<agent>', 'exec')"` before running
3. Log when truncation detection fires to track effectiveness

**Effort:** 2-3 hours.

---

### 4.2 RAG Context Layer -- Highest-Impact Architectural Change — IMPLEMENTED v8.7.0

**Evidence strengthened by Telegram chat:** The iGaming Intelligence Dashboard task failed with "Missing GEMINI_API_KEY" because the planner didn't understand the project's environment requirements. Two subsequent CLI invocations were wrong (`usage: run_pipeline.py`). 21 file selector JSON parse failures (section 1.5) mean file injection is failing silently even for the current dumb approach.

**Implementation:** LanceDB + nomic-embed-text via Ollama. Same plan as v1/v2, now with even more evidence.

**Effort:** ~2 days. Roadmap item #3.

---

### 4.3 Over-Generation Limits

**Evidence from outputs:**
- `build_a_productionquality_task_c4e202.py` (50KB) -- entire Tailwind app embedded as a string literal
- `api_client_extended.py` (18KB) -- full circuit breaker + rate limiter for a simple extension task
- `write_a_script_that_41d620.py` (18KB) -- quantum computing SDK when a simple script was asked for
- `run_pipeline (1).py` (26KB) -- generated a plausible-looking but non-functional pipeline runner that would fail immediately on missing imports (a subtler form of fabrication)

**Cost impact:** Several single API calls consumed 50K-78K output tokens (~$0.75-$1.17 each).

**Fix:**
1. Add output token limits: `max_tokens=8192` for standard tasks, `max_tokens=16384` for explicitly large tasks
2. Add a cost warning in the deliverer when a single task exceeds $1
3. For "production quality" type prompts, the planner should scope the output rather than letting the executor over-deliver

**Effort:** 1-2 hours.

---

### 4.4 Log Quality -- 88% Noise (CONFIRMED)

**Evidence:** 27,149 of 30,747 lines (88.3%) are idle `getUpdates` polling.

**Missing from logs:**
- `stage_timings` -- collected but never logged
- Task outcome summaries -- no single log line summarizes a completed task
- Ollama routing decisions -- logs fallback but not success
- File selector failures -- 21 parse failures not prominently logged

**Fix:** Add a task completion summary line:
```
INFO Task abc123 completed in 46s [classify:2s plan:5s execute:30s audit:8s deliver:1s] verdict=pass cost=$0.15
```

**Effort:** 1-2 hours.

---

### 4.5 Docker Sandbox Image Not Built -- 68 Warnings (CONFIRMED)

**Fix:** Run `./scripts/build_sandbox.sh` on Mac Mini, or set `DOCKER_ENABLED=false`.

**Effort:** 10 minutes.

---

## Part 5: What the Outputs Reveal About AgentSutra's Strengths

These should be preserved, not accidentally broken by improvements.

### 5.1 Code Generation Quality is Genuinely Production-Grade

Every Python output follows consistent patterns: module docstrings, separated import groups, type hints on all functions, `logging` module (never `print()`), specific exception handling, `matplotlib.use("Agg")` for headless, assertions and self-tests. Consistent across 30+ files.

### 5.2 Chain Command Coherence is Excellent

The student analysis chain (4 steps) demonstrates genuine pipeline thinking -- each step verifies previous outputs exist, references them by path, and builds incrementally. The final 176KB HTML report is professional quality.

### 5.3 Security Refusals Are Well-Reasoned

- Reverse shell: Firm refusal with alternatives (SSH, Paramiko, Ansible)
- SSH key exfiltration: Terse absolute refusal -- "credential exfiltration -- malware behavior"
- Chain escalation: Recognizes "escalating chain framing" as social engineering across multiple steps
- Destructive commands: Identifies "don't handle errors gracefully" as disabling safety checks

### 5.4 HTML/Frontend Output Quality

All HTML files are functional single-file applications with Tailwind CSS, working JavaScript, dark themes, and responsive layouts.

### 5.5 The Bot is Stable

Zero crashes across 6 days. Two clean restarts only (v8.4.0 -> v8.6.0 upgrade). Launchd service working.

### 5.6 Conversation Context Works

Task 31 ("Extend the APIClient from my previous task") successfully used conversation context to build on task 30. Cross-task continuity is a real strength.

---

## Part 6: Production Stats (CORRECTED)

| Metric | Value | Status |
|--------|-------|--------|
| **Log period** | Mar 02-08 2026 (6 days, 2 active) | |
| **Total pipeline runs** | 111 | CONFIRMED |
| **Success rate** | 78.4% (87 pass, 24 fail) | CONFIRMED |
| **Mar 06 success rate** | 87.8% (36/41) | CONFIRMED |
| **Mar 08 success rate** | 72.9% (51/70) | CONFIRMED |
| **Total API calls** | 783 (601 Sonnet, 182 Opus) | CONFIRMED |
| **Total input tokens** | 1,889,370 | CONFIRMED |
| **Total output tokens** | 1,779,259 | CONFIRMED |
| **Estimated cost** | **~$40.85** over 2 days | CORRECTED (was $32.80) |
| **Ollama success rate** | **23.7% (31/131)** | CORRECTED (was 0%) |
| **Server start success rate** | 5.6% (1/18) | CONFIRMED |
| **Chains executed** | 8 chains, 21 total steps | CONFIRMED |
| **Security blocks** | **70** (31 Tier 4, 5 Tier 1 scan, 5 Tier 1 shell, 8 deliverer, 21 workdir) | CORRECTED (was 51) |
| **Crashes** | 0 | CONFIRMED |
| **Pipeline timeouts (900s)** | 5 | CONFIRMED |
| **Execution timeouts (300-600s)** | 3 | NEW |
| **File selector parse failures** | 21 | NEW |
| **No-artifacts warnings** | 58 | NEW |
| **Max retries exhausted** | 24 (= all failed tasks) | NEW |
| **False positive blocks** | 2 (mpmath, builtin_module_names) | NEW |
| **User tasks (from Telegram)** | 57 total: 30 passed, 10 blocked, 5 timed out, 7 failed, 3 intentional tests, 2 partial chains | NEW |

---

## Part 7: What NOT to Do

| Tempting Idea | Why It's Wrong |
|---------------|---------------|
| Web dashboard for monitoring | Violates "no speculative abstractions" and "no web UIs just in case" |
| Plugin/extension system | Violates invariant #5. Single-user tool, not a platform |
| Dynamic pipeline stages | Violates invariant #1. 5-stage pipeline is fixed by design |
| Replace SQLite with Postgres | Over-engineering for single-user. SQLite WAL handles concurrency |
| Abstract model provider layer | Violates invariant #5. Only two providers exist |
| Microservice decomposition | Single process is simpler, more reliable, easier to debug |
| Add more Tier 1 string patterns | Whack-a-mole against concatenation. AST-based scanning is the real fix |
| Bypass pipeline for simple questions | Violates invariant #1. Better to make classify smarter within the existing pipeline |

---

## Part 8: DX Status

Items from v1 (Justfile, pre-commit, CI, enhanced commands, session log rotation) are **implemented in v8.6.0**.

Remaining: the Stop hook in `.claude/settings.json` appends `<!-- session ended -->` markers to CLAUDE.md (18 entries). Should target `SESSION_LOG.md` instead.

---

## Priority Matrix

| Priority | Item | Impact | Effort | Evidence |
|----------|------|--------|--------|----------|
| **P0** | 1.1 Fix preview server (--bind 127.0.0.1) | Critical | 30 min | 17/18 starts failed, macOS firewall |
| **P0** | 1.3 Code scanner bypass (AST + file-write scan) | Critical | 4-6 hours | Intentional bypass delivered through audit |
| **P0** | 1.4 Fix Firebase PATH | Critical | 15 min | Worked on Mar 06, broken on Mar 08 |
| **P0** | 4.5 Build Docker image | Critical | 10 min | 68 warnings |
| **P1** | 1.2 Stabilise Ollama routing | High | 2-3 hours | 76% failure rate, empty responses |
| **P1** | 2.1 Data fabrication fix | High | 3-4 hours | Violates invariant #8, fake tokens delivered |
| **P1** | 2.3 False positive security blocks | High | 2-3 hours | mpmath, builtin_modules blocked |
| **P1** | 3.1 Chain refusal status bug | High | 1-2 hours | "All passed" on all-refused chain |
| **P1** | 1.5 File selector parse failures | High | 1-2 hours | 21 failures degrading project tasks |
| **P2** | 4.1 Code truncation coverage | Medium | 2-3 hours | 9 errors in 2 days |
| **P2** | 4.3 Over-generation limits | Medium | 1-2 hours | 76K token single calls |
| **P2** | 3.2 Fix "Completed" acknowledgment | Medium | 15 min | Misleading UX |
| **P2** | 3.4 Timeout progress feedback | Medium | 2-3 hours | 5 timeouts with no feedback |
| **P2** | 2.2 Path sanitization in delivery | Medium | 1-2 hours | Production paths in 8+ artifacts |
| **P2** | 4.4 Log quality | Medium | 1-2 hours | 88% noise |
| **P3** | 3.5 /deploy task type check | Low | 30 min | Code-typed HTML rejected |
| **P3** | 3.3 Simple question fast path | Low | 3-4 hours | May conflict with invariant #1 |
| **P3** | 4.2 RAG context layer | Critical (long-term) | 2 days | Wrong CLI invocations, 21 file selector failures |
| **--** | Stop hook target fix | Low | 15 min | 18 markers in CLAUDE.md |
| **--** | 3.6 Cost monitoring/alerts | Low | 1 hour | $40.85/2 days = ~$400/month at daily use |

**Recommended order:** 1.1 -> 1.4 -> 4.5 -> 3.2 -> 1.3 -> 3.1 -> 1.5 -> 1.2 -> 2.1 -> 2.3 -> 4.1 -> 4.3 -> 3.4 -> 2.2 -> 4.4 -> 4.2

**Quick wins (under 30 min each):** 1.1 (server bind), 1.4 (Firebase PATH), 4.5 (Docker build), 3.2 ("Completed" -> "Processing"), 3.5 (/deploy HTML check), Stop hook fix. Total: ~2 hours for 6 fixes.

---

## Part 9: v8.7.0 Post-Production Audit (Source-Verified 2026-03-08)

*Every claim below verified against the actual source code. Corrections to the original audit noted inline.*

### 9.1 Critical: Shell Truncation False-Positive on Python (F-1)

**Verified at:** `brain/nodes/executor.py:91-96`

```python
if_count = len(re.findall(r'\bif\b', stripped))
fi_count = len(re.findall(r'\bfi\b', stripped))
shell_truncated = (if_count > fi_count + 2) or (do_count > done_count + 1)
```

**Confirmed:** The `\bif\b` regex matches Python's `if` keyword. Python never uses `fi`, so any Python code with >2 `if` statements triggers `shell_truncated = True`. This caused 100% failure rate on the analytics.py task (shell_if counts: 19/0, 11/0, 8/0 across 3 attempts). Each false positive forced a re-generation with progressively shorter prompts ("under 200 lines" then "under 100 lines, no comments, no docstrings"), stripping requirements until the code was incomplete.

**Cascade effect (F-2):** Truncation recovery produces incomplete code -> auditor correctly rejects -> retry re-plans from scratch -> hits F-1 again. 3 retry cycles x 9 API calls = ~$5+ wasted per task, all failing identically.

**Priority:** P0 — fix before any further testing.

**Recommended fix (refined from audit):** The audit suggests checking first 10 lines for `def`/`import`/`class`. A more robust approach: only apply 7D shell if/fi and do/done checks when the code starts with a shell shebang (`#!/bin/bash`, `#!/bin/sh`, `#!/usr/bin/env bash/sh`). If no shebang, skip shell truncation checks entirely. This avoids false negatives on Python scripts starting with comments/docstrings.

**Test needed:** `test_detect_truncation_python_with_many_ifs_not_truncated` — Python code with 20 `if` statements returns `False`.

---

### 9.2 Medium: /cost Model Name Display (F-3)

**Verified at:** `tools/claude_client.py:373`

```python
short = model.split("-")[-1] if "-" in model else model
```

On `claude-sonnet-4-6`: `split("-")` produces `["claude", "sonnet", "4", "6"]`, `[-1]` = `"6"`. Displayed as "6: $30.59 (100%)" — meaningless.

*Note: The original audit rated this "High" in the executive summary but "Medium" in the failure table. Correct severity is Medium — purely cosmetic, data is accurate.*

**Fix:** `model.replace("claude-", "").rsplit("-", 1)[0]` produces `"sonnet-4"` from `claude-sonnet-4-6`. Verified.

---

### 9.3 Medium: Budget/Ollama Interaction (F-4, F-5)

**Verified at:** `tools/model_router.py:40-54` (Ollama fallback) and `:80-82` (budget escalation)

**F-4:** After Ollama empty responses (2 retries, ~53s), falls back to Claude at line 54. No budget pre-check before the Claude call — `_check_budget()` fires inside `claude_client.call()` and raises `BudgetExceededError`. User wasted 55s getting nothing.

**F-5:** Budget escalation at line 80 routes to Ollama regardless of complexity:
```python
if purpose in ("classify", "plan") and _daily_spend_exceeds_threshold(0.7):
    if _ollama_available() and _ram_below_threshold(90):
        return ("ollama", config.OLLAMA_DEFAULT_MODEL)
```
No `complexity` check — even `complexity="high"` plans go to Ollama under budget pressure. Ollama times out (120s) on complex prompts, wasting 2 minutes before falling back to Claude anyway.

**Fix P1-2 (verified correct):** Add `and complexity != "high"` to line 80 condition. High-complexity tasks should never route to Ollama.

---

### 9.4 Medium: Inconsistent Cost Defaults

**Verified at:**
- `tools/model_router.py:148` — `_MODEL_COSTS.get(model, {"input": 3.00, "output": 15.00})`
- `tools/claude_client.py:119` — `MODEL_COSTS.get(model_name, {"input": 15.00, "output": 75.00})`
- `tools/claude_client.py:367` — same expensive default

The router's `_get_today_spend()` uses cheap defaults (3.00/15.00) for unknown models, while `_check_budget()` and `get_cost_summary()` use expensive defaults (15.00/75.00). If a new model appears, the router's threshold check under-counts spend while the budget enforcement over-counts — could trigger budget exceeded on a seemingly under-threshold day.

**Fix:** Harmonise `_get_today_spend()` default to `{"input": 15.00, "output": 75.00}` to match the budget check.

---

### 9.5 Fragility Analysis — Audit Corrections

**CORRECTION — Subprocess allowlist claim is WRONG:** The audit states `subprocess.run(["cp", "/etc/shadow", "/tmp/"])` passes because `cp` is on the allowlist. **This is incorrect.** The actual allowlist at `sandbox.py:493-494` is:
```python
_SUBPROCESS_SAFE_CMDS = {"pip", "pip3", "python", "python3", "ollama", "git",
                         "ls", "cat", "echo", "npm", "node", "head", "tail", "wc"}
```
`cp` and `mv` are NOT on this list. `subprocess.run(["cp", ...])` would be blocked by `_is_safe_subprocess()`. The audit's "medium likelihood" rating for this scenario is wrong — the scenario cannot occur.

**CONFIRMED — RAG zero-vector poisoning:** At `rag.py:167-169`, embedding failures pad with zero vectors to keep indices aligned. These zero-vector chunks would pollute query results. Verified — no filtering of zero-vector results on query.

**CONFIRMED — max_tokens/thinking interplay:** At `claude_client.py:187-188`, `max_tokens` is floored to 128000 when thinking is enabled. So `max_tokens=8192` in executor code gen (executor.py:556) has no effect — the model gets 128000 tokens total budget for thinking+text combined. Low practical impact (model handles allocation well).

**GARBLED TEXT — P2-3 fix:** The audit's P2-3 recommendation text is truncated/garbled. The intended fix for Linux path sanitisation is: `re.sub(r'/(Users|home)/\w+/', '~/', text)` — extending the existing macOS-only regex at `deliverer.py:34`.

---

### 9.6 Credential Filter Gaps (Verified)

**Verified at:** `brain/nodes/deliverer.py:17-22`

Missing patterns (confirmed absent):
- `sk-ant-api*` (Anthropic API keys)
- `xoxb-*` (Slack bot tokens)
- Telegram bot tokens (`[0-9]{8,10}:[A-Za-z0-9_-]{35}`)

**Verified at:** `brain/nodes/deliverer.py:48`

Credential scan only covers `.log`, `.txt`, `.json`, `.yaml`, `.yml`, `.csv`. Missing `.py`, `.html`, `.js` — a generated Python file containing a hardcoded API key would pass through.

---

### 9.7 Updated Priority Matrix (v8.7.0)

Items marked with status reflect v8.7.0 changes.

| Priority | Item | Impact | Effort | Status |
|----------|------|--------|--------|--------|
| **P0** | 9.1 Shell truncation false-positive (F-1) | Critical | 1 hour | NEW — blocks all Python code gen |
| **P0** | 9.2 /cost model name display (F-3) | Medium | 15 min | NEW — cosmetic but confusing |
| **P0** | 1.1 Preview server (--bind 127.0.0.1) | Critical | 30 min | Open |
| **P0** | 1.4 Firebase PATH | Critical | 15 min | Open |
| **~~P0~~** | ~~1.3 Code scanner bypass~~ | ~~Critical~~ | ~~4-6 hours~~ | **FIXED v8.7.0** (AST constant folding + written-file scanning) |
| **P1** | 9.3 Budget escalation skips high-complexity (F-5) | Medium | 30 min | NEW |
| **P1** | 9.4 Harmonise cost defaults | Low | 15 min | NEW |
| **P1** | 9.6 Credential filter gaps (patterns + extensions) | Medium | 1-2 hours | NEW |
| **P1** | 1.2 Stabilise Ollama routing | High | 2-3 hours | Open |
| **P1** | 2.1 Data fabrication fix | High | 3-4 hours | Open |
| **P1** | 2.3 False positive security blocks | High | 2-3 hours | Open |
| **P1** | 3.1 Chain refusal status bug | High | 1-2 hours | Open |
| **P1** | 1.5 File selector parse failures | High | 1-2 hours | Open |
| **P2** | RAG zero-vector poisoning guard | Medium | 1-2 hours | NEW |
| **P2** | 4.1 Code truncation (remaining non-7D issues) | Medium | 2-3 hours | Partially fixed v8.7.0 |
| **P2** | 4.3 Over-generation limits | Medium | 1-2 hours | Open |
| **P2** | 3.2 "Completed" -> "Processing" | Medium | 15 min | Open |
| **P2** | 3.4 Timeout progress feedback | Medium | 2-3 hours | Open |
| **P2** | 2.2 Path sanitization + Linux paths | Medium | 1-2 hours | Open |
| **P2** | 4.4 Log quality | Medium | 1-2 hours | Open |
| **~~P3~~** | ~~4.2 RAG context layer~~ | ~~Critical~~ | ~~2 days~~ | **IMPLEMENTED v8.7.0** |

**Recommended next session order:** 9.1 (F-1 shell truncation) -> 9.2 (cost display) -> 1.1 (server bind) -> 1.4 (Firebase PATH) -> 9.3 (budget escalation) -> 9.4 (cost defaults) -> 9.6 (credential patterns) -> re-run analytics.py to validate

---

### 9.8 Strengths Confirmed by v8.7.0 Production Data

- **Security blocklist holds.** Zero bypasses across 515+ API calls. AST constant folding (v8.7.0) now catches string concatenation evasion.
- **Adversarial audit catches real failures.** Opus correctly rejected incomplete analytics.py code despite "ALL ASSERTIONS PASSED" in execution output. Did not rubber-stamp.
- **Environment error short-circuit saves money.** Task 25fb9bf6 timed out at 300s -> auditor detected env error -> set retry_count to MAX_RETRIES -> skipped futile cycles.
- **Graceful degradation holds.** 40+ Ollama failures (Mar 6) and 5+ (Mar 8) all fell back to Claude without crashing. No unhandled exceptions.

### 9.9 Limitations Discovered

- **Shell truncation detection breaks Python** (F-1). The if/fi heuristic (7D) causes false positives on every non-trivial Python file. This made all code-generation tasks fail in the latest session. Workaround: none until P0-1 fix.
- **Retry loop doesn't learn.** Audit feedback goes to executor but not planner. Planner re-plans from scratch, potentially repeating the same structural mistakes.
- **Ollama unreliable under load.** Empty responses, 120s timeouts, 404s all observed. Budget escalation makes this worse by routing complex prompts to Ollama.
