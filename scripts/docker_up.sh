#!/usr/bin/env bash
# docker_up.sh — Build and start the full RFSN stack.
#
# Usage:
#   ./scripts/docker_up.sh           # build + start
#   ./scripts/docker_up.sh --clean   # rebuild from scratch
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

RED='\033[0;31m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
NC='\033[0m'

# Check .env
if [[ ! -f .env ]]; then
    if [[ -f .env.example ]]; then
        echo -e "${CYAN}Creating .env from .env.example...${NC}"
        cp .env.example .env
        echo -e "${RED}Please edit .env and set DEEPSEEK_API_KEY${NC}"
        exit 1
    fi
fi

# Verify API key
source .env 2>/dev/null || true
if [[ -z "${DEEPSEEK_API_KEY:-}" ]]; then
    echo -e "${RED}ERROR: DEEPSEEK_API_KEY not set in .env${NC}"
    exit 1
fi

# Clean rebuild if requested
if [[ "${1:-}" == "--clean" ]]; then
    echo -e "${CYAN}Cleaning old containers...${NC}"
    docker compose down --remove-orphans 2>/dev/null || true
    docker compose build --no-cache
else
    docker compose build
fi

# Build blessed sandbox image
echo -e "${CYAN}Building blessed sandbox image...${NC}"
docker build -t rfsn-blessed:0.2 -f blessed.Dockerfile . -q

# Ensure data dirs exist
mkdir -p data/repos data/venv data/wheels data/artifacts data/cassettes data/results data/logs

# Start stack
echo -e "${CYAN}Starting RFSN stack...${NC}"
docker compose up -d

# Wait for health
echo -e "${CYAN}Waiting for services...${NC}"
for i in $(seq 1 30); do
    code=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8000/health 2>/dev/null || echo "000")
    if [[ "$code" == "200" ]]; then
        echo -e "${GREEN}✓ Stack is healthy${NC}"
        echo ""
        docker compose ps
        exit 0
    fi
    sleep 2
done

echo -e "${RED}Stack did not become healthy in 60s${NC}"
docker compose logs --tail=20
exit 1
