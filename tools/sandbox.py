from __future__ import annotations

import ast
import os
import re
import shlex
import subprocess
import tempfile
import threading
import time
import uuid
import logging
from pathlib import Path
from typing import Any
from dataclasses import dataclass, field

import config

logger = logging.getLogger(__name__)

# ── Live output registry (thread-safe) ──────────────────────────────
_live_output: dict[str, list[str]] = {}
_live_output_lock = threading.Lock()


def _register_live_output(task_id: str):
    with _live_output_lock:
        _live_output[task_id] = []


def _append_live_output(task_id: str, line: str):
    with _live_output_lock:
        lines = _live_output.get(task_id)
        if lines is not None:
            lines.append(line)
            if len(lines) > 50:
                del lines[:25]  # Keep bounded


def get_live_output(task_id: str, tail: int = 3) -> str:
    """Get the last N lines of live stdout for a running task."""
    with _live_output_lock:
        lines = _live_output.get(task_id, [])
        return "\n".join(lines[-tail:])


def _clear_live_output(task_id: str):
    with _live_output_lock:
        _live_output.pop(task_id, None)


# ── Server registry (thread-safe) ─────────────────────────────────
_running_servers: dict[str, dict] = {}  # task_id -> {"proc": Popen, "port": int, "started_at": float}
_server_timers: dict[str, threading.Timer] = {}  # task_id -> Timer (cancellable auto-kill)
_server_lock = threading.Lock()


def _find_free_port() -> int:
    """Find a free port in the configured range.

    Returns:
        An available port number.

    Raises:
        RuntimeError: If no ports are free in the configured range.
    """
    import socket
    for port in range(config.SERVER_PORT_RANGE_START, config.SERVER_PORT_RANGE_END):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(
        f"No free ports in range {config.SERVER_PORT_RANGE_START}-{config.SERVER_PORT_RANGE_END}"
    )


def start_server(
    command: str,
    working_dir: Path,
    task_id: str,
    port: int | None = None,
) -> tuple[str, int]:
    """Start a server process and wait for it to respond on HTTP.

    Args:
        command: Shell command to run. Use {port} as placeholder for the port.
        working_dir: Directory to run the server in.
        task_id: Task ID for registry tracking.
        port: Specific port to use, or None to auto-detect.

    Returns:
        Tuple of (url, port) on success.

    Raises:
        RuntimeError: If the server doesn't start within SERVER_START_TIMEOUT.
    """
    if port is None:
        port = _find_free_port()
    else:
        # 1B: Check if explicit port is available
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                logger.warning("Port %d in use, finding free port", port)
                port = _find_free_port()

    resolved_cmd = command.replace("{port}", str(port))

    # 1A: Bind to localhost to bypass macOS firewall dialog
    if "http.server" in resolved_cmd and "--bind" not in resolved_cmd:
        resolved_cmd = resolved_cmd.replace("http.server", "http.server --bind 127.0.0.1")

    # A-1: Safety check server commands through Tier 1 blocklist
    safety_msg = _check_command_safety(resolved_cmd)
    if safety_msg:
        raise RuntimeError(f"Server command blocked: {safety_msg}")

    logger.info("Starting server for task %s on port %d: %s", task_id, port, resolved_cmd)

    proc = subprocess.Popen(
        resolved_cmd,
        shell=True,
        cwd=working_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env=_filter_env(),  # A-1: Strip credentials from server env
    )

    with _server_lock:
        _running_servers[task_id] = {
            "proc": proc,
            "port": port,
            "started_at": time.time(),
        }

    # Poll until HTTP responds or timeout
    if not _wait_for_http(port, config.SERVER_START_TIMEOUT):
        # Server didn't start — clean up
        stop_server(task_id)
        raise RuntimeError(
            f"Server on port {port} did not respond within {config.SERVER_START_TIMEOUT}s"
        )

    # Start cancellable auto-kill timer (R-1: avoids thread accumulation)
    kill_timer = threading.Timer(config.SERVER_MAX_LIFETIME, _auto_kill_callback, args=(task_id,))
    kill_timer.daemon = True
    kill_timer.start()
    with _server_lock:
        _server_timers[task_id] = kill_timer

    url = f"http://127.0.0.1:{port}"
    logger.info("Server for task %s running at %s", task_id, url)
    return url, port


def stop_server(task_id: str) -> bool:
    """Stop a running server by task_id.

    Cancels the auto-kill timer (R-1) and kills the server process.

    Args:
        task_id: The task ID whose server should be stopped.

    Returns:
        True if a server was stopped, False if none found.
    """
    import signal

    with _server_lock:
        entry = _running_servers.pop(task_id, None)
        timer = _server_timers.pop(task_id, None)

    if timer is not None:
        timer.cancel()

    if entry is None:
        return False

    proc = entry["proc"]
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass  # Already dead
        logger.info("Stopped server for task %s (port %d)", task_id, entry["port"])
    return True


def stop_all_servers() -> int:
    """Stop all running servers.

    Returns:
        Number of servers stopped.
    """
    with _server_lock:
        task_ids = list(_running_servers.keys())

    count = 0
    for tid in task_ids:
        if stop_server(tid):
            count += 1
    return count


def list_servers() -> list[dict]:
    """Return list of running servers with task_id, port, pid, uptime.

    Returns:
        List of dicts with server info. Only includes servers still running.
    """
    now = time.time()
    result = []
    with _server_lock:
        for tid, entry in list(_running_servers.items()):
            proc = entry["proc"]
            if proc.poll() is not None:
                # Process already exited — clean up
                _running_servers.pop(tid, None)
                continue
            result.append({
                "task_id": tid,
                "port": entry["port"],
                "pid": proc.pid,
                "uptime": int(now - entry["started_at"]),
            })
    return result


def _wait_for_http(port: int, timeout: int) -> bool:
    """Poll localhost:{port} until it returns 200 or timeout.

    Args:
        port: Port to check.
        timeout: Maximum seconds to wait.

    Returns:
        True if a 200 response was received, False on timeout.
    """
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = urllib.request.urlopen(f"http://127.0.0.1:{port}", timeout=2)
            if resp.status == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    # 1D: Log health check failure details
    logger.warning("Health check failed: port %d did not return HTTP 200 within %ds", port, timeout)
    return False


def _auto_kill_callback(task_id: str):
    """Called by threading.Timer when server lifetime expires (R-1)."""
    stopped = stop_server(task_id)
    if stopped:
        logger.info("Auto-killed server for task %s after %ds", task_id, config.SERVER_MAX_LIFETIME)


# Docker availability cache (lazy-checked, refreshed every 60s)
_docker_status: dict[str, Any] = {"available": False, "checked_at": 0.0}

# Serialize Docker pip installs to prevent .pip-cache corruption under concurrency
_docker_pip_lock = threading.Lock()

