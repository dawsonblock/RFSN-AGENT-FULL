#!/usr/bin/env bash
# Run SWE-bench Lite against both Gemini 3 Flash and Gemini 3 Pro.
# Usage: bash scripts/run_gemini_swebench.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
TASKS_DIR="$PROJECT_DIR/data/swebench_lite_tasks"

# Gemini API config
export LLM_API_KEY="AIzaSyCpHOg1UiCoKWRBWwanUumAsGPa8zeHq8k"
GEMINI_BASE="https://generativelanguage.googleapis.com/v1beta/openai/"

echo "============================================================"
echo "  RFSN SWE-bench Lite — Gemini 3 Flash"
echo "============================================================"
python "$SCRIPT_DIR/run_swebench_batch.py" \
    --tasks "$TASKS_DIR" \
    --results "$PROJECT_DIR/results/gemini3_flash" \
    --base-url "$GEMINI_BASE" \
    --model "gemini-3-flash-preview" \
    --proposer direct \
    --outcome-memory "$PROJECT_DIR/results/gemini3_flash/outcome_memory.jsonl"

echo ""
echo "============================================================"
echo "  RFSN SWE-bench Lite — Gemini 3 Pro"
echo "============================================================"
python "$SCRIPT_DIR/run_swebench_batch.py" \
    --tasks "$TASKS_DIR" \
    --results "$PROJECT_DIR/results/gemini3_pro" \
    --base-url "$GEMINI_BASE" \
    --model "gemini-3-pro-preview" \
    --proposer direct \
    --outcome-memory "$PROJECT_DIR/results/gemini3_pro/outcome_memory.jsonl"

echo ""
echo "============================================================"
echo "  ALL RUNS COMPLETE"
echo "============================================================"
