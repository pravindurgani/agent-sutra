# AgentSutra v8.7.0 — Implementation Plan

*Based on analysis of: `AgentSutra_Improvements_Report.md` (v3), 30,747 lines of production logs,
377 Telegram messages from `agentruntime1_bot_20260308_165431.json`, and 96 output artifacts
in `telegram_bot_exports_logs_testsuite/`.*

*All evidence cross-referenced against live codebase. Fixes designed for the existing architecture —
no new abstractions, no invariant violations.*

---

## Priority Execution Order

```
Phase 0:  Quick wins (5 fixes, <1 hour total)
Phase 1:  Preview server fix (30 min)
Phase 2:  Code scanner bypass — AST + file-write scanning (4-6 hours)
Phase 3:  Chain refusal bug + UX fixes (2-3 hours)
Phase 4:  Ollama stabilisation (2-3 hours)
Phase 5:  Anti-fabrication hardening (3-4 hours)
Phase 6:  False positive reduction + over-generation limits (3-4 hours)
Phase 7:  Log quality + progress feedback (3-4 hours)
Phase 8:  Path sanitisation in delivery (1-2 hours)
Phase 9:  RAG context layer — LanceDB + Ollama embeddings (8-12 hours)
```

**Total: ~28-38 hours across 10 phases. Phases 0-2 before next production run. Phase 9 is a dedicated 2-day effort.**

---

## Phase 0 — Quick Wins (Under 30 min each)

### 0A. Fix "Completed" acknowledgment to "Done."

**Report ref:** 3.2. The bot sends "Completed. (task XXXX)" when the pipeline finishes,
but users interpret it as an acceptance confirmation (because it appears before artifacts are sent).

**Evidence:** Line 879 in `bot/handlers.py`:
```python
await status_msg.edit_text(f"Completed. (task {task_id[:8]})")
```

**Fix:** `bot/handlers.py` line 879 — change to:
```python
await status_msg.edit_text(f"Done. (task {task_id[:8]})")
```

**Touch points:** `bot/handlers.py:879`

**What NOT to do:**
- Don't change the line 816 "Starting..." message — that's correct
- Don't add a separate "Accepted" message — the streaming status updates already handle that

---

### 0B. Fix `/deploy` error message

**Report ref:** 3.5. A 404 error page HTML was classified as `code`, so `/deploy` showed a
misleading error about "frontend/ui_design outputs".

**Evidence:** `bot/handlers.py:1174-1181` — `/deploy` already searches by filename glob,
not by task type. The code is correct; the error message is misleading.

**Fix:** Change the error message:
```python
# Line 1177-1180
await update.message.reply_text(
    f"No HTML artifacts found for '{task_id_prefix}'. "
    "Make sure the task generated an HTML file."
)
```

**Touch points:** `bot/handlers.py:1177-1180`

**What NOT to do:**
- Don't add task_type checking — the glob approach is simpler and more flexible

---

### 0C. Fix Firebase PATH on Mac Mini

**Report ref:** 1.4. Firebase CLI worked on Mar 06 (v8.4.0) but missing from PATH on Mar 08.

**Fix:** Mac Mini environment fix (not code):
```bash
# Check if firebase exists
which firebase || npm list -g firebase-tools
# If missing, reinstall
npm install -g firebase-tools
# Update launchd plist PATH to include npm global bin
```

**Touch points:** `scripts/com.agentsutra.bot.plist` (EnvironmentVariables > PATH)

**What NOT to do:**
- Don't hardcode firebase path in deployer.py — PATH should be correct at the environment level

---

### 0D. Build Docker sandbox image on Mac Mini

**Report ref:** 4.5. 68 warnings about missing Docker image.

**Fix:** Run `./scripts/build_sandbox.sh` on Mac Mini, or set `DOCKER_ENABLED=false` in `.env`.

---

### 0E. Fix Stop hook target

**Report ref:** Part 8 DX. Stop hook appends markers to CLAUDE.md instead of SESSION_LOG.md.

**Fix:** Update `.claude/settings.json` (not `settings.local.json`) to target `SESSION_LOG.md`.

---

## Phase 1 — Preview Server Fix (30 min)

**Report ref:** 1.1. 17/18 server starts failed. Root cause: macOS firewall blocks `python3 -m http.server`
when binding to `0.0.0.0`. Binding to `127.0.0.1` bypasses the dialog. Since the server is only
used for Playwright visual checks on the same machine, external access is never needed.

### 1A. Bind preview server to localhost

**Touch points:** `tools/sandbox.py:start_server()` (lines 79-145)

**Implementation:**

After line 102 (`resolved_cmd = command.replace("{port}", str(port))`):
```python
# Bind to localhost to bypass macOS firewall dialog
if "http.server" in resolved_cmd and "--bind" not in resolved_cmd:
    resolved_cmd = resolved_cmd.replace("http.server", "http.server --bind 127.0.0.1")
```

**Mac Mini one-time setup** (belt-and-braces):
```bash
sudo /usr/libexec/ApplicationFirewall/socketfilterfw --add $(which python3)
sudo /usr/libexec/ApplicationFirewall/socketfilterfw --unblockapp $(which python3)
```

### 1B. Port-in-use detection for explicit ports

**Touch points:** `tools/sandbox.py:start_server()` (after line 100)

When `port` is provided explicitly, the `_find_free_port()` check is skipped. Add:
```python
if port is not None:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            logger.warning("Port %d in use, finding free port", port)
            port = _find_free_port()
```

### 1C. Kill orphaned servers on bot startup

**Touch points:** `main.py` (startup sequence)

```python
from tools.sandbox import stop_all_servers
stopped = stop_all_servers()
if stopped:
    logger.info("Cleaned up %d orphaned server(s)", stopped)
```

### 1D. Log health check failure details

**Touch points:** `tools/sandbox.py:_wait_for_http()` (line 241)

