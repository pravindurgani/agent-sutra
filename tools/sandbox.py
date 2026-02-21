from __future__ import annotations

import os
import re
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

# Docker availability cache (lazy-checked, refreshed every 60s)
_docker_status: dict[str, Any] = {"available": False, "checked_at": 0.0}

# Serialize Docker pip installs to prevent .pip-cache corruption under concurrency
_docker_pip_lock = threading.Lock()

# ── Tiered command safety ────────────────────────────────────────────
# TIER 1: Catastrophic, irreversible — ALWAYS BLOCKED
_BLOCKED_PATTERNS = [
    # rm -rf targeting home, root, or user directories
    # Handles short flags (-rf), split flags (-r -f), and GNU long flags (--recursive --force)
    r"\brm\s+(-{1,2}[\w-]+\s+)*\s*(/\s*$|~\s*$|~/\s*$|\$HOME)",
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
    # Interpreter inline code execution (bypass via python -c, perl -e, etc.)
    r"\bpython3?\s+-[cE]\s",
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
    # printf/echo piped to shell (like curl|sh but via printf/echo)
    r"\bprintf\b.*\|\s*(sh|bash)\b",
    r"\becho\b.*\|\s*(sh|bash)\b",
    # eval with command substitution (obfuscation wrapper)
    r"\beval\b\s+\"?\$\(",
    # bash/sh -c with embedded empty quotes (string splitting obfuscation)
    r"""\b(bash|sh)\s+-c\s+.*(?:'{2}|"{2})""",
]
_BLOCKED_RE = [re.compile(p, re.IGNORECASE) for p in _BLOCKED_PATTERNS]

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
    """Check for catastrophic commands (Tier 1). Returns error message or None."""
    for pattern in _BLOCKED_RE:
        if pattern.search(command):
            return f"BLOCKED: Catastrophic command pattern '{pattern.pattern}'. Refusing to execute."
    # Log Tier 3 operations for audit trail
    for pattern, label in _LOGGED_PATTERNS:
        if pattern.search(command):
            logger.info("AUDIT: %s command detected: %s", label, command[:200])
    return None


# TIER 4: Code content patterns — scans Python code for dangerous operations
# Defense-in-depth for subprocess mode; not applied in Docker mode (filesystem isolation)
_CODE_BLOCKED_PATTERNS = [
    # Reading SSH keys, GPG keys, credentials
    (re.compile(r"""['"]~/?\.(ssh|gnupg|aws|kube|docker)/""", re.IGNORECASE), "credential directory access"),
    (re.compile(r"""['"].*\.env['"]"""), ".env file access"),
    (re.compile(r"""['"].*\.pem['"]"""), "PEM key file access"),
    (re.compile(r"""['"].*id_rsa['"]"""), "SSH key access"),
    # os.system — should use subprocess.run() instead
    (re.compile(r"\bos\.system\s*\(", re.IGNORECASE), "os.system call"),
    # shutil.rmtree on home or root
    (re.compile(r"shutil\.rmtree\s*\(\s*['\"]?(/|~|Path\.home)", re.IGNORECASE), "recursive delete of home/root"),
    # Reverse shells — legitimate HTTP uses requests/httpx, not raw sockets
    (re.compile(r"socket\..*connect\s*\(", re.IGNORECASE), "outbound socket connection"),
    # Reading /etc/passwd, /etc/shadow
    (re.compile(r"""open\s*\(\s*['"]/etc/(passwd|shadow|sudoers)""", re.IGNORECASE), "system file read"),
]


