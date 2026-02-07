#!/usr/bin/env bash
set -euo pipefail

# Run a single SWE-bench task with the RFSN agent.
# Usage: ./scripts/run_one_task.sh
# Requires: DEEPSEEK_API_KEY environment variable

cd "$(dirname "$0")/.."

TASK="data/tasks/task_sympy__sympy-11400.json"
OUT="data/results/result_sympy__sympy-11400.json"
WORKDIR="/tmp/swebench_work/sympy__sympy-11400"
LOG="/tmp/swebench_run.log"

echo "=== RFSN SWE-bench single-task runner ==="
echo "Task:    $TASK"
echo "Output:  $OUT"
echo "Workdir: $WORKDIR"
echo "Log:     $LOG"
echo ""

# Clean previous run
rm -rf "$WORKDIR"

# Verify API key
if [ -z "${DEEPSEEK_API_KEY:-}" ]; then
    echo "ERROR: DEEPSEEK_API_KEY not set"
    exit 1
fi
echo "API key: set (length ${#DEEPSEEK_API_KEY})"

# Run
echo ""
echo "Starting run at $(date)..."
python3 -m rfsn_swebench.cli \
    --task "$TASK" \
    --out "$OUT" \
    --proposer direct \
    --model deepseek-reasoner \
    2>&1 | tee "$LOG"

echo ""
echo "Finished at $(date)"
echo "Result:"
cat "$OUT" 2>/dev/null | python3 -m json.tool || echo "(no result file)"