Before `return False`, add:
```python
logger.warning("Health check failed: port %d did not return HTTP 200 within %ds", port, timeout)
```

**What NOT to do:**
- Don't increase SERVER_START_TIMEOUT — 30s is generous; the firewall was the root cause
- Don't accept non-200 responses — 200 confirms the server is serving content

---

## Phase 2 — Code Scanner Bypass Fix (4-6 hours) — CRITICAL SECURITY

**Report ref:** 1.3. String concatenation bypass: `"su" + "do"` evades static regex matching.
File-write bypass: `.sh` written via `open()`, never triggered by shell scanner.
Both layers confirmed in `write_a_bash_sysadmin_1c0cbc.py`.
Opus auditor did NOT catch this. Full pipeline passed it through to delivery.

### 2A. AST-based constant folding scanner

**Touch points:** `tools/sandbox.py` — new function, called from `_check_code_safety()`

**Implementation:**

```python
import ast

def _resolve_constant_strings(code: str) -> list[str]:
    """Parse Python AST and resolve string concatenation.

    Finds BinOp(Constant + Constant) chains. Returns all resolved string values.
    Does NOT run code — only resolves compile-time constant expressions.
    """
    resolved = []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []

    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            value = _try_fold_concat(node)
            if value and isinstance(value, str):
                resolved.append(value)
    return resolved


def _try_fold_concat(node: ast.BinOp) -> str | None:
    """Recursively fold a chain of string additions."""
    if isinstance(node.op, ast.Add):
        left = _try_fold_value(node.left)
        right = _try_fold_value(node.right)
        if isinstance(left, str) and isinstance(right, str):
            return left + right
    return None


def _try_fold_value(node: ast.expr) -> str | None:
    """Extract string value from a constant or nested BinOp."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp):
        return _try_fold_concat(node)
    return None
```

Then in `_check_code_safety()`, after the existing regex scan (before `return None`):

```python
# AST scan: resolve string concatenation and check against blocklists
resolved_strings = _resolve_constant_strings(code)
for resolved in resolved_strings:
    for blocked in _BLOCKED_RE:
        if blocked.search(resolved):
            return (
                f"BLOCKED: Code constructs blocked pattern '{resolved[:60]}' "
                f"via string concatenation."
            )
    for pattern, label in _CODE_BLOCKED_PATTERNS:
        if pattern.search(resolved):
            return f"BLOCKED: Code constructs {label} via string concatenation."
```

**What NOT to do:**
- Don't trace variable assignments across lines (e.g., `x = "su"; y = x + "do"`) — requires
  data flow analysis, which is unbounded. The real bypass uses inline constants, which AST handles.
- Don't add more regex patterns trying to catch concatenation — whack-a-mole. AST is the fix.
- Don't scan inside string *literals* for blocked patterns — the existing Tier 1 full-text scan
  already does this. AST scanning catches *constructed* strings.

### 2B. Scan written file content

**Touch points:** `tools/sandbox.py` (new function) + `brain/nodes/executor.py:_execute_code()`

The bypass writes `.sh` via `open(..., "w")`. The shell scanner only fires when `bash/sh` is
the execution command.

**Implementation:**

```python
def _scan_written_files(working_dir: Path, pre_existing: set[str]) -> str | None:
    """Scan files written during execution for dangerous content.

    Checks .sh, .bash, .py, .js files that were created during execution.
    Returns error message if dangerous content found, None otherwise.
    """
    dangerous_exts = {".sh", ".bash", ".py", ".js", ".bat", ".cmd", ".ps1"}
    for f in working_dir.iterdir():
        if not f.is_file() or str(f) in pre_existing:
            continue
        if f.suffix.lower() not in dangerous_exts:
            continue
        try:
            content = f.read_text(errors="replace")[:50_000]
        except OSError:
            continue

        if f.suffix.lower() in (".sh", ".bash"):
            result = _check_shell_safety(content)
            if result:
                return f"Generated file '{f.name}' {result}"
        elif f.suffix.lower() == ".py":
            result = _check_code_safety(content)
            if result:
                return f"Generated file '{f.name}' {result}"
        elif f.suffix.lower() == ".js":
            result = _check_js_safety(content)
            if result:
                return f"Generated file '{f.name}' {result}"
    return None
```

**Integration in `brain/nodes/executor.py:_execute_code()`:**

Before execution, snapshot existing files:
```python
pre_existing = {str(f) for f in working_dir.iterdir() if f.is_file()}
```

After execution returns:
```python
from tools.sandbox import _scan_written_files
file_scan = _scan_written_files(working_dir, pre_existing)
if file_scan:
    result = ExecutionResult(
        stdout="", stderr=file_scan, return_code=1,
        success=False, artifacts=[], duration_seconds=0,
    )
```

**What NOT to do:**
- Don't scan recursively (rglob) — only the working directory
- Don't block file CREATION — block file DELIVERY
- Don't scan binary files (images, xlsx, etc.)

### 2C. Tests

Required tests in `tests/test_sandbox.py`:
- `test_ast_catches_string_concat_sudo` — `"su" + "do"` blocked
- `test_ast_catches_nested_concat` — `"r" + "m" + " -" + "rf"` blocked
- `test_ast_allows_benign_concat` — `"hello" + " world"` passes
- `test_ast_handles_syntax_error` — malformed code returns empty list
- `test_written_file_scan_catches_sh_sudo` — `.sh` with `sudo` blocked
- `test_written_file_scan_allows_safe_sh` — `.sh` with `echo hello` passes
- `test_written_file_scan_ignores_preexisting` — pre-existing files skipped

---

## Phase 3 — Chain Refusal Bug + UX Fixes (2-3 hours)

### 3A. Chain "All passed" on security refusals

**Report ref:** 3.1. `rm -rf ~/` chain reported "all passed" because security blocks produce
`"BLOCKED: ..."` in execution_result, but the gate only checks `"Execution: FAILED"` prefix.

