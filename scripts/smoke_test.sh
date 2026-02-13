#!/usr/bin/env bash
# smoke_test.sh — End-to-end smoke for the hardened orchestrator API.
#
# Usage:
#   RFSN_SERVICE_TOKEN=... ./scripts/smoke_test.sh \
#     [repo_url] [repo_id]
#
# Defaults:
#   repo_url=https://github.com/pypa/sampleproject.git
#   repo_id=smoke-sampleproject
set -euo pipefail

BASE="${ORCHESTRATOR_URL:-http://localhost:8000}"
REPO_URL="${1:-https://github.com/pypa/sampleproject.git}"
REPO_ID="${2:-smoke-sampleproject}"
TOKEN="${RFSN_SERVICE_TOKEN:-}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

pass() { echo -e "${GREEN}[ok]${NC} $1"; }
warn() { echo -e "${YELLOW}[warn]${NC} $1"; }
fail() { echo -e "${RED}[fail]${NC} $1"; exit 1; }

AUTH_ARGS=()
if [[ -n "$TOKEN" ]]; then
  AUTH_ARGS=(-H "Authorization: Bearer $TOKEN")
else
  warn "RFSN_SERVICE_TOKEN not set; calls may fail if auth is enforced."
fi

echo "=== RFSN End-to-End Smoke ==="
echo "orchestrator=$BASE"
echo "repo_url=$REPO_URL"
echo "repo_id=$REPO_ID"
echo

echo "--- /health ---"
HEALTH="$(curl -sS "${AUTH_ARGS[@]}" "$BASE/health" || true)"
echo "$HEALTH"
python3 - <<'PY' "$HEALTH" || fail "health check failed"
import json,sys
obj=json.loads(sys.argv[1])
assert obj.get("ok") is True
PY
pass "health ok"

echo "--- /repos/import ---"
IMPORT="$(curl -sS -X POST "$BASE/repos/import" \
  "${AUTH_ARGS[@]}" \
  -H 'Content-Type: application/json' \
  -d "{\"repo_url\":\"$REPO_URL\",\"repo_id\":\"$REPO_ID\",\"depth\":1,\"force\":true}")"
echo "$IMPORT"
python3 - <<'PY' "$IMPORT" || fail "repo import failed"
import json,sys
obj=json.loads(sys.argv[1])
assert obj.get("ok") is True
assert obj.get("repo_id")
PY
pass "repo imported"

echo "--- /run (tests-only fast path) ---"
RUN="$(curl -sS -X POST "$BASE/run" \
  "${AUTH_ARGS[@]}" \
  -H 'Content-Type: application/json' \
  -d "{\"repo_id\":\"$REPO_ID\",\"task\":\"run tests only and make no changes\",\"max_iters\":2,\"scenario\":\"smoke_test\"}")"
echo "$RUN"
RUN_STATUS="$(python3 - <<'PY' "$RUN"
import json,sys
obj=json.loads(sys.argv[1])
print(obj.get("status",""))
PY
)"
RUN_ID="$(python3 - <<'PY' "$RUN"
import json,sys
obj=json.loads(sys.argv[1])
print(obj.get("run_id",""))
PY
)"
[[ "$RUN_STATUS" == "ok" ]] || fail "run failed with status=$RUN_STATUS"
[[ -n "$RUN_ID" ]] || fail "run_id missing"
pass "run ok ($RUN_ID)"

echo "--- /kernel/replay/manifest/check/$RUN_ID ---"
MANIFEST_CHECK="$(curl -sS "${AUTH_ARGS[@]}" "$BASE/kernel/replay/manifest/check/$RUN_ID")"
echo "$MANIFEST_CHECK"
python3 - <<'PY' "$MANIFEST_CHECK" || fail "manifest check failed"
import json,sys
obj=json.loads(sys.argv[1])
assert obj.get("ok") is True
PY
pass "replay manifest check ok"

echo "--- /chat (repo) ---"
CHAT_REPO="$(curl -sS -X POST "$BASE/chat" \
  "${AUTH_ARGS[@]}" \
  -H 'Content-Type: application/json' \
  -d "{\"repo_id\":\"$REPO_ID\",\"message\":\"Where are tests?\",\"max_files\":5}")"
echo "$CHAT_REPO"
python3 - <<'PY' "$CHAT_REPO" || fail "repo chat failed"
import json,sys
obj=json.loads(sys.argv[1])
assert obj.get("ok") is True
assert obj.get("thread_id")
assert obj.get("reply")
PY
pass "repo chat ok"

echo "--- /chat/text ---"
CHAT_TEXT="$(curl -sS -X POST "$BASE/chat/text" \
  "${AUTH_ARGS[@]}" \
  -H 'Content-Type: application/json' \
  -d '{"message":"hello from smoke test"}')"
echo "$CHAT_TEXT"
python3 - <<'PY' "$CHAT_TEXT" || fail "text chat failed"
import json,sys
obj=json.loads(sys.argv[1])
assert obj.get("ok") is True
assert obj.get("thread_id")
assert obj.get("reply")
PY
pass "text chat ok"

echo
echo "=== Smoke complete ==="
