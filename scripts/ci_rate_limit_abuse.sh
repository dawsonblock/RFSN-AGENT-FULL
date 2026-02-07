#!/usr/bin/env bash
set -euo pipefail
docker compose up --build -d
curl -sf http://localhost:8002/health >/dev/null

REPO="abuse"
ITER=1

for i in $(seq 1 10); do
  code=$(curl -s -o /tmp/rl_body.txt -w "%{http_code}" \
    http://localhost:8002/run_step \
    -H 'Content-Type: application/json' \
    -d "{
      \"repo_id\": \"${REPO}\",
      \"iter\": ${ITER},
      \"step\": { \"id\": \"s${i}\", \"type\": \"repo_search\", \"pattern\": \"def \" }
    }")
  if [[ "$i" -le 4 ]]; then
    if [[ "$code" == "429" ]]; then
      echo "Unexpected 429 at search i=$i"
      cat /tmp/rl_body.txt
      docker compose down
      exit 1
    fi
  else
    if [[ "$code" == "429" ]]; then
      echo "[*] OK: got 429 after exceeding search budget"
      break
    fi
  fi
done

for i in $(seq 1 12); do
  code=$(curl -s -o /tmp/rl_body.txt -w "%{http_code}" \
    http://localhost:8002/run_step \
    -H 'Content-Type: application/json' \
    -d "{
      \"repo_id\": \"${REPO}\",
      \"iter\": ${ITER},
      \"step\": { \"id\": \"r${i}\", \"type\": \"repo_read_range\", \"path\": \"repo/README.md\", \"line_start\": 1, \"line_end\": 5 }
    }")
  if [[ "$i" -le 6 ]]; then
    if [[ "$code" == "429" ]]; then
      echo "Unexpected 429 at read i=$i"
      cat /tmp/rl_body.txt
      docker compose down
      exit 1
    fi
  else
    if [[ "$code" == "429" ]]; then
      echo "[*] OK: got 429 after exceeding read budget"
      break
    fi
  fi
done

docker compose down
