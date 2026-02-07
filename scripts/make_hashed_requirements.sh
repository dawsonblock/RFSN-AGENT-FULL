#!/usr/bin/env bash
set -euo pipefail

ROOT="$(pwd)"
TOOLS_VENV="${ROOT}/.venv-tools"
OUT_REQ="${ROOT}/requirements.txt"
IN_REQ="${ROOT}/requirements.in"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if [[ ! -f "$IN_REQ" ]]; then
  echo "[!] Missing requirements.in. Run scripts/bootstrap_requirements_in.sh or create it."
  exit 2
fi

"$PYTHON_BIN" -m venv "$TOOLS_VENV"
# shellcheck disable=SC1091
source "$TOOLS_VENV/bin/activate"

python -m pip install --upgrade pip
python -m pip install "pip-tools>=7.4.1"

pip-compile --generate-hashes --resolver=backtracking --strip-extras --output-file "$OUT_REQ" "$IN_REQ"

echo "[*] Verifying pip --require-hashes works"
TMP="$(mktemp -d)"
python -m venv "$TMP/venv"
# shellcheck disable=SC1091
source "$TMP/venv/bin/activate"
python -m pip install --upgrade pip
python -m pip install --require-hashes --only-binary=:all: -r "$OUT_REQ"
rm -rf "$TMP"

echo "[*] OK: requirements.txt pinned + hashed"
