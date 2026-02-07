#!/bin/bash
set -euo pipefail

export DEEPSEEK_API_KEY="${DEEPSEEK_API_KEY:?Set DEEPSEEK_API_KEY}"

cd "$(dirname "$0")/.."

rm -rf /tmp/swebench_work/sympy__sympy-11400

echo "=== Starting bench run at $(date) ==="

python3 -m rfsn_swebench.cli \
    --task data/tasks/task_sympy__sympy-11400.json \
    --out data/results/result_sympy__sympy-11400.json \
    --proposer direct \
    --model deepseek-reasoner

echo "=== Bench run finished at $(date) ==="