# ── Tiered command safety ────────────────────────────────────────────
# TIER 1: Catastrophic, irreversible — ALWAYS BLOCKED
_BLOCKED_PATTERNS = [
    # rm -rf targeting home, root, or user directories
    # Handles short flags (-rf), split flags (-r -f), and GNU long flags (--recursive --force)
    r"\brm\s+(-{1,2}[\w-]+\s+)*\s*(/\s*$|~\s*['\"]?\s*$|~/\s*$|\$HOME)",
    r"\brm\s+(-{1,2}[\w-]+\s+)*/Users\b",
    r"\brm\s+(-{1,2}[\w-]+\s+)*/home\b",
    # rm targeting critical home subdirectories
    r"\brm\s+(-{1,2}[\w-]+\s+)*\s*~/?(Desktop|Documents|Downloads|Pictures|Music|Movies|Library|Applications)\b",
    # Filesystem destruction
    r"\bmkfs\b",
    r"\bdd\s+if=",
    r">\s*/dev/sd[a-z]",
    # Fork bomb variants
    r":\(\)\s*\{",
    r"\bfork\s*bomb\b",
    # System power
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bhalt\b",
    r"\bpoweroff\b",
    # Privilege escalation
    r"\bsudo\b",
    # Pipe-to-shell attacks (remote code execution via URL)
    r"\bcurl\b.*\|\s*\bsh\b",
    r"\bcurl\b.*\|\s*\bbash\b",
    r"\bwget\b.*\|\s*\bsh\b",
    r"\bwget\b.*\|\s*\bbash\b",
    # Recursive permission destruction
    r"\bchmod\s+(-[rR]\s+|--recursive\s+)?(777|a\+rwx)\s+[/~]",
    # Interpreter inline code execution (perl -e, ruby -e, node -e)
    # NOTE: python3 -c removed from Tier 1 — it's a normal scripting pattern.
    # It remains in _LOGGED_PATTERNS for audit trail.
    r"\bperl\s+-[eE]\s",
    r"\bruby\s+-[eE]\s",
    r"\bnode\s+-[eE]\s",
    # Destructive find operations
    r"\bfind\b.*\s-delete\b",
    r"\bfind\b.*-exec\s+rm\b",
    # Encoding bypass (base64 decode piped to shell)
    r"\bbase64\s.*\|\s*(sh|bash)\b",
    # Home directory relocation (~ or ~/ as source argument)
    r"\bmv\s+(-\w+\s+)*~(\s|$)",
    r"\bmv\s+(-\w+\s+)*~/(\s|$)",
    # Write/append redirects to critical dotfiles
    r">>?\s*~/?\.(ssh|bashrc|bash_profile|zshrc|zprofile|profile|gitconfig|gnupg|npmrc|netrc)",
    # Symlink attacks on critical dotfiles
    r"\bln\s+.*~/?\.(ssh|bashrc|bash_profile|zshrc|zprofile|profile|gitconfig|gnupg)",
    # printf/echo/cat piped to shell (like curl|sh but via printf/echo/cat)
    r"\bprintf\b.*\|\s*(sh|bash)\b",
    r"\becho\b.*\|\s*(sh|bash)\b",
    r"\bcat\b.*\|\s*\b(sh|bash)\b",
    # eval with command substitution (obfuscation wrapper)
    r"\beval\b\s+\"?\$\(",
    # bash/sh -c with embedded empty quotes (string splitting obfuscation)
    r"""\b(bash|sh)\s+-c\s+.*(?:'{2}|"{2})""",
    # xargs piped to destructive commands
    r"\bxargs\b.*\brm\b",
    r"\bxargs\b.*\bdel\b",
    # rsync with --delete (can wipe destination)
    r"\brsync\b.*--delete\b",
    # truncate (can zero out files)
    r"\btruncate\b",
    # crontab (persistence mechanism)
    r"\bcrontab\b",
]
_BLOCKED_RE = [re.compile(p, re.IGNORECASE | re.MULTILINE) for p in _BLOCKED_PATTERNS]

# TIER 3: Allowed but logged for audit trail
_LOGGED_PATTERNS = [
    (re.compile(r"\brm\s", re.IGNORECASE), "file deletion"),
    (re.compile(r"\bchmod\b|\bchown\b", re.IGNORECASE), "permission change"),
    (re.compile(r"\bgit\s+push\b", re.IGNORECASE), "git push"),
    (re.compile(r"\bsystemctl\b|\blaunchctl\b", re.IGNORECASE), "service management"),
    (re.compile(r"\bcurl\b|\bwget\b", re.IGNORECASE), "network download"),
    (re.compile(r"\bpip3?\s+install\b.*https?://", re.IGNORECASE), "pip install from URL"),
    (re.compile(r"\bfind\b", re.IGNORECASE), "find command"),
    (re.compile(r"\bln\b", re.IGNORECASE), "symlink operation"),
    (re.compile(r"\bmv\b", re.IGNORECASE), "file move"),
    (re.compile(r"\bpython3?\s+-c\b", re.IGNORECASE), "python inline execution"),
    (re.compile(r"\beval\b", re.IGNORECASE), "eval command"),
    (re.compile(r"\bprintf\b.*\|", re.IGNORECASE), "printf pipe"),
]


def _filter_env() -> dict[str, str]:
    """Build a safe environment dict, stripping credentials.

    Strips vars in PROTECTED_ENV_KEYS (exact match) and vars whose name
    contains any substring in PROTECTED_ENV_SUBSTRINGS (e.g. KEY, TOKEN, SECRET).
    """
    env = {}
    for k, v in os.environ.items():
        if k in config.PROTECTED_ENV_KEYS:
            continue
        if any(sub in k.upper() for sub in config.PROTECTED_ENV_SUBSTRINGS):
            continue
        env[k] = v
    return env


def _check_command_safety(command: str) -> str | None:
    """Check for catastrophic commands (Tier 1). Returns error message or None.

    Also scans the content of script files when the command executes bash/sh on a file,
    preventing the bypass where dangerous patterns are hidden inside .sh files.
    """
    for pattern in _BLOCKED_RE:
        if pattern.search(command):
            return f"BLOCKED: Catastrophic command pattern '{pattern.pattern}'. Refusing to execute."

    # If command executes a shell script file, scan its content
    try:
        parts = shlex.split(command)
        if len(parts) >= 2 and parts[0] in ("bash", "sh", "zsh", "/bin/bash", "/bin/sh", "/bin/zsh"):
            script_path = Path(parts[-1])
            if script_path.exists() and script_path.is_file():
                content = script_path.read_text(errors="replace")
                content_check = _check_shell_safety(content)
                if content_check:
                    return f"BLOCKED: Script file '{script_path.name}' contains dangerous patterns. {content_check}"
        # Also catch: source script.sh, . script.sh
        if len(parts) >= 2 and parts[0] in ("source", "."):
            script_path = Path(parts[1])
            if script_path.exists() and script_path.is_file():
                content = script_path.read_text(errors="replace")
                content_check = _check_shell_safety(content)
                if content_check:
                    return f"BLOCKED: Sourced file '{script_path.name}' contains dangerous patterns. {content_check}"
    except (ValueError, OSError):
        pass  # shlex parse failure or file read failure — continue

    # Log Tier 3 operations for audit trail
    for pattern, label in _LOGGED_PATTERNS:
        if pattern.search(command):
            logger.info("AUDIT: %s command detected: %s", label, command[:200])
    return None


