#!/usr/bin/env bash
set -euo pipefail
if [[ ! -f "requirements.in" ]]; then
  ./scripts/bootstrap_requirements_in.sh
fi
./scripts/make_hashed_requirements.sh
echo "[*] Repo ready for agent deps policy"