**Touch points:** `bot/handlers.py:1117-1135`

**Implementation:**

```python
# Enhanced strict-AND gate
exec_result = result.get("execution_result", "")
exec_failed = exec_result.startswith("Execution: FAILED")
exec_blocked = "BLOCKED:" in exec_result

if exec_failed or exec_blocked or result.get("audit_verdict") != "pass":
    if exec_blocked:
        reason = "Security policy blocked this step"
    elif exec_failed:
        reason = "Execution returned non-zero exit code"
    else:
        reason = result.get("audit_feedback", "Unknown")[:300]
    await update.message.reply_text(
        f"Chain halted at step {i+1}/{len(steps)}.\n\n"
        f"Step refused: {step_msg[:100]}\n"
        f"Reason: {reason}\n\n"
        f"Steps {i+2}-{len(steps)} were NOT executed."
    )
    return
```

**What NOT to do:**
- Don't change the audit verdict for blocked tasks — the audit is correct
- Don't let chains continue past security blocks

### 3B. Timeout progress feedback

**Report ref:** 3.4. 5 tasks timed out at 900s with no progress feedback.

**Touch points:** `bot/handlers.py:846-870` (streaming status loop)

**Implementation:** Inside the `while not task_future.done()` loop:

```python
elapsed = _time.monotonic() - now_ts

# Progress update at 5 minutes
if elapsed > 300 and not context.user_data.get(f"_progress_{task_id}"):
    context.user_data[f"_progress_{task_id}"] = True
    try:
        stage = get_stage(task_id)
        await status_msg.edit_text(
            f"Still working... ({STAGE_LABELS.get(stage, stage)}, {int(elapsed)}s)"
        )
    except Exception:
        pass

# Warning at 80% of timeout
if elapsed > config.LONG_TIMEOUT * 0.8 and not context.user_data.get(f"_warn_{task_id}"):
    context.user_data[f"_warn_{task_id}"] = True
    remaining = config.LONG_TIMEOUT - int(elapsed)
    try:
        await status_msg.edit_text(f"Taking longer than expected. Timeout in {remaining}s.")
    except Exception:
        pass
```

Clean up in `finally` block:
```python
context.user_data.pop(f"_progress_{task_id}", None)
context.user_data.pop(f"_warn_{task_id}", None)
```

**What NOT to do:**
- Don't send separate messages — edit the existing status message
- Don't reduce LONG_TIMEOUT — 900s is needed for complex frontend tasks

---

## Phase 4 — Ollama Stabilisation (2-3 hours)

**Report ref:** 1.2. 76% failure rate: Mar 06 was wrong endpoint (100% fail), Mar 08 was
empty responses and timeouts (61% fail). 36 empty responses previously unreported.

### 4A. Empty response retry

**Touch points:** `tools/model_router.py:38-48`

DeepSeek R1 sometimes produces ONLY `<think>...</think>` with no final answer.

**Implementation:** Replace lines 38-48:
```python
if provider == "ollama":
    for attempt in range(2):
        try:
            result = _call_ollama(prompt, system, model, max_tokens)
            if result.strip():
                return result
            logger.warning(
                "Ollama empty response (attempt %d/2), %s",
                attempt + 1, "retrying" if attempt == 0 else "falling back to Claude",
            )
            if attempt == 0:
                time.sleep(2)
        except Exception as e:
            logger.warning("Ollama failed: %s, falling back to Claude", e)
            break
    provider, model = "claude", config.DEFAULT_MODEL
```

### 4B. Startup inference test

**Touch points:** `main.py` (after Ollama availability check)

```python
if ollama_ok:
    try:
        from tools.model_router import _call_ollama
        test = _call_ollama(
            "Classify: 'hello world'", system="Reply with ONE word: code",
            model=config.OLLAMA_DEFAULT_MODEL, max_tokens=10,
        )
        if test.strip():
            logger.info("Ollama inference OK: %s", test.strip()[:30])
        else:
            logger.warning("Ollama inference returned empty")
    except Exception as e:
        logger.warning("Ollama inference test failed: %s", e)
```

### 4C. Model recommendation (Mac Mini config, not code)

`deepseek-r1:14b` is overkill for classification. Recommend `OLLAMA_DEFAULT_MODEL=qwen2.5-coder:7b`.

**What NOT to do:**
- Don't add complex model selection logic in the router
- Don't reduce timeout below 120s — models need load time on first call
- Don't remove the `/api/generate` fallback — older Ollama versions exist

---

## Phase 5 — Anti-Fabrication Hardening (3-4 hours)

**Report ref:** 2.1. Four fabrication incidents. Only 1/4 caught by auditor.

### 5A. Executor input validation for referenced files

**Touch points:** `brain/nodes/executor.py:_execute_code()`

When the task says "grep agentsutra.log", check if that file exists BEFORE code generation.

```python
def _check_referenced_files(message: str, working_dir: Path) -> str:
    """Return a warning if files referenced in the message don't exist."""
    file_refs = re.findall(
        r'[\w./~-]+\.(?:log|csv|json|xlsx|txt|py|yaml|yml|db|sqlite)\b', message
    )
    missing = []
    for ref in file_refs:
        expanded = Path(ref).expanduser()
        if not expanded.exists() and not (working_dir / Path(ref).name).exists():
            missing.append(ref)
    if missing:
        return (
            "\nWARNING: These files do NOT exist: " + ", ".join(missing) + ". "
            "Do NOT create fake/sample versions. Report the file was not found "
            "and FAIL. NEVER fabricate data for a missing file."
        )
    return ""
```

Inject this warning into the code generation prompt.

### 5B. Strengthen auditor fabrication prompt

**Touch points:** `brain/nodes/auditor.py:SYSTEM_BASE` (append after line 42)

