#!/bin/bash
set -euo pipefail
export DEEPSEEK_API_KEY="${DEEPSEEK_API_KEY:?Set DEEPSEEK_API_KEY}"
export PYTHONUNBUFFERED=1
cd "$(dirname "$0")/.."
rm -rf /tmp/swebench_work/django__django-11049
echo "=== Starting django bench run at $(date) ==="
python3 -m rfsn_swebench.cli \
    --task data/tasks/task_django__django-11049.json \
    --out data/results/result_django__django-11049.json \
    --proposer direct --model deepseek-reasoner
echo "=== Django bench run finished at $(date) ==="
