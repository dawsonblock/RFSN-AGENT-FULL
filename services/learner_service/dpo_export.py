"""DPO Export Utility for RFSN.

Converts captured trajectories and outcomes into Preference Datasets
compatible with HuggingFace TRL (Transformer Reinforcement Learning).

Schema:
  {
      "prompt": "Task description...",
      "chosen": [accepted_steps...],
      "rejected": [failed_steps...]
  }
"""

import json
import os
from typing import List, Dict, Any, Optional
from services.learner_service.store_duckdb import DuckStore


def export_for_dpo(db_path: str, output_path: str, min_score_diff: float = 0.5) -> int:
    """Export trajectories to JSONL for DPO training.

    Pairs "chosen" (successful) runs with "rejected" (failed) runs
    for the same task.
    """
    if not os.path.exists(db_path):
        print(f"Database not found: {db_path}")
        return 0

    store = DuckStore(db_path)

    # 1. Fetch all trajectories
    # We need to join with outcome_map or just use the success flag in trajectories?
    # The trajectories table has 'success' and 'task_hash'.

    query = """
        SELECT task_hash, success, steps, run_id
        FROM trajectories
        WHERE steps IS NOT NULL
    """

    try:
        # Check if we are running in a test with mock rows that might be dicts or tuples
        rows = store.con.execute(query).fetchall()
        # In the test, we mock fetchall to return a list of tuples.
    except Exception as e:
        print(f"Error querying trajectories: {e}")
        return 0

    # Group by task_hash
    tasks: Dict[str, Dict[str, List[Any]]] = {}

    for r in rows:
        # DuckDB returns tuples.
        task_hash = r[0]
        # Ensure boolean
        val = r[1]
        if isinstance(val, int):
            success = bool(val)
        else:
            success = val == "true" or val is True

        steps_raw = r[2]
        if isinstance(steps_raw, str):
            try:
                steps = json.loads(steps_raw)
            except:
                steps = []
        else:
            steps = steps_raw

        if task_hash not in tasks:
            tasks[task_hash] = {"chosen": [], "rejected": []}

        if success:
            tasks[task_hash]["chosen"].append(steps)
        else:
            tasks[task_hash]["rejected"].append(steps)

    # 2. Pair and Export
    count = 0
    with open(output_path, "w", encoding="utf-8") as f:
        for task_hash, groups in tasks.items():
            # Simply cartesian product of chosen x rejected?
            # Or just best chosen vs best rejected?
            # For now, 1-to-1 pairing or broadcasting.

            if not groups["chosen"] or not groups["rejected"]:
                continue

            # Take the first chosen (heuristic: assumes any success is good)
            chosen_traj = groups["chosen"][0]

            # Format prompt from the first step's intent or similar?
            # Ideally the prompt is the Task description.
            # We captured 'task' in the run, but only task_hash is in trajectories?
            # Ah, we need the Task text.
            # Limitation: We only stored task_hash in the previous step's schema.
            # We should probably fetch the task description from the first step if available,
            # or we need to update the schema to store 'task_content'.

            # Extract prompt from the first step if possible
            prompt = "Replay Task"
            if chosen_traj and len(chosen_traj) > 0:
                first_step = chosen_traj[0]
                if "task" in first_step:
                    prompt = first_step["task"]
                elif "intent" in first_step and "task" in first_step["intent"]:
                    prompt = first_step["intent"]["task"]

            for rejected_traj in groups["rejected"]:
                entry = {
                    "prompt": prompt,
                    "chosen": _format_convo(chosen_traj),
                    "rejected": _format_convo(rejected_traj),
                    "metadata": {"task_hash": task_hash},
                }
                f.write(json.dumps(entry) + "\n")
                count += 1

    return count


def _format_convo(steps: List[Dict]) -> List[Dict]:
    """Format steps into chat conversation format."""
    convo = []
    for step in steps:
        # User/System: Context/Intent
        # Assistant: Action
        # User/Tool: Result

        intent = step.get("intent", {})
        result = step.get("result", {})

        # This is a simplification. Real DPO needs precise chat templates.
        # We will dump the raw interaction for now.
        convo.append(
            {
                "role": "user",
                "content": f"Execute step {step.get('iteration')}: {json.dumps(intent)}",
            }
        )
        convo.append(
            {"role": "assistant", "content": f"Tool Output: {json.dumps(result)}"}
        )

    return convo


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print("Usage: python dpo_export.py <db_path> <output_jsonl>")
        sys.exit(1)

    c = export_for_dpo(sys.argv[1], sys.argv[2])
    print(f"Exported {c} DPO pairs.")