# TIER 4: Code content patterns — scans Python code for dangerous operations
# Defense-in-depth for subprocess mode; not applied in Docker mode (filesystem isolation)
_CODE_BLOCKED_PATTERNS = [
    # Reading SSH keys, GPG keys, credentials (quoted paths)
    (re.compile(r"""['"]~/?\.(ssh|gnupg|aws|kube|docker)/""", re.IGNORECASE), "credential directory access"),
    (re.compile(r"""['"].*\.env['"]"""), ".env file access"),
    (re.compile(r"""['"].*\.pem['"]"""), "PEM key file access"),
    (re.compile(r"""['"].*id_rsa['"]"""), "SSH key access"),
    # Reading credentials via unquoted path construction (S-3: bypasses quoted patterns)
    (re.compile(r"Path\.home\(\).*\.(ssh|gnupg|aws|kube|docker)", re.IGNORECASE), "credential directory via Path.home()"),
    (re.compile(r"expanduser\(.*\.(ssh|gnupg|aws|kube|docker)", re.IGNORECASE), "credential directory via expanduser"),
    (re.compile(r"Path\.home\(\).*\.env\b", re.IGNORECASE), ".env file via Path.home()"),
    # Dynamic import bypass for sandbox evasion (S-2)
    (re.compile(r"\b__import__\s*\(", re.IGNORECASE), "dynamic __import__ call"),
    # importlib.import_module — moved to AST-based _is_safe_importlib() check in _check_code_safety()
    # Dangerous direct shell execution via os module
    (re.compile(r"\bos\.system\s*\(", re.IGNORECASE), "direct shell execution via os"),
    # shutil.rmtree on home or root
    (re.compile(r"shutil\.rmtree\s*\(\s*['\"]?(/|~|Path\.home)", re.IGNORECASE), "recursive delete of home/root"),
    # Reverse shells — legitimate HTTP uses requests/httpx, not raw sockets
    (re.compile(r"socket\..*connect\s*\(", re.IGNORECASE), "outbound socket connection"),
    # Reading /etc/passwd, /etc/shadow
    (re.compile(r"""open\s*\(\s*['"]/etc/(passwd|shadow|sudoers)""", re.IGNORECASE), "system file read"),
    # A-2: Block import of config module (exposes API keys at runtime)
    (re.compile(r"\bimport\s+config\b"), "config module import (credential exposure)"),
    (re.compile(r"\bfrom\s+config\s+import\b"), "config module import (credential exposure)"),
    # A-3: os.popen -- shell access not covered by os.system block
    (re.compile(r"\bos\.popen\s*\(", re.IGNORECASE), "shell via os.popen"),
    # A-4: dynamic code bypasses all static scanning
    (re.compile(r"(?<!\w)exec\s*\("), "dynamic code via exec()"),
    (re.compile(r"(?<!\w)eval\s*\("), "dynamic code via eval()"),
    # A-5: subprocess.* — moved to AST-based _is_safe_subprocess() check in _check_code_safety()
    # A-6: Runtime string construction / obfuscation
    (re.compile(r"\bgetattr\s*\(\s*os\b"), "getattr on os module"),
    (re.compile(r"\bbase64\.\w*decode\s*\("), "base64 decode"),
    (re.compile(r"\bctypes\b"), "ctypes access"),
    (re.compile(r"chr\(\d+\)\s*\+\s*chr\(\d+\)\s*\+\s*chr\(\d+\)"), "chr() chain obfuscation"),
]


def _try_fold_value(node: ast.expr) -> str | None:
    """Extract string value from a constant or nested BinOp."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp):
        return _try_fold_concat(node)
    return None


def _try_fold_concat(node: ast.BinOp) -> str | None:
    """Recursively fold a chain of string additions."""
    if isinstance(node.op, ast.Add):
        left = _try_fold_value(node.left)
        right = _try_fold_value(node.right)
        if isinstance(left, str) and isinstance(right, str):
            return left + right
    return None


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


# 6A: Safe subprocess commands — AST-inspected instead of blanket blocking
_SUBPROCESS_SAFE_CMDS = {"pip", "pip3", "python", "python3", "ollama", "git",
                         "ls", "cat", "echo", "npm", "node", "head", "tail", "wc"}
_SUBPROCESS_DANGEROUS_ARGS = {
    "git": {"push", "push --force", "remote"},
    "rm": set(),  # rm not in safe list, but belt-and-braces
}


def _is_safe_subprocess(code: str) -> bool:
    """True if ALL subprocess calls use commands from the safe list.

    Also checks secondary arguments: git is safe but git push is audit-logged.

    Args:
        code: Python source code to inspect.

    Returns:
        True if all subprocess calls are safe, False otherwise.
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
            return False  # Non-string command element
        return False  # Dynamic command — can't verify
    return True


# Known-safe modules for importlib.import_module() — stdlib introspection set
_IMPORTLIB_SAFE_MODULES = frozenset({
    "sys", "os", "math", "json", "re", "datetime", "pathlib", "collections",
    "itertools", "functools", "typing", "abc", "io", "string", "textwrap",
    "copy", "pprint", "enum", "dataclasses", "decimal", "fractions",
    "statistics", "random", "hashlib", "hmac", "secrets", "struct",
    "codecs", "unicodedata", "difflib", "csv", "html", "xml",
    "urllib", "http", "email", "logging", "warnings", "contextlib",
    "inspect", "dis", "ast", "token", "tokenize", "types", "builtins",
    "importlib", "pkgutil", "platform", "sysconfig", "time", "calendar",
    "operator", "numbers",
})


