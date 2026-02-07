#!/usr/bin/env bash
# run_swebench_batch.sh — Run all (or selected) SWE-bench tasks
#
# Usage:
#   ./scripts/run_swebench_batch.sh                  # all tasks
#   ./scripts/run_swebench_batch.sh sympy flask       # tasks matching patterns
#
# Requires DEEPSEEK_API_KEY set in environment.
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TASKS_DIR="$ROOT/data/tasks"
RESULTS_DIR="$ROOT/data/results"
LOG_DIR="$ROOT/data/logs"

mkdir -p "$RESULTS_DIR" "$LOG_DIR"

if [[ -z "${DEEPSEEK_API_KEY:-}" ]]; then
    echo -e "${RED}ERROR: DEEPSEEK_API_KEY not set${NC}"
    exit 1
fi

# Collect task files
PATTERNS=("$@")
TASK_FILES=()

for f in "$TASKS_DIR"/task_*.json; do
    [[ ! -f "$f" ]] && continue
    basename_f="$(basename "$f")"
    # Skip demo fixtures (not real SWE-bench tasks)
    [[ "$basename_f" == *"demo_"* ]] && continue

    if [[ ${#PATTERNS[@]} -eq 0 ]]; then
        TASK_FILES+=("$f")
    else
        for pat in "${PATTERNS[@]}"; do
            if [[ "$basename_f" == *"$pat"* ]]; then
                TASK_FILES+=("$f")
                break
            fi
        done
    fi
done

echo -e "${CYAN}=== RFSN SWE-bench Batch Runner ===${NC}"
echo "Tasks found: ${#TASK_FILES[@]}"
echo "Results dir: $RESULTS_DIR"
echo ""

PASS=0
FAIL=0
ABORT=0
TOTAL=${#TASK_FILES[@]}

for task_file in "${TASK_FILES[@]}"; do
    task_id=$(python3 -c "import json; print(json.load(open('$task_file'))['task_id'])")
    result_file="$RESULTS_DIR/result_${task_id}.json"
    log_file="$LOG_DIR/${task_id}.log"

    echo -e "${CYAN}[$(( PASS + FAIL + ABORT + 1 ))/$TOTAL]${NC} $task_id"

    # Clean workdir
    workdir=$(python3 -c "import json; print(json.load(open('$task_file'))['workdir'])")
    rm -rf "$workdir" 2>/dev/null || true

    # Run
    t0=$(date +%s)
    python3 -m rfsn_swebench.cli \
        --task "$task_file" \
        --out "$result_file" \
        --proposer direct \
        --model "${DEEPSEEK_MODEL:-deepseek-reasoner}" \
        > "$log_file" 2>&1 \
        && rc=0 || rc=$?
    t1=$(date +%s)
    elapsed=$(( t1 - t0 ))

    # Parse result
    if [[ -f "$result_file" ]]; then
        status=$(python3 -c "import json; print(json.load(open('$result_file')).get('status','?'))" 2>/dev/null || echo "?")
    else
        status="ABORT"
    fi

    case "$status" in
        PASS) echo -e "  ${GREEN}✓ PASS${NC} (${elapsed}s)"; PASS=$((PASS+1)) ;;
        FAIL) echo -e "  ${RED}✗ FAIL${NC} (${elapsed}s)"; FAIL=$((FAIL+1)) ;;
        *)    echo -e "  ${YELLOW}⚠ $status${NC} (${elapsed}s)"; ABORT=$((ABORT+1)) ;;
    esac
done

echo ""
echo -e "${CYAN}=== Results ===${NC}"
echo -e "  ${GREEN}PASS: $PASS${NC}"
echo -e "  ${RED}FAIL: $FAIL${NC}"
echo -e "  ${YELLOW}ABORT: $ABORT${NC}"
echo "  TOTAL: $TOTAL"

if [[ $FAIL -eq 0 && $ABORT -eq 0 ]]; then
    echo -e "\n${GREEN}All tasks passed!${NC}"
    exit 0
else
    exit 1
fi
