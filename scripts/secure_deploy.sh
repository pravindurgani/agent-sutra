#!/bin/bash
# secure_deploy.sh - Deployment hardening for AgentCore
# Run this after transferring to a dedicated runtime user or production environment.
set -e

AGENTCORE_DIR="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
BACKUP_DIR="${AGENTCORE_DIR}/backups"

echo "AgentCore Secure Deploy"
echo "======================"
echo "Target: ${AGENTCORE_DIR}"
echo ""

# --- 1. Protect configuration files (read-only) ---
echo "[1/3] Setting read-only permissions on config files..."

for f in "projects.yaml" "USECASES.md" ".env"; do
    target="${AGENTCORE_DIR}/${f}"
    if [ -f "$target" ]; then
        chmod 444 "$target"
        echo "  chmod 444 ${f}"
    else
        echo "  SKIP ${f} (not found)"
    fi
done

# --- 2. Ensure workspace directories exist with correct permissions ---
echo "[2/3] Setting up workspace directories..."

for d in "workspace/uploads" "workspace/outputs" "workspace/projects" "storage"; do
    target="${AGENTCORE_DIR}/${d}"
    mkdir -p "$target"
    chmod 755 "$target"
    echo "  mkdir + chmod 755 ${d}/"
done

# --- 3. Install daily backup cron job ---
echo "[3/3] Installing daily backup cron job..."

mkdir -p "$BACKUP_DIR"

CRON_CMD="0 3 * * * tar czf ${BACKUP_DIR}/workspace_\$(date +\\%Y\\%m\\%d).tar.gz -C ${AGENTCORE_DIR} workspace/ storage/ projects.yaml 2>/dev/null; find ${BACKUP_DIR} -name 'workspace_*.tar.gz' -mtime +7 -delete"

# Check if cron job already exists
if crontab -l 2>/dev/null | grep -q "AgentCore workspace backup"; then
    echo "  Cron job already installed, skipping."
else
    # Add new cron job preserving existing entries
    (crontab -l 2>/dev/null; echo "# AgentCore workspace backup (daily 3am, keep 7 days)"; echo "$CRON_CMD") | crontab -
    echo "  Installed: daily backup at 3am to ${BACKUP_DIR}/"
    echo "  Retention: 7 days"
fi

echo ""
echo "Done. To undo read-only permissions for editing:"
echo "  chmod 644 ${AGENTCORE_DIR}/projects.yaml"
echo "  chmod 644 ${AGENTCORE_DIR}/.env"
