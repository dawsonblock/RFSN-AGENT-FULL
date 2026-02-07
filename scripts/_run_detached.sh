#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

export DEEPSEEK_API_KEY="${DEEPSEEK_API_KEY:?Set DEEPSEEK_API_KEY}"

TASK="data/tasks/task_sympy__sympy-11400.json"
OUT="data/results/result_sympy__sympy-11400.json"
LOG="/tmp/swebench_run5.log"
DONE="/tmp/swebench_done5.flag"

rm -f "$DONE"
echo "=== Starting SWE-bench run at $(date) ===" > "$LOG"

python3 -m rfsn_swebench.cli \
    --task "$TASK" \
    --out "$OUT" \
    --proposer direct \
    --model deepseek-reasoner \
    >> "$LOG" 2>&1

echo "EXIT=$?" >> "$LOG"
echo "=== Finished at $(date) ===" >> "$LOG"
cat "$OUT" >> "$LOG" 2>/dev/null
touch "$DONE"
