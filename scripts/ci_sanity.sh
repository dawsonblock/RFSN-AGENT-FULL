#!/usr/bin/env bash
set -euo pipefail

echo "[sanity] checking docker.sock mount..."
if grep -R "/var/run/docker.sock" -n docker-compose.yml >/dev/null 2>&1; then
  echo "ERROR: docker.sock mount detected in docker-compose.yml"
  exit 1
fi

echo "[sanity] checking compose auth defaults are fail-closed..."
if grep -R "RFSN_DEV_MODE: \${RFSN_DEV_MODE:-1}" -n docker-compose.yml >/dev/null 2>&1; then
  echo "ERROR: compose defaults to dev-open auth mode"
  exit 1
fi

echo "[sanity] checking legacy kernel gate not used..."
if grep -R "validate_and_plan" -n services/orchestrator/app.py >/dev/null 2>&1; then
  echo "ERROR: legacy kernel validate_and_plan still referenced in orchestrator"
  exit 1
fi
if grep -R "from kernel import Kernel" -n services/orchestrator/app.py >/dev/null 2>&1; then
  echo "ERROR: legacy kernel import still referenced in orchestrator"
  exit 1
fi
if grep -R "kernel = Kernel(" -n services/orchestrator/app.py >/dev/null 2>&1; then
  echo "ERROR: legacy Kernel() construction still referenced in orchestrator"
  exit 1
fi

echo "[sanity] checking legacy kernel/ledger files removed..."
if [ -e services/orchestrator/kernel.py ]; then
  echo "ERROR: legacy services/orchestrator/kernel.py still exists"
  exit 1
fi
if [ -e services/orchestrator/ledger.py ]; then
  echo "ERROR: legacy services/orchestrator/ledger.py still exists"
  exit 1
fi

echo "[sanity] checking single-ledger path..."
if grep -R "from ledger import Ledger" -n services/orchestrator/app.py >/dev/null 2>&1; then
  echo "ERROR: legacy ledger import still referenced in orchestrator"
  exit 1
fi
if grep -R "/data/ledger.jsonl" -n services/orchestrator/app.py >/dev/null 2>&1; then
  echo "ERROR: legacy /data/ledger.jsonl path still referenced in orchestrator"
  exit 1
fi

echo "[sanity] ok"
