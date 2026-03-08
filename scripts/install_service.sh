#!/bin/bash
# Install AgentSutra as a launchd service on Mac Mini (agentruntime1)
# Usage: bash scripts/install_service.sh

set -e

PLIST_SRC="$(dirname "$0")/com.agentsutra.bot.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.agentsutra.bot.plist"
LOG_DIR="$HOME/Library/Logs/agentsutra"

mkdir -p "$LOG_DIR"
cp "$PLIST_SRC" "$PLIST_DST"
launchctl load "$PLIST_DST"

echo "AgentSutra service installed."
echo "  Logs: $LOG_DIR/"
echo "  Check: launchctl list | grep agentsutra"
echo "  Stop:  launchctl unload $PLIST_DST"
