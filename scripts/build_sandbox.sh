#!/bin/bash
# build_sandbox.sh — Build the AgentSutra sandbox Docker image
# Usage: ./scripts/build_sandbox.sh
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AGENTSUTRA_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "Building AgentSutra sandbox Docker image..."
echo "Context: ${AGENTSUTRA_DIR}"
echo ""

# Check Docker is installed
if ! command -v docker &> /dev/null; then
    echo "ERROR: Docker is not installed."
    echo ""
    echo "Install Docker Desktop for Mac:"
    echo "  https://docs.docker.com/desktop/install/mac-install/"
    echo ""
    echo "After installing, launch Docker Desktop and wait for it to start."
    exit 1
fi

# Check Docker daemon is running
if ! docker info &> /dev/null 2>&1; then
    echo "ERROR: Docker daemon is not running."
    echo "Launch Docker Desktop and wait for the whale icon to appear in the menu bar."
    exit 1
fi

# Build the image
docker build \
    -t agentsutra-sandbox \
    -f "${AGENTSUTRA_DIR}/Dockerfile" \
    "${AGENTSUTRA_DIR}"

echo ""
echo "Done. Image 'agentsutra-sandbox' is ready."
echo ""
echo "Enable Docker sandbox in .env:"
echo "  DOCKER_ENABLED=true"

# Create pip cache directory on host
PIP_CACHE="${AGENTSUTRA_DIR}/workspace/.pip-cache"
mkdir -p "$PIP_CACHE"
echo ""
echo "Pip cache directory: ${PIP_CACHE}"
