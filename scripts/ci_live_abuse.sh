#!/usr/bin/env bash
set -euo pipefail
docker compose up --build -d
curl -sf http://localhost:8002/health >/dev/null

RESP=$(curl -s -o /tmp/abuse_body.txt -w "%{http_code}" \
  http://localhost:8002/run_step \
  -H 'Content-Type: application/json' \
  -d '{
    "repo_id": "demo1",
    "iter": 1,
    "step": {
      "id": "x",
      "type": "repo_read_range",
      "path": "../etc/passwd",
      "line_start": 1,
      "line_end": 5
    }
  }')

if [[ "$RESP" != "403" ]]; then
  echo "Expected 403, got $RESP"
  cat /tmp/abuse_body.txt
  docker compose down
  exit 1
fi

echo "[*] OK: traversal blocked"
docker compose down