Add:
```
FABRICATION CHECK (CRITICAL):
- If task asks to read a SPECIFIC file and code CREATES sample data instead: FAIL
- If code generates realistic credentials (ghp_*, ya29.*, sk-*) as test data: FAIL
- Look for open(..., "w") creating files that match names the task asked to READ
```

### 5C. Deliverer credential pattern filter

**Touch points:** `brain/nodes/deliverer.py` (new check in `deliver()`)

Block delivery of artifacts containing credential-shaped strings:

```python
_CREDENTIAL_RE = [
    re.compile(r'\bghp_[a-zA-Z0-9]{36}\b'),       # GitHub PAT
    re.compile(r'\bya29\.[a-zA-Z0-9_-]{50,}\b'),   # Google OAuth
    re.compile(r'\bsk-[a-zA-Z0-9]{48}\b'),          # OpenAI key
    re.compile(r'\bAKIA[A-Z0-9]{16}\b'),            # AWS access key
]

def _has_credential_patterns(path: Path) -> bool:
    if path.suffix not in ('.log', '.txt', '.json', '.yaml', '.yml', '.csv'):
        return False
    try:
        content = path.read_text(errors='replace')[:50_000]
        return any(p.search(content) for p in _CREDENTIAL_RE)
    except OSError:
        return False
```

Filter artifacts: `artifacts = [a for a in artifacts if not _has_credential_patterns(Path(a))]`

**What NOT to do:**
- Don't block ALL artifacts on credential detection — only the specific file
- Don't check binary files or `.py` source files
- Don't add runtime file monitoring — too invasive (invariant #5)

---

## Phase 6 — False Positive Reduction + Over-Generation Limits (3-4 hours)

### 6A. Smart subprocess scanning

**Report ref:** 2.3. 15 legitimate subprocess uses blocked. `subprocess.run(["ls"])` is safe.

**Touch points:** `tools/sandbox.py:_check_code_safety()` and `_CODE_BLOCKED_PATTERNS`

Replace the blanket subprocess block with AST-based argument inspection:

```python
_SUBPROCESS_SAFE_CMDS = {"pip", "pip3", "python", "python3", "ollama", "git",
                         "ls", "cat", "echo", "npm", "node", "head", "tail", "wc"}
# git is on the safe list but git push is Tier 3 audit-logged. The _is_safe_subprocess
# function allows the command through; the existing Tier 3 _LOGGED_PATTERNS in
# _check_command_safety() handles audit logging for "git push" at the shell level.
# For subprocess.run(["git", "push", ...]), add a secondary argument check:
_SUBPROCESS_DANGEROUS_ARGS = {
    "git": {"push", "push --force", "remote"},
    "rm": set(),  # rm not in safe list, but belt-and-braces
}

def _is_safe_subprocess(code: str) -> bool:
    """True if ALL subprocess calls use commands from the safe list.

    Also checks secondary arguments: git is safe but git push is audit-logged.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "subprocess"):
            continue
        if not node.args:
            return False
        first = node.args[0]
        if isinstance(first, ast.List) and first.elts:
            cmd = first.elts[0]
            if isinstance(cmd, ast.Constant) and isinstance(cmd.value, str):
                name = Path(cmd.value).name
                if name not in _SUBPROCESS_SAFE_CMDS:
                    return False
                # Secondary arg check for dangerous subcommands
                dangerous = _SUBPROCESS_DANGEROUS_ARGS.get(name)
                if dangerous and len(first.elts) > 1:
                    arg2 = first.elts[1]
                    if isinstance(arg2, ast.Constant) and arg2.value in dangerous:
                        logger.info("AUDIT: subprocess %s %s detected", name, arg2.value)
                        # Don't block — just log (Tier 3 behavior)
                continue
        return False  # Dynamic command — can't verify
    return True
```

Remove blanket pattern from `_CODE_BLOCKED_PATTERNS` and add conditional:
```python
# In _check_code_safety():
if re.search(r"\bsubprocess\.\w+\s*\(", code):
    if not _is_safe_subprocess(code):
        return "BLOCKED: Code contains subprocess call with unsafe or dynamic command."
```

### 6B. Over-generation limits

**Report ref:** 4.3. Single calls consuming 50K-78K tokens (~$1.17 each).

**Touch points:** `brain/nodes/executor.py:_execute_code()` — set `max_tokens=8192` for
standard code generation calls. Frontend tasks already have appropriate limits.

**What NOT to do:**
- Don't set below 8192 — complex tasks need 6000-8000 tokens
- Don't whitelist ALL subprocess commands individually — use AST inspection

---

## Phase 7 — Log Quality + File Selector Fix (3-4 hours)

### 7A. Filter getUpdates noise

**Report ref:** 4.4. 88% of log lines are idle polling.

**Touch points:** `main.py` (logging configuration)

```python
logging.getLogger("httpx").setLevel(logging.WARNING)
```

Single line eliminates ~88% of noise.

### 7B. Task completion summary

**Touch points:** `brain/graph.py:run_task()` (line 140)

```python
timings = result.get("stage_timings", [])
timing_str = " ".join(f"{t['name']}:{t['duration_ms']}ms" for t in timings)
total_ms = sum(t["duration_ms"] for t in timings) if timings else 0
logger.info(
    "Task %s completed in %.1fs [%s] verdict=%s type=%s",
    task_id, total_ms / 1000, timing_str,
    result.get("audit_verdict", "?"), result.get("task_type", "?"),
)
```

### 7C. File selector retry on parse failure

**Report ref:** 1.5. 21 JSON parse failures degrade project task quality.

**Touch points:** `brain/nodes/planner.py` — file selector function

Wrap in single retry:
```python
for attempt in range(2):
    response = route_and_call(selector_prompt, ...)
    try:
        selected = json.loads(response)
        break
    except json.JSONDecodeError:
        if attempt == 0:
            logger.warning(
                "File selector parse failure (task %s), retrying. Raw: %s",
                state.get("task_id", "?"), response[:200],
            )
            continue
        logger.warning("File selector failed twice, falling back to enumeration")
        selected = []
```

