import os
from pathlib import Path
from dotenv import load_dotenv

VERSION = "8.6.0"

load_dotenv()


def _safe_int(key: str, default: int) -> int:
    """A-26: Parse int env var with fallback on malformed values."""
    raw = os.getenv(key, str(default))
    try:
        return int(raw)
    except (ValueError, TypeError):
        return default


def _safe_float(key: str, default: float) -> float:
    """A-26: Parse float env var with fallback on malformed values."""
    raw = os.getenv(key, str(default))
    try:
        return float(raw)
    except (ValueError, TypeError):
        return default

# Paths
BASE_DIR = Path(__file__).parent
WORKSPACE_DIR = BASE_DIR / "workspace"
UPLOADS_DIR = WORKSPACE_DIR / "uploads"
OUTPUTS_DIR = WORKSPACE_DIR / "outputs"
PROJECTS_DIR = WORKSPACE_DIR / "projects"
PROJECTS_VENV_DIR = WORKSPACE_DIR / "project_venv"
DB_PATH = BASE_DIR / "storage" / "agentsutra.db"

# Filesystem boundary — agent can operate anywhere within user's home directory
HOST_HOME = Path.home()

# Extended thinking — enables adaptive thinking for claude-sonnet-4-6 / claude-opus-4-6
ENABLE_THINKING = os.getenv("ENABLE_THINKING", "true").lower() in ("true", "1", "yes")

# Ensure directories exist
for d in [UPLOADS_DIR, OUTPUTS_DIR, PROJECTS_DIR, DB_PATH.parent, WORKSPACE_DIR / ".pip-cache"]:
    d.mkdir(parents=True, exist_ok=True)

# API Keys
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
def _parse_user_ids(raw: str) -> list[int]:
    """Parse comma-separated user IDs, skipping malformed entries."""
    ids = []
    for uid in raw.split(","):
        uid = uid.strip()
        if uid:
            try:
                ids.append(int(uid))
            except ValueError:
                pass  # Skip non-numeric entries
    return ids


ALLOWED_USER_IDS = _parse_user_ids(os.getenv("ALLOWED_USER_IDS", ""))

# Environment keys stripped from subprocess (AgentSutra's own credentials only)
PROTECTED_ENV_KEYS = {"ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN"}

# Pattern-based env filtering: strip any var whose name contains these substrings
# Catches AWS_SECRET_ACCESS_KEY, GITHUB_TOKEN, DATABASE_PASSWORD, etc.
PROTECTED_ENV_SUBSTRINGS = {"KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "DATABASE", "AUTH"}

# Model config
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "claude-sonnet-4-6")
COMPLEX_MODEL = os.getenv("COMPLEX_MODEL", "claude-opus-4-6")

# Execution limits
EXECUTION_TIMEOUT = _safe_int("EXECUTION_TIMEOUT", 120)       # Single code execution
MAX_CODE_EXECUTION_TIMEOUT = _safe_int("MAX_CODE_EXECUTION_TIMEOUT", 600)  # Hard cap
LONG_TIMEOUT = _safe_int("LONG_TIMEOUT", 900)                 # Full pipeline timeout (interactive + scheduled)

# Retry limits
MAX_RETRIES = _safe_int("MAX_RETRIES", 3)           # Pipeline audit-retry limit
API_MAX_RETRIES = _safe_int("API_MAX_RETRIES", 5)   # Claude API call retries (rate limit, timeout)

# File limits
MAX_FILE_SIZE_MB = _safe_int("MAX_FILE_SIZE_MB", 50)
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

# Ollama configuration
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_DEFAULT_MODEL = os.getenv("OLLAMA_DEFAULT_MODEL", "deepseek-r1:14b")

# Data processing thresholds
BIG_DATA_ROW_THRESHOLD = _safe_int("BIG_DATA_ROW_THRESHOLD", 500)

# Resource management
MAX_CONCURRENT_TASKS = _safe_int("MAX_CONCURRENT_TASKS", 3)
RAM_THRESHOLD_PERCENT = _safe_int("RAM_THRESHOLD_PERCENT", 90)
MAX_FILE_INJECT_COUNT = _safe_int("MAX_FILE_INJECT_COUNT", 50)

# Docker sandbox (for isolated code execution)
DOCKER_ENABLED = os.getenv("DOCKER_ENABLED", "false").lower() in ("true", "1", "yes")
DOCKER_IMAGE = os.getenv("DOCKER_IMAGE", "agentsutra-sandbox")
DOCKER_MEMORY_LIMIT = os.getenv("DOCKER_MEMORY_LIMIT", "2g")
DOCKER_CPU_LIMIT = _safe_float("DOCKER_CPU_LIMIT", 2.0)
DOCKER_NETWORK = os.getenv("DOCKER_NETWORK", "bridge")  # "bridge" or "none"
DOCKER_PIP_CACHE = WORKSPACE_DIR / ".pip-cache"

# Budget controls (0 = unlimited)
DAILY_BUDGET_USD = _safe_float("DAILY_BUDGET_USD", 0.0)
MONTHLY_BUDGET_USD = _safe_float("MONTHLY_BUDGET_USD", 0.0)

# Deployment (optional — for publishing generated sites/apps)
DEPLOY_ENABLED = os.getenv("DEPLOY_ENABLED", "false").lower() == "true"
DEPLOY_PROVIDER = os.getenv("DEPLOY_PROVIDER", "github_pages")  # "github_pages", "vercel", or "firebase"
DEPLOY_REPO = os.getenv("DEPLOY_REPO", "")  # e.g., "agentsutra-bot/deployed-sites"
DEPLOY_GITHUB_TOKEN = os.getenv("DEPLOY_GITHUB_TOKEN", "")
DEPLOY_VERCEL_TOKEN = os.getenv("DEPLOY_VERCEL_TOKEN", "")
DEPLOY_BASE_URL = os.getenv("DEPLOY_BASE_URL", "")  # e.g., "https://agentsutra-bot.github.io"
DEPLOY_FIREBASE_PROJECT = os.getenv("DEPLOY_FIREBASE_PROJECT", "")
DEPLOY_FIREBASE_TOKEN = os.getenv("DEPLOY_FIREBASE_TOKEN", "")

# Server management (for local dev server preview)
SERVER_START_TIMEOUT = _safe_int("SERVER_START_TIMEOUT", 30)
SERVER_MAX_LIFETIME = _safe_int("SERVER_MAX_LIFETIME", 300)
SERVER_PORT_RANGE_START = _safe_int("SERVER_PORT_RANGE_START", 8100)
SERVER_PORT_RANGE_END = _safe_int("SERVER_PORT_RANGE_END", 8120)

# Visual verification (Playwright headless Chromium)
VISUAL_CHECK_ENABLED = os.getenv("VISUAL_CHECK_ENABLED", "false").lower() == "true"
VISUAL_CHECK_TIMEOUT = _safe_int("VISUAL_CHECK_TIMEOUT", 15)

# Telegram limits
TELEGRAM_MAX_MESSAGE_LENGTH = 4096
