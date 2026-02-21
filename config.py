import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Paths
BASE_DIR = Path(__file__).parent
WORKSPACE_DIR = BASE_DIR / "workspace"
UPLOADS_DIR = WORKSPACE_DIR / "uploads"
OUTPUTS_DIR = WORKSPACE_DIR / "outputs"
PROJECTS_DIR = WORKSPACE_DIR / "projects"
DB_PATH = BASE_DIR / "storage" / "agentcore.db"

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

# Environment keys stripped from subprocess (AgentCore's own credentials only)
PROTECTED_ENV_KEYS = {"ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN"}

# Pattern-based env filtering: strip any var whose name contains these substrings
# Catches AWS_SECRET_ACCESS_KEY, GITHUB_TOKEN, DATABASE_PASSWORD, etc.
PROTECTED_ENV_SUBSTRINGS = {"KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL"}

# Model config
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "claude-sonnet-4-6")
COMPLEX_MODEL = os.getenv("COMPLEX_MODEL", "claude-opus-4-6")

# Execution limits
EXECUTION_TIMEOUT = int(os.getenv("EXECUTION_TIMEOUT", "120"))       # Single code execution
MAX_CODE_EXECUTION_TIMEOUT = int(os.getenv("MAX_CODE_EXECUTION_TIMEOUT", "600"))  # Hard cap
LONG_TIMEOUT = int(os.getenv("LONG_TIMEOUT", "900"))                 # Full pipeline timeout (interactive + scheduled)

# Retry limits
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))           # Pipeline audit-retry limit
API_MAX_RETRIES = int(os.getenv("API_MAX_RETRIES", "5"))   # Claude API call retries (rate limit, timeout)

# File limits
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "50"))
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

# Ollama configuration
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_DEFAULT_MODEL = os.getenv("OLLAMA_DEFAULT_MODEL", "llama3.1:8b")

# Data processing thresholds
BIG_DATA_ROW_THRESHOLD = int(os.getenv("BIG_DATA_ROW_THRESHOLD", "500"))

# Resource management
MAX_CONCURRENT_TASKS = int(os.getenv("MAX_CONCURRENT_TASKS", "3"))
RAM_THRESHOLD_PERCENT = int(os.getenv("RAM_THRESHOLD_PERCENT", "90"))

# Docker sandbox (for isolated code execution)
DOCKER_ENABLED = os.getenv("DOCKER_ENABLED", "false").lower() in ("true", "1", "yes")
DOCKER_IMAGE = os.getenv("DOCKER_IMAGE", "agentcore-sandbox")
DOCKER_MEMORY_LIMIT = os.getenv("DOCKER_MEMORY_LIMIT", "2g")
DOCKER_CPU_LIMIT = float(os.getenv("DOCKER_CPU_LIMIT", "2"))
DOCKER_NETWORK = os.getenv("DOCKER_NETWORK", "bridge")  # "bridge" or "none"
DOCKER_PIP_CACHE = WORKSPACE_DIR / ".pip-cache"

# Budget controls (0 = unlimited)
DAILY_BUDGET_USD = float(os.getenv("DAILY_BUDGET_USD", "0"))
MONTHLY_BUDGET_USD = float(os.getenv("MONTHLY_BUDGET_USD", "0"))

# Telegram limits
TELEGRAM_MAX_MESSAGE_LENGTH = 4096
