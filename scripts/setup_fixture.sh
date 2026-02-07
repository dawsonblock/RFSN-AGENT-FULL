#!/usr/bin/env bash
# setup_fixture.sh — Copy a fixture repo into data/repos/<repo_id>/
# and generate hashed requirements for the deps policy.
#
# Usage:
#   ./scripts/setup_fixture.sh <fixture_name> [repo_id]
#
# Examples:
#   ./scripts/setup_fixture.sh demo_failrepo
#   ./scripts/setup_fixture.sh demo1 my_custom_id
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"

FIXTURE="${1:?Usage: setup_fixture.sh <fixture_name> [repo_id]}"
REPO_ID="${2:-$FIXTURE}"
FIXTURE_DIR="$ROOT/fixtures/$FIXTURE"

if [[ ! -d "$FIXTURE_DIR" ]]; then
    echo "[!] Fixture not found: $FIXTURE_DIR"
    echo "    Available fixtures:"
    ls -1 "$ROOT/fixtures/" 2>/dev/null | sed 's/^/      /'
    exit 1
fi

DEST="$ROOT/data/repos/$REPO_ID"

echo "[*] Setting up fixture '$FIXTURE' as repo_id='$REPO_ID'"

# Copy fixture into data/repos/
rm -rf "$DEST"
mkdir -p "$DEST"
cp -a "$FIXTURE_DIR/." "$DEST/"

# Initialise as a git repo so git operations work
cd "$DEST"
if [[ ! -d .git ]]; then
    git init -q
    git add -A
    git commit -q -m "fixture: $FIXTURE" --allow-empty
fi

# Generate hashed requirements if requirements.in exists and
# requirements.txt is still a stub/TODO.
if [[ -f requirements.in ]]; then
    if grep -q "TODO" requirements.txt 2>/dev/null; then
        echo "[*] Generating hashed requirements.txt from requirements.in ..."
        PYTHON_BIN="${PYTHON_BIN:-python3}"
        TOOLS_VENV="$DEST/.venv-tools"
        "$PYTHON_BIN" -m venv "$TOOLS_VENV"
        # shellcheck disable=SC1091
        source "$TOOLS_VENV/bin/activate"
        python -m pip install --upgrade pip pip-tools >/dev/null 2>&1
        pip-compile \
            --generate-hashes \
            --resolver=backtracking \
            --strip-extras \
            --output-file requirements.txt \
            requirements.in 2>/dev/null
        deactivate
        rm -rf "$TOOLS_VENV"
        echo "[*] requirements.txt generated with hashes."
    else
        echo "[*] requirements.txt already populated — skipping pip-compile."
    fi
fi

echo "[✓] Fixture ready at $DEST"
echo "    Repo ID for API calls: $REPO_ID"