### 7D. Extend truncation detection to shell scripts

**Report ref:** 4.1. 5x `unexpected EOF` in shell scripts.

**Touch points:** `brain/nodes/executor.py:_detect_truncation()`

Add shell checks:
```python
if_count = len(re.findall(r'\bif\b', stripped))
fi_count = len(re.findall(r'\bfi\b', stripped))
do_count = len(re.findall(r'\bdo\b', stripped))
done_count = len(re.findall(r'\bdone\b', stripped))
shell_truncated = (if_count > fi_count + 1) or (do_count > done_count + 1)
truncated = truncated or shell_truncated
```

**What NOT to do:**
- Don't add more than 1 retry for file selector — diminishing returns
- Don't add log rotation in code — use system-level `logrotate`

---

## Phase 8 — Path Sanitisation in Delivery (1-2 hours)

**Report ref:** 2.2. Production paths (`/Users/agentruntime1/`) in 8+ delivered artifacts.

### 8A. Sanitise paths in delivery messages

**Touch points:** `brain/nodes/deliverer.py` — in `deliver()` before returning

```python
def _sanitize_paths(text: str) -> str:
    """Replace production paths with generic equivalents in delivery messages."""
    text = re.sub(r'/Users/\w+/', '~/', text)
    text = re.sub(r'\bAdmin\.local\b', '<hostname>', text)
    return text
```

Apply: `final_response = _sanitize_paths(final_response)`

**What NOT to do:**
- Don't sanitise artifact FILE CONTENT — only the Telegram delivery message
- Don't sanitise paths in logs — useful for debugging

---

## Phase 9 — RAG Context Layer (8-12 hours, dedicated 2-day session)

**Report ref:** 4.2 + 1.5. The current `_inject_project_files()` system in `brain/nodes/planner.py:285-379`
has three cascading failures:
1. **50-file cap** — projects with >50 source files are skipped entirely (line 324-329)
2. **21 JSON parse failures** — Claude returns prose instead of JSON 16% of the time (line 348-350)
3. **Lottery injection** — even when it works, Claude picks 3-5 files from a flat listing with no
   semantic understanding, missing architectural context

RAG replaces the file-selection Claude call with vector similarity search: embed the task description,
retrieve the top-k most relevant code chunks, inject them. No LLM call needed for selection.

### 9A. Dependencies and index storage

**New dependency:** `lancedb` in `requirements.txt`
**Embedding model:** `nomic-embed-text` via Ollama (768-dim, local, no API cost)

```bash
pip install lancedb
ollama pull nomic-embed-text
```

**Index location:** `~/.agentsutra/rag_indexes/{project_name}/` — one LanceDB table per project.

**Touch points:** `requirements.txt`, `config.py` (new constants)

**New constants in `config.py`:**
```python
# RAG configuration
RAG_ENABLED = os.getenv("RAG_ENABLED", "true").lower() == "true"
RAG_EMBED_MODEL = os.getenv("RAG_EMBED_MODEL", "nomic-embed-text")
RAG_INDEX_DIR = Path.home() / ".agentsutra" / "rag_indexes"
RAG_CHUNK_SIZE = 120          # lines per chunk (code-aware, see 9B)
RAG_CHUNK_OVERLAP = 20        # lines overlap between chunks
RAG_TOP_K = 8                 # chunks to retrieve
RAG_STALE_HOURS = 24          # re-index if older than this
RAG_MAX_INDEX_FILES = 500     # skip indexing if project exceeds this
```

### 9B. Python-aware chunking

**Touch points:** New file `tools/rag.py`

Naive line splitting breaks functions in half. Use `ast.parse()` for `.py` files to chunk
at function/class boundaries. Fall back to line-based chunking for other file types.

```python
"""RAG context layer — LanceDB + Ollama embeddings for project file injection."""

import ast
import hashlib
import logging
import time
from pathlib import Path

import config

logger = logging.getLogger(__name__)


def _chunk_python(content: str, file_path: str) -> list[dict]:
    """Chunk a Python file at function/class boundaries.

    Each chunk includes the full function/class body plus a header
    with the file path and definition name for context.
    """
    chunks = []
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return _chunk_lines(content, file_path)

    lines = content.splitlines(keepends=True)

    # Extract top-level and nested functions/classes
    nodes = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            nodes.append(node)

    if not nodes:
        return _chunk_lines(content, file_path)

    # Sort by line number
    nodes.sort(key=lambda n: n.lineno)

    for node in nodes:
        start = node.lineno - 1  # 0-indexed
        end = node.end_lineno if node.end_lineno else start + 1
        body = "".join(lines[start:end])
        if len(body.strip()) < 20:
            continue
        chunks.append({
            "text": f"# {file_path}:{node.lineno} — {node.name}\n{body}",
            "file_path": file_path,
            "line_start": node.lineno,
            "line_end": end,
            "kind": "function" if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else "class",
        })

    # Module-level code (imports, constants) — first 50 lines or up to first def
    first_def_line = nodes[0].lineno if nodes else len(lines)
    module_header = "".join(lines[:min(first_def_line - 1, 50)])
    if module_header.strip():
        chunks.insert(0, {
            "text": f"# {file_path}:1 — module header\n{module_header}",
            "file_path": file_path,
            "line_start": 1,
            "line_end": min(first_def_line - 1, 50),
            "kind": "module_header",
        })

    return chunks


def _chunk_lines(content: str, file_path: str) -> list[dict]:
    """Line-based chunking with overlap for non-Python files."""
    lines = content.splitlines(keepends=True)
    chunks = []
    size = config.RAG_CHUNK_SIZE
    overlap = config.RAG_CHUNK_OVERLAP

    for i in range(0, len(lines), size - overlap):
        block = "".join(lines[i:i + size])
        if len(block.strip()) < 20:
            continue
        chunks.append({
            "text": f"# {file_path}:{i+1}\n{block}",
            "file_path": file_path,
            "line_start": i + 1,
            "line_end": min(i + size, len(lines)),
            "kind": "block",
        })

    return chunks


def chunk_file(file_path: Path, project_root: Path) -> list[dict]:
    """Chunk a single file using the appropriate strategy."""
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    # Cap file size to avoid embedding giant files
    if len(content) > 100_000:
        content = content[:100_000]

    rel_path = str(file_path.relative_to(project_root))

    if file_path.suffix == ".py":
        return _chunk_python(content, rel_path)
    return _chunk_lines(content, rel_path)
```

