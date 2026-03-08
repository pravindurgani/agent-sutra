#!/bin/bash
# Install AgentSutra as a launchd service.
# Auto-detects Python path and project directory.
# Usage: bash scripts/install_service.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PLIST_SRC="$SCRIPT_DIR/com.agentsutra.bot.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.agentsutra.bot.plist"
LOG_DIR="$HOME/Library/Logs/agentsutra"
DOMAIN_TARGET="gui/$(id -u)"
SERVICE_TARGET="$DOMAIN_TARGET/com.agentsutra.bot"

# Auto-detect Python: prefer venv, then pyenv, then system
if [ -x "$PROJECT_DIR/venv/bin/python3" ]; then
    PYTHON_BIN="$PROJECT_DIR/venv/bin/python3"
elif [ -x "$PROJECT_DIR/.venv/bin/python3" ]; then
    PYTHON_BIN="$PROJECT_DIR/.venv/bin/python3"
elif command -v pyenv > /dev/null 2>&1; then
    PYTHON_BIN="$(pyenv which python3 2>/dev/null || echo "")"
fi

if [ -z "$PYTHON_BIN" ] || [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN="$(which python3)"
fi

PYENV_BIN="$(dirname "$PYTHON_BIN")"

echo "Python:  $PYTHON_BIN"
echo "Project: $PROJECT_DIR"

# Verify python can import the project
if ! (cd "$PROJECT_DIR" && "$PYTHON_BIN" -c "import config") 2>/dev/null; then
    echo "ERROR: '$PYTHON_BIN' cannot import config.py from $PROJECT_DIR"
    echo "Make sure dependencies are installed in the correct venv."
    exit 1
fi

mkdir -p "$LOG_DIR"

# Generate plist from template with actual paths
sed \
    -e "s|__PYTHON__|$PYTHON_BIN|g" \
    -e "s|__PROJECT__|$PROJECT_DIR|g" \
    -e "s|__HOME__|$HOME|g" \
    -e "s|__PYENV_BIN__|$PYENV_BIN|g" \
    "$PLIST_SRC" > "$PLIST_DST"

# Unload existing service if loaded
launchctl bootout "$SERVICE_TARGET" 2>/dev/null || true

# Load the service
launchctl bootstrap "$DOMAIN_TARGET" "$PLIST_DST"

echo ""
echo "AgentSutra service installed."
echo "  Logs:    $LOG_DIR/"
echo "  Check:   launchctl print $SERVICE_TARGET"
echo "  Stop:    launchctl bootout $SERVICE_TARGET"
echo "  Restart: launchctl kickstart -k $SERVICE_TARGET"
