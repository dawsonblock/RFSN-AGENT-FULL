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
BLESSED_BUILD_TAG="${BLESSED_BUILD_TAG:-rfsn-blessed:0.2}"

upsert_env_kv() {
    local key="$1"
    local value="$2"
    local file=".env"
    local tmp
    tmp="$(mktemp)"
    awk -v k="$key" -v v="$value" -F= '
        BEGIN { updated = 0 }
        $1 == k && updated == 0 {
            print k "=" v
            updated = 1
            next
        }
        $1 == k { next }
        { print }
        END {
            if (updated == 0) {
                print k "=" v
            }
        }
    ' "$file" > "$tmp"
    mv "$tmp" "$file"
}

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
docker build -t "$BLESSED_BUILD_TAG" -f blessed.Dockerfile . -q

# Resolve a digest-pinned ref for strict runtime mode.
BLESSED_IMAGE_REF="$(docker image inspect "$BLESSED_BUILD_TAG" --format '{{index .RepoDigests 0}}' 2>/dev/null || true)"
if [[ -z "${BLESSED_IMAGE_REF:-}" ]]; then
    IMAGE_ID="$(docker image inspect "$BLESSED_BUILD_TAG" --format '{{.Id}}' 2>/dev/null || true)"
    if [[ "$IMAGE_ID" =~ ^sha256:[a-f0-9]{64}$ ]]; then
        BLESSED_IMAGE_REF="rfsn-blessed@${IMAGE_ID}"
    fi
fi
if [[ -z "${BLESSED_IMAGE_REF:-}" ]]; then
    echo -e "${RED}ERROR: Could not resolve blessed image digest ref${NC}"
    exit 1
fi
echo -e "${GREEN}Using blessed image ref:${NC} ${BLESSED_IMAGE_REF}"
upsert_env_kv "BLESSED_IMAGE" "${BLESSED_IMAGE_REF}"
upsert_env_kv "RFSN_STRICT_IMAGE_DIGEST" "${RFSN_STRICT_IMAGE_DIGEST:-1}"
export BLESSED_IMAGE="${BLESSED_IMAGE_REF}"

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
