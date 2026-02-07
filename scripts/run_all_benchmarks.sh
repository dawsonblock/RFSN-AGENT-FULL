#!/bin/bash
set -e
cd "/Users/dawsonblock/Downloads/new builds/rfsn-agent"
export DEEPSEEK_API_KEY="sk-70640705a68c4f879e15cadc77dad95c"

TASKS=(
  "flask__flask-4045"
  "requests__requests-3362"
  "scikit-learn__scikit-learn-10297"
  "matplotlib__matplotlib-23476"
)

for TASK in "${TASKS[@]}"; do
  TASK_FILE="data/tasks/task_${TASK}.json"
  OUT_FILE="data/results/result_${TASK}.json"
  WORKDIR="/tmp/swebench_work/${TASK}"

  echo ""
  echo "============================================"
  echo "=== Starting ${TASK} at $(date) ==="
  echo "============================================"

  # Clean workdir for fresh run
  rm -rf "${WORKDIR}"
  rm -f "${OUT_FILE}"

  python3 -m rfsn_swebench.cli \
    --task "${TASK_FILE}" \
    --out "${OUT_FILE}" \
    --proposer direct \
    --model deepseek-reasoner || true

  echo ""
  echo "=== Finished ${TASK} at $(date) ==="
  if [ -f "${OUT_FILE}" ]; then
    echo "=== Status: $(python3 -c "import json; print(json.load(open('${OUT_FILE}'))['status'])"  ) ==="
  else
    echo "=== No result file produced ==="
  fi
  echo ""
done

echo ""
echo "============================================"
echo "=== ALL BENCHMARKS COMPLETE at $(date) ==="
echo "============================================"
echo ""
echo "Results:"
for TASK in "${TASKS[@]}"; do
  OUT_FILE="data/results/result_${TASK}.json"
  if [ -f "${OUT_FILE}" ]; then
    STATUS=$(python3 -c "import json; d=json.load(open('${OUT_FILE}')); print(f\"{d['status']} (iters={d['iters']})\")")
    echo "  ${TASK}: ${STATUS}"
  else
    echo "  ${TASK}: NO RESULT"
  fi
done