def _check_code_safety(code: str) -> str | None:
    """Scan Python code content for dangerous operations. Returns error message or None.

    Defense-in-depth for the subprocess execution path. NOT a security boundary.
    Not applied in Docker mode where filesystem isolation provides stronger protection.
    """
    for pattern, label in _CODE_BLOCKED_PATTERNS:
        if pattern.search(code):
            return f"BLOCKED: Code contains {label}. Refusing to execute in subprocess mode."
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

    # Track path→mtime to detect both new AND modified files (e.g. overwritten on retry)
    existing_mtimes = {}
    if working_dir.exists():
        for f in _walk_artifacts(working_dir):
            try:
                existing_mtimes[f] = f.stat().st_mtime
            except OSError:
                pass

    suffix = {"python": ".py", "javascript": ".js", "bash": ".sh"}.get(language, ".py")
    container_name = f"agentcore-{uuid.uuid4().hex[:12]}"
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

        new_files = []
        for f in _walk_artifacts(working_dir):
            if f != script_path:
                prev_mtime = existing_mtimes.get(f)
                if prev_mtime is None or f.stat().st_mtime > prev_mtime:
                    new_files.append(str(f))

        # Fallback: if mtime found nothing but execution succeeded, parse stdout for file paths
        if not new_files and result.returncode == 0 and result.stdout:
            new_files = _extract_paths_from_stdout(result.stdout, working_dir)
            if new_files:
                logger.info("Artifacts detected via stdout fallback: %s", [Path(f).name for f in new_files])

        # Sanity check: too many artifacts likely indicates venv/package leak
        new_files = _apply_artifact_sanity_check(new_files, working_dir)

        if new_files:
            logger.info("Artifacts detected: %s", [Path(f).name for f in new_files])
        else:
            logger.warning("No artifacts detected in %s (%d files scanned)", working_dir, len(existing_mtimes))
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
        container_name = f"agentcore-pip-{uuid.uuid4().hex[:8]}"
        cmd = [
            "docker", "run",
            "--name", container_name,
            "--rm",
            "-v", f"{config.DOCKER_PIP_CACHE}:/pip-cache",
            "-e", "PIP_TARGET=/pip-cache",
            "--network", config.DOCKER_NETWORK,
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
    # Lightweight code content scan (defense-in-depth, not a security boundary)
    if language == "python":
        safety_msg = _check_code_safety(code)
        if safety_msg:
            logger.warning("Code content blocked: %s", safety_msg)
            return ExecutionResult(success=False, stderr=safety_msg)

    safety_msg = _validate_working_dir(working_dir)
    if safety_msg:
        return ExecutionResult(success=False, stderr=safety_msg)

    working_dir.mkdir(parents=True, exist_ok=True)

    # Track path→mtime to detect both new AND modified files (e.g. overwritten on retry)
    existing_mtimes = {}
    if working_dir.exists():
        for f in _walk_artifacts(working_dir):
            try:
                existing_mtimes[f] = f.stat().st_mtime
            except OSError:
                pass

    suffix = {"python": ".py", "javascript": ".js", "bash": ".sh"}.get(language, ".py")

    script_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=suffix, dir=working_dir, delete=False,
        ) as f:
            f.write(code)
            script_path = Path(f.name)

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

        # Use start_new_session so we can kill the entire process group on timeout
        proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=working_dir, env=env, start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Kill the entire process group to prevent orphaned children
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            logger.warning("Execution timed out after %ds, killed process group %d", timeout, proc.pid)
            return ExecutionResult(success=False, stderr=f"Execution timed out after {timeout}s", timed_out=True)

        new_files = []
        for f in _walk_artifacts(working_dir):
            if f != script_path:
                prev_mtime = existing_mtimes.get(f)
                if prev_mtime is None or f.stat().st_mtime > prev_mtime:
                    new_files.append(str(f))

        # Fallback: if mtime found nothing but execution succeeded, parse stdout for file paths
        if not new_files and proc.returncode == 0 and stdout:
            new_files = _extract_paths_from_stdout(stdout, working_dir)
            if new_files:
                logger.info("Artifacts detected via stdout fallback: %s", [Path(f).name for f in new_files])

        # Sanity check: too many artifacts likely indicates venv/package leak
        new_files = _apply_artifact_sanity_check(new_files, working_dir)

        if new_files:
            logger.info("Artifacts detected: %s", [Path(f).name for f in new_files])
        else:
            logger.warning("No artifacts detected in %s (%d files scanned)", working_dir, len(existing_mtimes))
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
) -> ExecutionResult:
    """Execute code with automatic pip install on ImportError.

    If execution fails with ImportError/ModuleNotFoundError, parses the missing
    module name, runs pip install, and retries. Up to max_install_retries attempts.
    In Docker mode, pip install runs inside a container targeting the shared cache.
    """
    auto_installed = []
    use_docker = config.DOCKER_ENABLED and _docker_available()

    for attempt in range(max_install_retries + 1):
        result = run_code(code, language, timeout, working_dir, venv_path)

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
            install_result = run_shell(
                f"{pip_bin} install {missing}",
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

    # Protected env: strip only AgentCore's own credentials
    env = _filter_env()
    if env_vars:
        env.update(env_vars)

    # Track path→mtime to detect both new AND modified files (e.g. overwritten on retry)
    existing_mtimes = {}
    for f in _walk_artifacts(working_dir):
        try:
            existing_mtimes[f] = f.stat().st_mtime
        except OSError:
            pass

    logger.info("Shell exec: %s (cwd=%s, timeout=%ds)", command[:200], working_dir, timeout)

    try:
        # Use start_new_session so we can kill the entire process group on timeout
        proc = subprocess.Popen(
            full_command, shell=True, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=working_dir, env=env, start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            logger.warning("Shell command timed out after %ds, killed process group %d", timeout, proc.pid)
            return ExecutionResult(success=False, stderr=f"Timed out after {timeout}s", timed_out=True)

        new_files = []
        for f in _walk_artifacts(working_dir):
            prev_mtime = existing_mtimes.get(f)
            if prev_mtime is None or f.stat().st_mtime > prev_mtime:
                new_files.append(str(f))

        # Fallback: if mtime found nothing but execution succeeded, parse stdout for file paths
        if not new_files and proc.returncode == 0 and stdout:
            new_files = _extract_paths_from_stdout(stdout, working_dir)
            if new_files:
                logger.info("Artifacts detected via stdout fallback: %s", [Path(f).name for f in new_files])

        # Sanity check: too many artifacts likely indicates venv/package leak
        new_files = _apply_artifact_sanity_check(new_files, working_dir)

        if new_files:
            logger.info("Artifacts detected: %s", [Path(f).name for f in new_files])
        else:
            logger.warning("No artifacts detected in %s (%d files scanned)", working_dir, len(existing_mtimes))
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