def _is_safe_importlib(code: str) -> bool:
    """True if ALL importlib.import_module() calls use known-safe module names.

    Uses AST parsing to extract the first argument of each call. Only allows
    string literals from the safe set. Dynamic arguments (variables, f-strings)
    are rejected.

    Args:
        code: Python source code to inspect.

    Returns:
        True if all importlib calls are safe, False if any are unsafe or unparseable.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Match importlib.import_module(...)
        if not (isinstance(func, ast.Attribute)
                and func.attr == "import_module"
                and isinstance(func.value, ast.Name)
                and func.value.id == "importlib"):
            continue
        # Must have at least one argument
        if not node.args:
            return False
        first_arg = node.args[0]
        # Only allow string literal arguments
        if not (isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str)):
            return False
        # Check the module name against safe list (base module only)
        module_base = first_arg.value.split(".")[0]
        if module_base not in _IMPORTLIB_SAFE_MODULES:
            return False
    return True


def _is_safe_shutil_rmtree(code: str) -> bool:
    """True if ALL shutil.rmtree() calls use safe (non-home, non-root) targets.

    Blocks:
    - Any shutil.rmtree with a dynamic/variable argument (can't verify target)
    - Any shutil.rmtree with a string literal starting with /, ~, or containing home
    - Any shutil.rmtree with os.path.expanduser or Path.home() in the argument tree
    - Current/parent directory wipes (., .., ../*)

    Args:
        code: Python source code to inspect.

    Returns:
        True if all calls are safe, False if any are unsafe or unparseable.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Match shutil.rmtree(...)
        if not (isinstance(func, ast.Attribute)
                and func.attr == "rmtree"
                and isinstance(func.value, ast.Name)
                and func.value.id == "shutil"):
            continue
        if not node.args:
            return False  # No argument — can't verify

        first_arg = node.args[0]

        # Block variable arguments (can't verify target at scan time)
        if isinstance(first_arg, ast.Name):
            return False

        # Block string literals pointing to dangerous paths
        if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
            path_val = first_arg.value.strip()
            if path_val.startswith(("/", "~")) or "home" in path_val.lower():
                return False
            # Block current/parent directory wipes
            if path_val in (".", "..") or path_val.startswith(".."):
                return False

        # Block any call expression as argument (expanduser, Path.home(), etc.)
        if isinstance(first_arg, ast.Call):
            return False

        # Block Path operations: Path.home() / "x", Path("/") / "x"
        if isinstance(first_arg, ast.BinOp):
            return False  # Path division — can't verify safely

    return True


def _check_code_safety(code: str) -> str | None:
    """Scan Python code content for dangerous operations. Returns error message or None.

    Defense-in-depth for the subprocess execution path. NOT a security boundary.
    Not applied in Docker mode where filesystem isolation provides stronger protection.

    Also scans the full code text against Tier 1 shell blocklist patterns. If "sudo",
    "cat|bash", "rm -rf /", etc. appear ANYWHERE in generated Python code — in strings,
    comments, or code — it is blocked. False positives are acceptable for Tier 1
    catastrophic patterns since they should never appear in legitimate generated code.
    """
    for pattern, label in _CODE_BLOCKED_PATTERNS:
        if pattern.search(code):
            return f"BLOCKED: Code contains {label}. Refusing to execute in subprocess mode."

    # 6A: Smart subprocess check — AST-inspect arguments instead of blanket block
    if re.search(r"\bsubprocess\.\w+\s*\(", code):
        if not _is_safe_subprocess(code):
            return "BLOCKED: Code contains subprocess call with unsafe or dynamic command."

    # importlib.import_module — AST-based check (safe modules allowed, config/dotenv blocked)
    if re.search(r"\bimportlib\s*\.\s*import_module\s*\(", code, re.IGNORECASE):
        if not _is_safe_importlib(code):
            return "BLOCKED: importlib.import_module with unsafe or dynamic module name"

    # AST-based shutil.rmtree check — catches variable indirection and function calls
    if "shutil" in code and "rmtree" in code:
        if not _is_safe_shutil_rmtree(code):
            return "BLOCKED: shutil.rmtree with unsafe or unverifiable target path"

    # Scan full code text against Tier 1 shell blocklist.
    # Catches Python that writes .sh files with dangerous content (cat|bash, sudo),
    # embeds dangerous commands in subprocess calls, or references blocked patterns
    # in any context. Tier 1 patterns are catastrophic — false positives acceptable.
    #
    # Also expand escape sequences (\n, \t) so regex word boundaries work across
    # string literal line breaks (e.g. "#!/bin/bash\ncat x | bash" → actual newline).
    expanded = code.replace("\\n", "\n").replace("\\t", "\t")
    for blocked in _BLOCKED_RE:
        if blocked.search(code) or blocked.search(expanded):
            return (
                f"BLOCKED: Code contains shell pattern matching "
                f"'{blocked.pattern}'. Refusing to execute."
            )

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
    return None


# TIER 5: JavaScript-specific dangerous patterns (S-1)
_JS_BLOCKED_PATTERNS = [
    (re.compile(r"""\brequire\s*\(\s*['"]child_process['"]""", re.IGNORECASE), "child_process access"),
    (re.compile(r"\bexecSync\s*\(", re.IGNORECASE), "synchronous shell exec"),
    (re.compile(r"\bspawn(?:Sync)?\s*\(\s*['\"](?:bash|sh|rm|sudo)", re.IGNORECASE), "dangerous spawn"),
    (re.compile(r"\bprocess\.env\b", re.IGNORECASE), "environment variable access"),
    (re.compile(r"\bfs\.(?:unlink|rmdir|rm)\w*\b", re.IGNORECASE), "filesystem deletion"),
    (re.compile(r"\beval\s*\(", re.IGNORECASE), "eval execution"),
    (re.compile(r"""\brequire\s*\(\s*['"]fs['"]\s*\)\s*\.\s*(?:unlink|rmdir|rm|writeFile)\w*""", re.IGNORECASE), "chained fs destructive call"),
]


def _check_js_safety(code: str) -> str | None:
    """Scan JavaScript code for dangerous operations.

    Checks JS-specific patterns (child_process, eval, fs deletion) and also
    runs the full Tier 1 shell blocklist against the code text.
    """
    # Tier 1 shell patterns — catastrophic commands should never appear in JS
    for pattern in _BLOCKED_RE:
        if pattern.search(code):
            return f"BLOCKED: JavaScript contains shell pattern '{pattern.pattern}'."
    # JS-specific patterns
    for pattern, label in _JS_BLOCKED_PATTERNS:
        if pattern.search(code):
            return f"BLOCKED: JavaScript contains {label}."
    return None


def _check_shell_safety(code: str) -> str | None:
    """Scan bash script content for Tier 1 blocked patterns.

    Applies the same blocklist used by _check_command_safety() but against the
    full script body, not just the wrapper command. This catches dangerous patterns
    inside heredocs, multi-line scripts, and .sh files that would otherwise bypass
    the command-level check.
    """
    for pattern in _BLOCKED_RE:
        if pattern.search(code):
            return f"BLOCKED: Shell script contains catastrophic pattern '{pattern.pattern}'."
    return None


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


def _validate_working_dir(working_dir: Path) -> str | None:
    """Validate that working_dir is within HOST_HOME."""
    try:
        working_dir.resolve().relative_to(config.HOST_HOME.resolve())
        return None
    except ValueError:
        return f"BLOCKED: Working directory {working_dir} is outside HOME ({config.HOST_HOME})"


# ── Docker container isolation ─────────────────────────────────────


def _docker_available() -> bool:
    """Check if Docker daemon is running and sandbox image exists. Cached for 60s."""
    now = time.time()
    if now - _docker_status["checked_at"] < 60:
        return _docker_status["available"]

    # Fast-fail: check Docker socket exists before spawning subprocess.
    # Skip if DOCKER_HOST is set (remote daemon via TCP/SSH — no local socket).
    if not os.environ.get("DOCKER_HOST"):
        _docker_sock = Path("/var/run/docker.sock")
        _docker_sock_home = Path.home() / ".docker" / "run" / "docker.sock"
        if not _docker_sock.exists() and not _docker_sock_home.exists():
            _docker_status.update(available=False, checked_at=now)
            logger.warning("Docker socket not found. Falling back to subprocess execution.")
            return False

    try:
        result = subprocess.run(
            ["docker", "info"], stdin=subprocess.DEVNULL, capture_output=True, timeout=5,
        )
        if result.returncode != 0:
            _docker_status.update(available=False, checked_at=now)
            logger.warning("Docker daemon not running. Falling back to subprocess execution.")
            return False

        result = subprocess.run(
            ["docker", "image", "inspect", config.DOCKER_IMAGE],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=5,
        )
        if result.returncode != 0:
            _docker_status.update(available=False, checked_at=now)
            logger.warning(
                "Docker is running but '%s' image not found. "
                "Run: scripts/build_sandbox.sh",
                config.DOCKER_IMAGE,
            )
            return False

        _docker_status.update(available=True, checked_at=now)
        return True
    except FileNotFoundError:
        _docker_status.update(available=False, checked_at=now)
        logger.warning("Docker not installed. Falling back to subprocess execution.")
        return False
    except subprocess.TimeoutExpired:
        _docker_status.update(available=False, checked_at=now)
        logger.warning("Docker check timed out. Falling back to subprocess execution.")
        return False


def _build_docker_cmd(
    container_name: str,
    working_dir: Path,
    script_path_in_container: str,
    language: str,
) -> list[str]:
    """Build the docker run command list. Pure function for testability."""
    uid = os.getuid()
    gid = os.getgid()

    cmd = [
        "docker", "run",
        "--name", container_name,
        "--rm",
        # Mount working directory at same host path (read-write for outputs)
        "-v", f"{working_dir}:{working_dir}",
        # Mount uploads directory at same host path (read-only)
        "-v", f"{config.UPLOADS_DIR}:{config.UPLOADS_DIR}:ro",
        # Mount pip cache for auto-install persistence
        "-v", f"{config.DOCKER_PIP_CACHE}:/pip-cache",
        # Environment for pip cache
        "-e", "PIP_TARGET=/pip-cache",
        "-e", "PYTHONPATH=/pip-cache",
        # Resource limits
        "--memory", config.DOCKER_MEMORY_LIMIT,
        "--cpus", str(config.DOCKER_CPU_LIMIT),
        # Network mode
        "--network", config.DOCKER_NETWORK,
        # Run as host user to preserve file ownership
        "--user", f"{uid}:{gid}",
        # Security hardening
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--pids-limit", "256",
        # Working directory inside container
        "-w", str(working_dir),
        # Image
        config.DOCKER_IMAGE,
    ]

    # Interpreter command
    if language == "python":
        cmd.extend(["python3", "-u", script_path_in_container])
    elif language == "javascript":
        cmd.extend(["node", script_path_in_container])
    elif language == "bash":
        cmd.extend(["bash", "-e", script_path_in_container])
    else:
        cmd.extend(["python3", "-u", script_path_in_container])

    return cmd


def _run_code_docker(
    code: str,
    language: str,
    timeout: int,
    working_dir: Path,
) -> ExecutionResult:
    """Execute code inside a Docker container with filesystem isolation.

    Only working_dir (rw) and uploads_dir (ro) are mounted.
    The host filesystem, SSH keys, .env, and all other files are inaccessible.
    """
    # Defense-in-depth: validate working_dir even in Docker path,
    # since it gets mounted read-write into the container.
    safety_msg = _validate_working_dir(working_dir)
    if safety_msg:
        return ExecutionResult(success=False, stderr=safety_msg)

    working_dir.mkdir(parents=True, exist_ok=True)
    existing_mtimes = _snapshot_mtimes(working_dir)

    suffix = {"python": ".py", "javascript": ".js", "bash": ".sh"}.get(language, ".py")
    container_name = f"agentsutra-{uuid.uuid4().hex[:12]}"
    script_path = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=suffix, dir=working_dir, delete=False,
        ) as f:
            f.write(code)
            script_path = Path(f.name)

        cmd = _build_docker_cmd(
            container_name, working_dir, str(script_path), language,
        )

        logger.info(
            "Docker exec: %s code (timeout=%ds, cwd=%s, container=%s, network=%s)",
            language, timeout, working_dir, container_name, config.DOCKER_NETWORK,
        )

        result = subprocess.run(
            cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout,
        )

        new_files = _detect_artifacts(
            existing_mtimes, working_dir, result.stdout or "", result.returncode,
            exclude_path=script_path,
        )
        tb = _extract_traceback(result.stderr) if result.returncode != 0 else ""

        return ExecutionResult(
            success=result.returncode == 0,
            stdout=result.stdout[:50000],
            stderr=result.stderr[:20000],
            traceback=tb,
            files_created=new_files,
            return_code=result.returncode,
        )

    except subprocess.TimeoutExpired:
        logger.warning(
            "Docker execution timed out after %ds, killing container %s",
            timeout, container_name,
        )
        subprocess.run(["docker", "kill", container_name], stdin=subprocess.DEVNULL, capture_output=True, timeout=5)
        subprocess.run(["docker", "rm", "-f", container_name], stdin=subprocess.DEVNULL, capture_output=True, timeout=5)
        return ExecutionResult(
            success=False, stderr=f"Execution timed out after {timeout}s", timed_out=True,
        )
    except Exception as e:
        logger.error("Docker execution error: %s", e)
        subprocess.run(["docker", "rm", "-f", container_name], stdin=subprocess.DEVNULL, capture_output=True, timeout=5)
        return ExecutionResult(success=False, stderr=str(e))
    finally:
        if script_path is not None and script_path.exists():
            script_path.unlink()


def _docker_pip_install(package: str) -> ExecutionResult:
    """Install a pip package into the shared Docker pip cache volume.

    Serialized via _docker_pip_lock to prevent .pip-cache corruption
    when multiple concurrent tasks trigger auto-install simultaneously.
    """
    with _docker_pip_lock:
        container_name = f"agentsutra-pip-{uuid.uuid4().hex[:8]}"
        cmd = [
            "docker", "run",
            "--name", container_name,
            "--rm",
            "-v", f"{config.DOCKER_PIP_CACHE}:/pip-cache",
            "-e", "PIP_TARGET=/pip-cache",
            "--network", config.DOCKER_NETWORK,
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", "256",
            config.DOCKER_IMAGE,
            "pip", "install", package,
        ]

        try:
            result = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
            return ExecutionResult(
                success=result.returncode == 0,
                stdout=result.stdout[:10000],
                stderr=result.stderr[:5000],
                return_code=result.returncode,
            )
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "kill", container_name], stdin=subprocess.DEVNULL, capture_output=True, timeout=5)
            subprocess.run(["docker", "rm", "-f", container_name], stdin=subprocess.DEVNULL, capture_output=True, timeout=5)
            return ExecutionResult(success=False, stderr="pip install timed out", timed_out=True)
        except Exception as e:
            return ExecutionResult(success=False, stderr=str(e))


# ── Artifact detection and filtering ─────────────────────────────

# Directories that should NEVER be scanned for artifacts
_EXCLUDED_DIR_NAMES = frozenset({
    # Python virtual environments
    ".venv", "venv", "env", "virtualenv",
    "site-packages", "dist-packages",
    # Python internals
    "__pycache__", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".tox", ".nox",
    # Node.js
    "node_modules",
    # Version control
    ".git", ".svn", ".hg",
    # IDE and OS
    ".idea", ".vscode",
    # Build artifacts
    "__pypackages__",
    # Pip/package management
    ".pip-cache", "pip-cache",
})

# Exact filenames that are NEVER user artifacts
_EXCLUDED_FILENAMES = frozenset({
    # Virtual environment markers
    "pyvenv.cfg",
    # Shell activation scripts
    "activate", "activate.csh", "activate.fish",
    "activate.nu", "activate.ps1", "Activate.ps1",
    "activate_this.py", "deactivate.nu",
    # Pip wrapper scripts (no extension)
    "pip", "pip3", "pip3.11", "pip3.12", "pip3.13",
    "wheel", "easy_install",
    # Package metadata files
    "RECORD", "WHEEL", "METADATA", "INSTALLER",
    "entry_points.txt", "top_level.txt", "direct_url.json",
    "PKG-INFO",
    # OS metadata
    ".DS_Store", "Thumbs.db", "desktop.ini",
    # Python
    ".coverage", ".python-version",
    # Package manager locks
    "package-lock.json", "yarn.lock", "poetry.lock",
    "Pipfile.lock", "pdm.lock",
})

# File extensions that are NEVER user artifacts
_EXCLUDED_EXTENSIONS = frozenset({
    ".pyc", ".pyo", ".pyd",       # Python compiled
    ".so", ".dylib", ".dll",       # Shared libraries
    ".o", ".a", ".lib",            # Object/static libraries
    ".h", ".hpp", ".hh",           # C/C++ headers
    ".whl",                        # Wheel packages
    ".egg",                        # Egg packages
})


def _is_artifact_file(path: Path) -> bool:
    """Return True if a file is a genuine output artifact (not infrastructure/cache/metadata).

    Multi-layer filter:
    1. Exclude files inside known infrastructure directories
    2. Exclude files matching known non-artifact filenames
    3. Exclude files with known non-artifact extensions
    """
    # Layer 1: Directory-level exclusions
    for part in path.parts:
        if part in _EXCLUDED_DIR_NAMES:
            return False
        if part.endswith(".dist-info") or part.endswith(".egg-info"):
            return False

    name = path.name

    # Layer 2: Exact filename matches
    if name in _EXCLUDED_FILENAMES:
        return False

    # Layer 3: Extension-based exclusions
    suffix = path.suffix.lower()
    if suffix in _EXCLUDED_EXTENSIONS:
        return False

    return True


def _walk_artifacts(directory: Path) -> list[Path]:
    """Walk directory for artifact files, pruning excluded directory trees.

    Unlike rglob('*'), this skips entire subtrees that contain
    only infrastructure files (venvs, site-packages, node_modules, etc.).
    Also skips empty files (0 bytes).
    """
    artifacts = []
    for root, dirs, files in os.walk(directory):
        # Prune excluded directories IN-PLACE (prevents os.walk from descending)
        dirs[:] = [
            d for d in dirs
            if d not in _EXCLUDED_DIR_NAMES
            and not d.endswith(".dist-info")
            and not d.endswith(".egg-info")
        ]
        for fname in files:
            fpath = Path(root) / fname
            if _is_artifact_file(fpath):
                try:
                    if fpath.stat().st_size > 0:
                        artifacts.append(fpath)
                except OSError:
                    pass
    return artifacts


# Maximum artifacts before triggering safety filter
_MAX_EXPECTED_ARTIFACTS = 20

# Common output file extensions for stdout fallback and sanity filtering
_OUTPUT_EXTENSIONS = {
    ".html", ".pdf", ".csv", ".xlsx", ".xls", ".json", ".xml",
    ".png", ".jpg", ".jpeg", ".gif", ".svg",
    ".txt", ".md", ".zip", ".tar", ".gz",
    ".py", ".js", ".css", ".parquet",
}


def _apply_artifact_sanity_check(new_files: list[str], working_dir: Path) -> list[str]:
    """If too many artifacts detected, filter to known output extensions only."""
    if len(new_files) <= _MAX_EXPECTED_ARTIFACTS:
        return new_files
    logger.warning(
        "Excessive artifacts detected (%d files) in %s — possible venv/package leak. "
        "Filtering to known output extensions only.",
        len(new_files), working_dir,
    )
    filtered = [f for f in new_files if Path(f).suffix.lower() in _OUTPUT_EXTENSIONS]
    if filtered:
        logger.info("Filtered to %d files with output extensions: %s",
                     len(filtered), [Path(f).name for f in filtered])
        return filtered
    logger.warning("No files matched output extensions after filtering")
    return new_files  # Return originals if filtering removes everything


def _snapshot_mtimes(working_dir: Path) -> dict[Path, float]:
    """Snapshot path→mtime for all artifact files in working_dir.

    Called before execution to establish a baseline. After execution,
    _detect_artifacts() compares against this snapshot to find new/modified files.
    """
    mtimes: dict[Path, float] = {}
    if working_dir.exists():
        for f in _walk_artifacts(working_dir):
            try:
                mtimes[f] = f.stat().st_mtime
            except OSError:
                pass
    return mtimes


def _detect_artifacts(
    existing_mtimes: dict[Path, float],
    working_dir: Path,
    stdout: str,
    returncode: int,
    exclude_path: Path | None = None,
) -> list[str]:
    """Detect new/modified artifact files after code execution.

    Compares current state against a pre-execution mtime snapshot.
    Falls back to parsing stdout for file paths if mtime finds nothing.
    Applies a sanity check to filter excessive results (venv/package leaks).

    Args:
        existing_mtimes: Snapshot from _snapshot_mtimes() before execution.
        working_dir: Directory to scan for artifacts.
        stdout: Execution stdout (used for fallback path extraction).
        returncode: Process return code (fallback only runs on success).
        exclude_path: Optional path to exclude (e.g. the temp script file).
    """
    new_files = []
    for f in _walk_artifacts(working_dir):
        if f == exclude_path:
            continue
        try:
            prev_mtime = existing_mtimes.get(f)
            if prev_mtime is None or f.stat().st_mtime > prev_mtime:
                new_files.append(str(f))
        except OSError:
            pass

    # Fallback: if mtime found nothing but execution succeeded, parse stdout for file paths
    if not new_files and returncode == 0 and stdout:
        new_files = _extract_paths_from_stdout(stdout, working_dir)
        if new_files:
            logger.info("Artifacts detected via stdout fallback: %s", [Path(f).name for f in new_files])

    # Sanity check: too many artifacts likely indicates venv/package leak
    new_files = _apply_artifact_sanity_check(new_files, working_dir)

    if new_files:
        logger.info("Artifacts detected: %s", [Path(f).name for f in new_files])
    else:
        logger.warning("No artifacts detected in %s (%d files scanned)", working_dir, len(existing_mtimes))

    return new_files


def _extract_paths_from_stdout(stdout: str, working_dir: Path) -> list[str]:
    """Extract file paths mentioned in stdout that exist on disk.

    Universal fallback when mtime-based detection finds 0 files.
    Looks for absolute paths and relative paths (resolved against working_dir)
    that actually exist and have a recognized output extension.
    """
    if not stdout:
        return []

    found = []
    seen = set()

    # 1. Match absolute paths (any Unix-style path starting with /)
    for match in re.finditer(r'(/[^\s:,\'">\]]+)', stdout):
        candidate = match.group(0).rstrip('.,;:)]\'"')
        p = Path(candidate)
        if p.suffix.lower() in _OUTPUT_EXTENSIONS and p.is_file() and _is_artifact_file(p):
            resolved = str(p.resolve())
            if resolved not in seen:
                seen.add(resolved)
                found.append(str(p))

    # 2. Match relative paths with output extensions (resolve against working_dir)
    ext_pattern = '|'.join(ext.lstrip('.') for ext in _OUTPUT_EXTENSIONS)
    for match in re.finditer(r'([\w./\\-]+\.(?:' + ext_pattern + r'))\b', stdout):
        candidate = match.group(1)
        if candidate.startswith('/'):
            continue  # Already handled above
        p = (working_dir / candidate).resolve()
        if p.is_file() and _is_artifact_file(p):
            resolved = str(p)
            if resolved not in seen:
                seen.add(resolved)
                found.append(str(p))

    return found


@dataclass
class ExecutionResult:
    success: bool
    stdout: str = ""
    stderr: str = ""
    traceback: str = ""
    files_created: list[str] = field(default_factory=list)
    timed_out: bool = False
    return_code: int = -1
    auto_installed: list[str] = field(default_factory=list)


def run_code(
    code: str,
    language: str = "python",
    timeout: int | None = None,
    working_dir: Path | None = None,
    venv_path: str | None = None,
    task_id: str = "",
) -> ExecutionResult:
    """Execute generated code in a subprocess or Docker container.

    When DOCKER_ENABLED is True and Docker is available, code runs inside
    an isolated container with only workspace directories mounted.
    Otherwise falls back to direct subprocess execution.
    """
    timeout = timeout or config.EXECUTION_TIMEOUT
    working_dir = working_dir or config.OUTPUTS_DIR

    # Docker isolation path (venv_path ignored — container has its own env)
    if config.DOCKER_ENABLED and _docker_available():
        return _run_code_docker(code, language, timeout, working_dir)

    # --- Subprocess path (original behavior) ---
    # Universal Tier 1 scan: catastrophic patterns blocked in ALL languages.
    # These patterns (sudo, rm -rf ~, cat|bash, etc.) should never appear in
    # generated code regardless of language, even inside string literals.
    for pattern in _BLOCKED_RE:
        if pattern.search(code):
            logger.warning("Universal Tier 1 scan blocked %s code: pattern '%s'", language, pattern.pattern)
            return ExecutionResult(
                success=False,
                stderr=f"BLOCKED: Generated code contains catastrophic pattern '{pattern.pattern}'. Refusing to execute.",
            )

    # Language-specific content scan (defense-in-depth, not a security boundary)
    if language == "python":
        safety_msg = _check_code_safety(code)
    elif language == "bash":
        safety_msg = _check_shell_safety(code)
    elif language == "javascript":
        safety_msg = _check_js_safety(code)
    else:
        # WARNING: any new language added here MUST have a content scanner
        safety_msg = None
    if safety_msg:
        logger.warning("Code content blocked: %s", safety_msg)
        return ExecutionResult(success=False, stderr=safety_msg)

    safety_msg = _validate_working_dir(working_dir)
    if safety_msg:
        return ExecutionResult(success=False, stderr=safety_msg)

    working_dir.mkdir(parents=True, exist_ok=True)
    existing_mtimes = _snapshot_mtimes(working_dir)

    suffix = {"python": ".py", "javascript": ".js", "bash": ".sh"}.get(language, ".py")

    script_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=suffix, dir=working_dir, delete=False,
        ) as f:
            f.write(code)
            script_path = Path(f.name)

        # S-9: Interpreter is hardcoded — never from LLM output. Safe.
        if language == "python":
            python_bin = f"{venv_path}/bin/python3" if venv_path else "python3"
            cmd = [python_bin, "-u", str(script_path)]
        elif language == "javascript":
            cmd = ["node", str(script_path)]
        elif language == "bash":
            cmd = ["bash", "-e", str(script_path)]
        else:
            cmd = ["python3", "-u", str(script_path)]

        logger.info("Executing %s code (timeout=%ds, cwd=%s)", language, timeout, working_dir)

        env = _filter_env()

        if task_id:
            _register_live_output(task_id)
        proc = None
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=working_dir, env=env, start_new_session=True,
            )

            stdout_chunks: list[str] = []
            stderr_chunks: list[str] = []

            def _read_stdout():
                for raw_line in proc.stdout:
                    line = raw_line.decode(errors="replace").rstrip()
                    stdout_chunks.append(line)
                    if task_id:
                        _append_live_output(task_id, line)

            def _read_stderr():
                for raw_line in proc.stderr:
                    stderr_chunks.append(raw_line.decode(errors="replace").rstrip())

            t_out = threading.Thread(target=_read_stdout, daemon=True)
            t_err = threading.Thread(target=_read_stderr, daemon=True)
            t_out.start()
            t_err.start()

            deadline = time.time() + timeout
            while proc.poll() is None:
                if time.time() > deadline:
                    import signal
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                    # R-2: Close pipes so reader threads unblock from readline()
                    if proc.stdout:
                        proc.stdout.close()
                    if proc.stderr:
                        proc.stderr.close()
                    t_out.join(timeout=2)
                    t_err.join(timeout=2)
                    logger.warning("Execution timed out after %ds, killed process group %d", timeout, proc.pid)
                    return ExecutionResult(success=False, stderr=f"Execution timed out after {timeout}s", timed_out=True)
                time.sleep(0.1)

            t_out.join(timeout=5)
            t_err.join(timeout=5)

            stdout = "\n".join(stdout_chunks)
            stderr = "\n".join(stderr_chunks)

            new_files = _detect_artifacts(
                existing_mtimes, working_dir, stdout, proc.returncode,
                exclude_path=script_path,
            )
            tb = _extract_traceback(stderr) if proc.returncode != 0 else ""

            return ExecutionResult(
                success=proc.returncode == 0,
                stdout=stdout[:50000],
                stderr=stderr[:20000],
                traceback=tb,
                files_created=new_files,
                return_code=proc.returncode,
            )
        finally:
            # R-3: Ensure subprocess is killed if still running on error
            if proc is not None and proc.poll() is None:
                try:
                    import signal
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=5)
                except Exception:
                    pass
            if task_id:
                _clear_live_output(task_id)
    except Exception as e:
        logger.error("Execution error: %s", e)
        return ExecutionResult(success=False, stderr=str(e))
    finally:
        if script_path is not None and script_path.exists():
            script_path.unlink()


def run_code_with_auto_install(
    code: str,
    language: str = "python",
    timeout: int | None = None,
    working_dir: Path | None = None,
    venv_path: str | None = None,
    max_install_retries: int = 2,
    task_id: str = "",
) -> ExecutionResult:
    """Execute code with automatic pip install on ImportError.

    If execution fails with ImportError/ModuleNotFoundError, parses the missing
    module name, runs pip install, and retries. Up to max_install_retries attempts.
    In Docker mode, pip install runs inside a container targeting the shared cache.
    """
    auto_installed = []
    use_docker = config.DOCKER_ENABLED and _docker_available()

    for attempt in range(max_install_retries + 1):
        result = run_code(code, language, timeout, working_dir, venv_path, task_id=task_id)

        if result.success:
            if auto_installed:
                result.stdout += f"\n[Auto-installed: {', '.join(auto_installed)}]"
            result.auto_installed = auto_installed
            return result

        missing = _parse_import_error(result.traceback or result.stderr)
        if not missing or attempt >= max_install_retries:
            result.auto_installed = auto_installed
            return result

        logger.info("Auto-installing missing module: %s (attempt %d)", missing, attempt + 1)

        if use_docker:
            install_result = _docker_pip_install(missing)
        else:
            pip_bin = f"{venv_path}/bin/pip" if venv_path else "pip3"
            # A-10: --only-binary :all: prevents setup.py execution (supply-chain vector)
            install_result = run_shell(
                f"{pip_bin} install --only-binary :all: {shlex.quote(missing)}",
                working_dir=str(working_dir or config.OUTPUTS_DIR),
                timeout=120,
            )

        if not install_result.success:
            logger.warning("Auto-install failed for %s: %s", missing, install_result.stderr[:200])
            result.auto_installed = auto_installed
            return result

        auto_installed.append(missing)
        logger.info("Auto-installed %s, retrying execution", missing)

    result.auto_installed = auto_installed
    return result


def run_shell(
    command: str,
    working_dir: str | Path,
    timeout: int | None = None,
    venv_path: str | None = None,
    env_vars: dict[str, str] | None = None,
    task_id: str = "",
) -> ExecutionResult:
    """Execute a shell command with full system access.

    Catastrophic commands (rm -rf ~, mkfs, etc.) are still blocked.
    All other commands (curl, pip, wget, ssh, etc.) are fully allowed.
    """
    timeout = timeout or config.EXECUTION_TIMEOUT
    working_dir = Path(working_dir)

    if not working_dir.exists():
        try:
            working_dir.mkdir(parents=True, exist_ok=True)
            logger.info("Created working directory: %s", working_dir)
        except OSError as e:
            return ExecutionResult(success=False, stderr=f"Cannot create working directory {working_dir}: {e}")

    safety_msg = _validate_working_dir(working_dir)
    if safety_msg:
        return ExecutionResult(success=False, stderr=safety_msg)

    # Block only catastrophic commands
    safety_warning = _check_command_safety(command)
    if safety_warning:
        logger.warning("Command blocked: %s", command[:200])
        return ExecutionResult(success=False, stderr=safety_warning)

    parts = []
    if venv_path:
        activate = Path(venv_path) / "bin" / "activate"
        if activate.exists():
            parts.append(f"source '{activate}'")
        else:
            logger.warning("Venv activate not found: %s", activate)

    parts.append(command)
    full_command = " && ".join(parts)
    # S-7: Safety check runs on `command` (line 1202), not `full_command`.
    # The only prefix added is venv activation (safe by construction).
    # If other prefixes are ever added here, they must also be checked.

    # Protected env: strip only AgentSutra's own credentials
    env = _filter_env()
    if env_vars:
        env.update(env_vars)

    existing_mtimes = _snapshot_mtimes(working_dir)

    logger.info("Shell exec: %s (cwd=%s, timeout=%ds)", command[:200], working_dir, timeout)

    if task_id:
        _register_live_output(task_id)
    proc = None
    try:
        proc = subprocess.Popen(
            full_command, shell=True, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=working_dir, env=env, start_new_session=True,
        )

        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []

        def _read_stdout():
            for raw_line in proc.stdout:
                line = raw_line.decode(errors="replace").rstrip()
                stdout_chunks.append(line)
                if task_id:
                    _append_live_output(task_id, line)

        def _read_stderr():
            for raw_line in proc.stderr:
                stderr_chunks.append(raw_line.decode(errors="replace").rstrip())

        t_out = threading.Thread(target=_read_stdout, daemon=True)
        t_err = threading.Thread(target=_read_stderr, daemon=True)
        t_out.start()
        t_err.start()

        deadline = time.time() + timeout
        while proc.poll() is None:
            if time.time() > deadline:
                import signal
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
                # R-2: Close pipes so reader threads unblock from readline()
                if proc.stdout:
                    proc.stdout.close()
                if proc.stderr:
                    proc.stderr.close()
                t_out.join(timeout=2)
                t_err.join(timeout=2)
                logger.warning("Shell command timed out after %ds, killed process group %d", timeout, proc.pid)
                return ExecutionResult(success=False, stderr=f"Timed out after {timeout}s", timed_out=True)
            time.sleep(0.1)

        t_out.join(timeout=5)
        t_err.join(timeout=5)

        stdout = "\n".join(stdout_chunks)
        stderr = "\n".join(stderr_chunks)

        new_files = _detect_artifacts(
            existing_mtimes, working_dir, stdout, proc.returncode,
        )
        tb = _extract_traceback(stderr) if proc.returncode != 0 else ""

        return ExecutionResult(
            success=proc.returncode == 0,
            stdout=stdout[:50000],
            stderr=stderr[:20000],
            traceback=tb,
            files_created=new_files,
            return_code=proc.returncode,
        )
    except Exception as e:
        logger.error("Shell execution error: %s", e)
        return ExecutionResult(success=False, stderr=str(e))
    finally:
        # R-3: Ensure subprocess is killed if still running on error
        if proc is not None and proc.poll() is None:
            try:
                import signal
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
            except Exception:
                pass
        if task_id:
            _clear_live_output(task_id)


_PIP_NAME_MAP = {
    "PIL": "Pillow",
    "cv2": "opencv-python",
    "bs4": "beautifulsoup4",
    "yaml": "pyyaml",
    "sklearn": "scikit-learn",
    "dateutil": "python-dateutil",
    "dotenv": "python-dotenv",
    "gi": "PyGObject",
    "attr": "attrs",
    "serial": "pyserial",
    "usb": "pyusb",
    "Bio": "biopython",
}


def _parse_import_error(error_text: str) -> str | None:
    """Extract missing module name from ImportError/ModuleNotFoundError.

    Maps common import-name → pip-name mismatches (e.g. PIL → Pillow).
    """
    if not error_text:
        return None
    match = re.search(r"(?:ModuleNotFoundError|ImportError): No module named '(\w+)'", error_text)
    if match:
        module = match.group(1)
        return _PIP_NAME_MAP.get(module, module)
    return None


def _extract_traceback(stderr: str) -> str:
    """Extract the Python traceback from stderr output."""
    if not stderr:
        return ""
    lines = stderr.strip().split("\n")
    tb_start = -1
    for i, line in enumerate(lines):
        if "Traceback (most recent call last):" in line:
            tb_start = i
    if tb_start == -1:
        return ""
    return "\n".join(lines[tb_start:])
