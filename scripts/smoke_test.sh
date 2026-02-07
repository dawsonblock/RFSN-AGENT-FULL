#!/usr/bin/env bash
# smoke_test.sh — Verify the full RFSN stack is healthy and can run a fixture.
#
# After security hardening, only the orchestrator (port 8000) is exposed
# to the host.  Executor, tool_gateway, and llm_service are internal-only.
#
# Prerequisites:
#   1. docker compose up --build -d
#   2. blessed image built (docker build -t rfsn-blessed:0.2 -f blessed.Dockerfile .)
#   3. Fixture repo prepared in data/repos/<repo_id>
#
# Usage:
#   ./scripts/smoke_test.sh [repo_id]
set -euo pipefail

REPO_ID="${1:-demo_failrepo}"
BASE="${ORCHESTRATOR_URL:-http://localhost:8000}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

pass() { echo -e "${GREEN}[✓]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
fail() { echo -e "${RED}[✗]${NC} $1"; exit 1; }

echo "=== RFSN Stack Smoke Test ==="
echo "    Repo ID: $REPO_ID"
echo "    Orchestrator: $BASE"
echo ""

# --- Health check: orchestrator (only exposed port) ---
echo "--- Health Check ---"
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/health" 2>/dev/null || echo "000")
if [[ "$code" == "200" ]]; then
    pass "orchestrator $BASE/health"
else
    fail "orchestrator $BASE/health returned $code — is the stack up?"
fi

# Internal services health via docker exec (if containers are running)
for svc in llm_service tool_gateway executor; do
    container="rfsn-agent-${svc}-1"
    if docker ps --format '{{.Names}}' | grep -q "$container"; then
        port=8001
        [[ "$svc" == "tool_gateway" ]] && port=8002
        [[ "$svc" == "executor" ]] && port=8003
        h=$(docker exec "$container" curl -sf "http://localhost:${port}/health" 2>/dev/null || echo "FAIL")
        if echo "$h" | grep -q '"ok"'; then
            pass "$svc internal health"
        else
            warn "$svc internal health check: $h"
        fi
    else
        warn "$svc container not found (name=$container)"
    fi
done
echo ""

# --- Orchestrator: /run (quick, max_iters=1) ---
echo "--- Orchestrator: /run (max_iters=1) ---"
ORCH_RESP=$(curl -s -X POST "$BASE/run" \
    -H 'Content-Type: application/json' \
    -d "{
        \"repo_id\": \"$REPO_ID\",
        \"task\": \"Fix the failing test. Minimal diff only.\",
        \"max_iters\": 1,
        \"scenario\": \"smoke_test\"
    }" --max-time 180 2>/dev/null)

ORCH_STATUS=$(echo "$ORCH_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','?'))" 2>/dev/null || echo "?")
if [[ "$ORCH_STATUS" != "?" ]]; then
    pass "orchestrator /run returned status=$ORCH_STATUS"
else
    warn "orchestrator /run response: $ORCH_RESP"
fi
echo ""

echo "=== Smoke test complete ==="
