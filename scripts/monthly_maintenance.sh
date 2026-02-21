#!/bin/bash
# AgentCore monthly maintenance — reclaim disk space
# Scheduled via launchd: 1st of every month at 4:00 AM
set -e

AGENTCORE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
echo "[$(date)] AgentCore monthly maintenance starting"

# 1. VACUUM SQLite databases (reclaim free pages from deleted rows)
for db in "$AGENTCORE_DIR/storage/agentcore.db" "$AGENTCORE_DIR/storage/scheduler.db"; do
    if [ -f "$db" ]; then
        echo "  VACUUM $db (before: $(du -h "$db" | cut -f1))"
        sqlite3 "$db" "VACUUM;"
        echo "  VACUUM $db (after:  $(du -h "$db" | cut -f1))"
    fi
done

# 2. Clean Docker pip cache (remove packages not accessed in 30+ days)
PIP_CACHE="$AGENTCORE_DIR/workspace/.pip-cache"
if [ -d "$PIP_CACHE" ]; then
    SIZE_BEFORE=$(du -sh "$PIP_CACHE" | cut -f1)
    find "$PIP_CACHE" -type f -atime +30 -delete 2>/dev/null || true
    find "$PIP_CACHE" -type d -empty -delete 2>/dev/null || true
    SIZE_AFTER=$(du -sh "$PIP_CACHE" | cut -f1)
    echo "  pip-cache cleaned ($SIZE_BEFORE -> $SIZE_AFTER)"
fi

# 3. Docker system prune (dangling images, stopped containers, build cache older than 30 days)
if command -v docker &>/dev/null && docker info &>/dev/null 2>&1; then
    echo "  Docker prune:"
    docker system prune -f --filter "until=720h"
fi

echo "[$(date)] Monthly maintenance complete"