### 9C. Embedding and index management

**Touch points:** `tools/rag.py` (continued)

```python
def _embed_via_ollama(texts: list[str]) -> list[list[float]]:
    """Get embeddings from Ollama's nomic-embed-text model.

    Batches requests to avoid overwhelming Ollama on 16GB machines.
    """
    import httpx

    base_url = config.OLLAMA_BASE_URL.rstrip("/")
    embeddings = []
    batch_size = 16

    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        try:
            resp = httpx.post(
                f"{base_url}/api/embed",
                json={"model": config.RAG_EMBED_MODEL, "input": batch},
                timeout=60.0,
            )
            resp.raise_for_status()
            data = resp.json()
            embeddings.extend(data["embeddings"])
        except Exception as e:
            logger.warning("Embedding batch %d failed: %s", i // batch_size, e)
            # Pad with zero vectors so indices stay aligned
            embeddings.extend([[0.0] * 768] * len(batch))

    return embeddings


def build_index(project_name: str, project_path: Path) -> bool:
    """Build or rebuild the RAG index for a project.

    Returns True if index was built successfully.
    """
    import lancedb

    index_dir = config.RAG_INDEX_DIR / project_name
    index_dir.mkdir(parents=True, exist_ok=True)

    # Check staleness
    marker = index_dir / ".indexed_at"
    if marker.exists():
        age_hours = (time.time() - marker.stat().st_mtime) / 3600
        if age_hours < config.RAG_STALE_HOURS:
            logger.debug("RAG index for %s is fresh (%.1fh old)", project_name, age_hours)
            return True

    # Collect files (reuse planner's exclude list)
    from brain.nodes.planner import _INJECT_EXTENSIONS, _INJECT_EXCLUDE_DIRS

    source_files: list[Path] = []
    file_count = 0
    try:
        for p in project_path.rglob("*"):
            file_count += 1
            if file_count > config.RAG_MAX_INDEX_FILES * 2:
                break
            if any(excluded in p.parts for excluded in _INJECT_EXCLUDE_DIRS):
                continue
            if p.is_symlink():
                continue
            if p.is_file() and p.suffix in _INJECT_EXTENSIONS:
                source_files.append(p)
    except OSError as e:
        logger.warning("Failed to scan %s for RAG indexing: %s", project_path, e)
        return False

    if not source_files:
        logger.info("No indexable files in %s", project_name)
        return False

    if len(source_files) > config.RAG_MAX_INDEX_FILES:
        logger.warning(
            "Project %s has %d files, exceeds RAG_MAX_INDEX_FILES (%d). Skipping.",
            project_name, len(source_files), config.RAG_MAX_INDEX_FILES,
        )
        return False

    # Chunk all files
    all_chunks = []
    for f in source_files:
        all_chunks.extend(chunk_file(f, project_path))

    if not all_chunks:
        return False

    logger.info("RAG indexing %s: %d chunks from %d files", project_name, len(all_chunks), len(source_files))

    # Embed
    texts = [c["text"] for c in all_chunks]
    vectors = _embed_via_ollama(texts)

    if len(vectors) != len(all_chunks):
        logger.warning("Embedding count mismatch: %d vectors vs %d chunks", len(vectors), len(all_chunks))
        return False

    # Build LanceDB table
    records = []
    for chunk, vec in zip(all_chunks, vectors):
        records.append({
            "text": chunk["text"],
            "file_path": chunk["file_path"],
            "line_start": chunk["line_start"],
            "line_end": chunk["line_end"],
            "kind": chunk["kind"],
            "vector": vec,
        })

    db = lancedb.connect(str(index_dir))
    # Overwrite existing table
    db.create_table("chunks", records, mode="overwrite")

    # Write marker
    marker.write_text(str(time.time()))
    logger.info("RAG index built for %s: %d chunks", project_name, len(records))
    return True


def query_index(project_name: str, query: str, top_k: int | None = None) -> list[dict]:
    """Retrieve the top-k most relevant chunks for a query.

    Returns list of dicts with 'text', 'file_path', 'line_start', 'line_end'.
    Returns empty list on any failure (graceful degradation).
    """
    import lancedb

    k = top_k or config.RAG_TOP_K
    index_dir = config.RAG_INDEX_DIR / project_name

    if not (index_dir / ".indexed_at").exists():
        return []

    try:
        query_vec = _embed_via_ollama([query])[0]
    except Exception as e:
        logger.warning("RAG query embedding failed: %s", e)
        return []

    try:
        db = lancedb.connect(str(index_dir))
        table = db.open_table("chunks")
        results = table.search(query_vec).limit(k).to_list()
        return [
            {
                "text": r["text"],
                "file_path": r["file_path"],
                "line_start": r["line_start"],
                "line_end": r["line_end"],
            }
            for r in results
        ]
    except Exception as e:
        logger.warning("RAG query failed for %s: %s", project_name, e)
        return []
```

### 9D. Integration into planner

**Touch points:** `brain/nodes/planner.py:_inject_project_files()` — replace, not wrap

The existing function costs one Sonnet call (~$0.02) per project task for file selection,
fails 16% of the time (21 JSON errors), and skips projects with >50 files. RAG replaces
this with zero-LLM-cost vector search.

