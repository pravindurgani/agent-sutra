#!/bin/bash
# Install AgentSutra as a launchd service on Mac Mini (agentruntime1)
# Usage: bash scripts/install_service.sh

set -e

PLIST_SRC="$(dirname "$0")/com.agentsutra.bot.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.agentsutra.bot.plist"
LOG_DIR="$HOME/Library/Logs/agentsutra"
DOMAIN_TARGET="gui/$(id -u)"
SERVICE_TARGET="$DOMAIN_TARGET/com.agentsutra.bot"

mkdir -p "$LOG_DIR"

# Unload existing service if loaded (ignore errors if not loaded)
launchctl bootout "$SERVICE_TARGET" 2>/dev/null || true

# Copy plist and load
cp "$PLIST_SRC" "$PLIST_DST"
launchctl bootstrap "$DOMAIN_TARGET" "$PLIST_DST"

echo "AgentSutra service installed."
echo "  Logs:    $LOG_DIR/"
echo "  Check:   launchctl print $SERVICE_TARGET"
echo "  Stop:    launchctl bootout $SERVICE_TARGET"
echo "  Restart: launchctl kickstart -k $SERVICE_TARGET"