**Implementation:**

Replace lines 285-379 of `brain/nodes/planner.py`:
```python
def _inject_project_files(state: AgentState, system: str) -> str:
    """Inject relevant project code into the system prompt using RAG.

    Uses vector similarity search (LanceDB + nomic-embed-text) to find
    relevant code chunks. Falls back to the legacy file selector if RAG
    is disabled or the index doesn't exist.

    Costs: ~0.5s Ollama embedding call (no Claude API cost).
    """
    import json

    project_config = state.get("project_config", {})
    project_path = Path(project_config.get("path", ""))
    project_name = state.get("project_name", project_path.name)

    if not project_path.is_dir():
        return system

    # Try RAG first
    if config.RAG_ENABLED:
        try:
            from tools.rag import build_index, query_index

            # Build/refresh index (no-op if fresh)
            build_index(project_name, project_path)

            # Query with the task message
            chunks = query_index(project_name, state["message"])
            if chunks:
                injected = "\n\n".join(c["text"] for c in chunks)
                system += (
                    f"\n\nRELEVANT CODE FROM {project_name.upper()} (via RAG):\n"
                    + injected
                )
                logger.info(
                    "RAG injected %d chunks for %s (files: %s)",
                    len(chunks), project_name,
                    ", ".join(sorted(set(c["file_path"] for c in chunks))),
                )
                return system
        except ImportError:
            logger.warning("lancedb not installed, falling back to file selector")
        except Exception as e:
            logger.warning("RAG failed for %s: %s, falling back", project_name, e)

    # Legacy fallback: Claude-based file selector (existing logic)
    source_files: list[str] = []
    try:
        _enum_count = 0
        for p in project_path.rglob("*"):
            _enum_count += 1
            if _enum_count > config.MAX_FILE_INJECT_COUNT * 2:
                break
            if any(excluded in p.parts for excluded in _INJECT_EXCLUDE_DIRS):
                continue
            if p.is_symlink():
                continue
            if p.is_file() and p.suffix in _INJECT_EXTENSIONS:
                source_files.append(str(p.relative_to(project_path)))
    except OSError as e:
        logger.warning("Failed to scan project directory %s: %s", project_path, e)
        return system

    if not source_files or len(source_files) > config.MAX_FILE_INJECT_COUNT:
        return system

    tree_listing = "\n".join(sorted(source_files))
    selector_prompt = (
        f"Task: {state['message'][:500]}\n\n"
        f"Project file tree:\n{tree_listing}"
    )

    try:
        selection = claude_client.call(
            selector_prompt, system=_FILE_SELECTOR_SYSTEM,
            max_tokens=300, temperature=0.0,
        )
        selected = json.loads(selection)
        if not isinstance(selected, list):
            raise ValueError("Expected a JSON list")
    except (json.JSONDecodeError, ValueError, Exception) as e:
        logger.warning("File selector failed: %s", e)
        return system

    resolved_root = project_path.resolve()
    injected_parts: list[str] = []
    for rel_path in selected[:5]:
        full = (project_path / rel_path).resolve()
        if not full.is_file():
            continue
        try:
            full.relative_to(resolved_root)
        except ValueError:
            logger.warning("Path traversal blocked: %s escapes %s", rel_path, resolved_root)
            continue
        try:
            content = full.read_text(encoding="utf-8", errors="replace")[:3000]
            injected_parts.append(f"--- {rel_path} ---\n{content}")
        except OSError:
            continue

    if injected_parts:
        system += (
            f"\n\nRELEVANT CODE FROM {project_name.upper()}:\n"
            + "\n\n".join(injected_parts)
        )
        logger.info("Injected %d files for project %s (legacy)", len(injected_parts), project_name)
    return system
```

**Key design decisions:**
- RAG is tried first, legacy is the fallback — not the other way around
- `build_index()` is called inline but no-ops if index is fresh (<24h). First call for a project
  will be slow (~10-30s depending on project size) but subsequent calls are instant.
- The legacy path is kept intact so `RAG_ENABLED=false` restores original behaviour exactly
- No change to the pipeline structure (invariant #1)

### 9E. Telegram command for manual re-indexing

**Touch points:** `bot/handlers.py` — new `/reindex` command

```python
async def cmd_reindex(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Force re-index a project for RAG."""
    if not _is_authorized(update):
        return

    args = context.args
    if not args:
        await update.message.reply_text("Usage: /reindex <project_name>")
        return

    project_name = " ".join(args)
    from tools.projects import load_projects
    projects = load_projects()
    project = projects.get(project_name)
    if not project:
        await update.message.reply_text(f"Unknown project: {project_name}")
        return

    await update.message.reply_text(f"Re-indexing {project_name}...")
    try:
        from tools.rag import build_index
        # Delete staleness marker to force rebuild
        marker = config.RAG_INDEX_DIR / project_name / ".indexed_at"
        if marker.exists():
            marker.unlink()
        success = await asyncio.get_event_loop().run_in_executor(
            None, build_index, project_name, Path(project["path"]),
        )
        if success:
            await update.message.reply_text(f"Re-indexed {project_name}.")
        else:
            await update.message.reply_text(f"Failed to index {project_name}. Check logs.")
    except Exception as e:
        logger.warning("Re-index failed for %s: %s", project_name, e)
        await update.message.reply_text(f"Error: {e}")
```

Register in `bot/telegram_bot.py`: `app.add_handler(CommandHandler("reindex", cmd_reindex))`

### 9F. Tests

**Touch points:** `tests/test_rag.py` (new file)

```python
# Required tests:
# test_chunk_python_extracts_functions — verify ast-based chunking produces function-level chunks
# test_chunk_python_handles_syntax_error — falls back to line-based
# test_chunk_lines_basic — verify line-based chunking with overlap
# test_chunk_file_caps_large_files — >100KB truncated
# test_embed_via_ollama_batch — mock httpx, verify batching at 16
# test_embed_via_ollama_failure_pads_zeros — failed batch returns zero vectors
# test_build_index_creates_table — mock Ollama, verify LanceDB table created
# test_build_index_skips_fresh — marker <24h old, returns True without re-indexing
# test_build_index_skips_large_projects — >500 files returns False
# test_query_index_returns_results — mock search, verify top-k
# test_query_index_missing_index — returns empty list
# test_inject_project_files_uses_rag — RAG_ENABLED=true, verify RAG path taken
# test_inject_project_files_fallback — RAG fails, verify legacy path taken
# test_inject_project_files_rag_disabled — RAG_ENABLED=false, verify legacy path
```

### 9G. Rollout strategy

1. **Day 1:** Implement `tools/rag.py` (9B + 9C), add `lancedb` to requirements, add config constants
2. **Day 1:** Pull `nomic-embed-text` on Mac Mini: `ollama pull nomic-embed-text`
3. **Day 1:** Run tests against mock Ollama (no live embeddings needed for unit tests)
4. **Day 2:** Integrate into planner (9D), keeping legacy fallback
5. **Day 2:** Add `/reindex` command (9E)
6. **Day 2:** Live test on Mac Mini with a small project first (e.g., Work Reports — 12 files)
7. **Day 2:** If stable, enable for all 12 projects

**What NOT to do:**
- Don't use LangChain's document loaders or vector store wrappers — invariant #5 (no abstractions).
  LanceDB's native API is simpler than LangChain's wrapper around it.
- Don't use a cloud vector DB (Pinecone, Weaviate, Qdrant) — self-hosted, no external dependencies
- Don't embed at query time only — pre-build indexes. The 10-30s first-call latency is acceptable
  once per 24 hours, not on every task.
- Don't chunk at the line level for Python — AST-aware chunking preserves function boundaries.
  A split function body is worse than no context.
- Don't remove the legacy file selector — it's the safety net when RAG is disabled or Ollama
  is down. `RAG_ENABLED=false` should restore exact v8.5.2 behaviour.
- Don't embed docstrings/comments separately from code — they're meaningless without the function body
- Don't use `text-embedding-3-small` or other API-based embeddings — defeats the cost-saving purpose.
  Ollama's nomic-embed-text is free and local.
- Don't try to do incremental indexing (only changed files) in v1 — full rebuild is fast enough for
  <500 files. Git-diff-based incremental can come later if needed.
- Don't cache embeddings in SQLite — LanceDB IS the cache. Adding another storage layer is redundant.

---

## What NOT To Do (Global)

| Tempting Idea | Why It's Wrong |
|---------------|---------------|
| Web dashboard for monitoring | Violates invariant #5 (no speculative abstractions) |
| Dynamic pipeline (skip audit for simple tasks) | Violates invariant #1 (5-stage pipeline is FIXED) |
| Direct answer bypass for simple questions | Violates invariant #1. Make classifier smarter WITHIN the pipeline |
| Full AST analyser replacing all regex | Over-engineering. AST for concatenation + regex for everything else |
| Provider abstraction layer | Violates invariant #5. model_router.py is the right level |
| Whitelist ALL subprocess commands individually | Unmaintainable. Use AST-based argument scanning |
| Runtime process monitoring / ptrace | Too invasive. Threat model is LLM hallucination |
| Pre-scan user prompts for security intent | Wrong layer. Security belongs at execution time |
| Cache Ollama model in RAM permanently | 16GB Mac Mini — 14B model would starve the pipeline |

---

## Dependency Graph

```
Phase 0 -> independent, do first
Phase 1 -> independent
Phase 2 -> independent, CRITICAL (do before next production run)
Phase 3A -> independent (benefits from Phase 2 BLOCKED pattern)
Phase 3B -> independent
Phase 4 -> independent
Phase 5A -> benefits from Phase 2B (file scanning)
Phase 5B,5C -> independent
Phase 6A -> benefits from Phase 2A (AST scanner)
Phase 6B -> independent
Phase 7A -> independent, do early (makes all debugging easier)
Phase 7B-D -> independent
Phase 8 -> independent
Phase 9 -> requires Phase 4 (Ollama must be stable for embeddings)
Phase 9 -> benefits from Phase 7C (file selector retry shares fallback logic)
```

**Recommended session order:**
1. **Session 1** — Phase 0 + 1 + 7A (quick wins + server + log noise — 1.5 hours)
2. **Session 2** — Phase 2 (critical security — 4-6 hours, dedicated session)
   - **Checkpoint:** Run `pytest tests/test_sandbox.py -v` — all new AST + file-scan tests must pass before continuing
3. **Session 3** — Phase 3 + 4 (UX + Ollama — 4-5 hours)
4. **Session 4** — Phase 5 + 6 (anti-fabrication + false positives — 6-8 hours)
5. **Session 5** — Phase 7B-D + 8 (log quality + path sanitisation — 3-4 hours)
6. **Session 6** — Phase 9 (RAG context layer — 8-12 hours, 2-day dedicated session)

---

## Production Stats Reference

| Metric | Value | Source |
|--------|-------|--------|
| Total pipeline runs | 111 | Log analysis |
| Success rate | 78.4% (87/111) | Log analysis |
| Total API calls | 783 (601 Sonnet, 182 Opus) | Log analysis |
| Estimated cost | ~$40.85 / 2 active days | Log + pricing |
| Ollama success rate | 23.7% (31/131) | Log (corrected from 0%) |
| Server start success rate | 5.6% (1/18) | Log analysis |
| Security blocks | 70 total | Log (corrected from 51) |
| False positive blocks | 2 (mpmath, builtin_modules) | Telegram chat |
| File selector parse failures | 21 | Log analysis |
| Pipeline timeouts (900s) | 5 | Log analysis |
| Code truncation errors | 9 | Log analysis |
| Crashes | 0 | Log analysis |
