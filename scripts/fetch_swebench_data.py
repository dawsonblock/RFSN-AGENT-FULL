#!/usr/bin/env python3
"""Fetch official SWE-bench test patches and rebuild task files."""
import json
import urllib.request
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TASKS_DIR = os.path.join(ROOT, "data", "tasks")

# Map our task_id to search query and expected instance_id
TASK_MAP = {
    "flask__flask-4045": {
        "query": "flask-4045",
        "instance_id": "pallets__flask-4045",
        "repo_url": "https://github.com/pallets/flask.git",
    },
    "requests__requests-3362": {
        "query": "requests-3362",
        "instance_id": "psf__requests-3362",
        "repo_url": "https://github.com/psf/requests.git",
    },
    "scikit-learn__scikit-learn-10297": {
        "query": "scikit-learn-10297",
        "instance_id": "scikit-learn__scikit-learn-10297",
        "repo_url": "https://github.com/scikit-learn/scikit-learn.git",
    },
    "matplotlib__matplotlib-23476": {
        "query": "matplotlib-23476",
        "instance_id": "matplotlib__matplotlib-23476",
        "repo_url": "https://github.com/matplotlib/matplotlib.git",
    },
}

BASE_URL = "https://datasets-server.huggingface.co/search?dataset=princeton-nlp%2FSWE-bench_Lite&config=default&split=test&query="


def fetch_instance(query, expected_iid):
    """Fetch an instance from HuggingFace SWE-bench_Lite dataset."""
    url = BASE_URL + query
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode())

    for row_data in data.get("rows", []):
        row = row_data.get("row", {})
        if row.get("instance_id") == expected_iid:
            return row
    return None


def build_task(task_id, info, swebench_row):
    """Build a task JSON from SWE-bench data."""
    # Parse FAIL_TO_PASS
    fail_to_pass = json.loads(swebench_row["FAIL_TO_PASS"])

    # Determine focus files from the gold patch
    patch = swebench_row.get("patch", "")
    focus_files = []
    for line in patch.split("\n"):
        if line.startswith("diff --git a/"):
            parts = line.split(" b/")
            if len(parts) >= 2:
                fpath = parts[-1].strip()
                if fpath not in focus_files:
                    focus_files.append(fpath)

    task = {
        "task_id": task_id,
        "repo_url": info["repo_url"],
        "repo_ref": swebench_row["base_commit"],
        "workdir": f"/tmp/swebench_work/{task_id}",
        "issue_text": swebench_row["problem_statement"],
        "hints": {
            "failing_tests": fail_to_pass,
            "focus_files": focus_files,
            "test_patch": swebench_row["test_patch"],
        },
        "commands": {
            "setup": ["pip install -e ."],
            "test_quick": f"python -m pytest -x -q {' '.join(fail_to_pass[:2])}",
            "test_full": f"python -m pytest -x -q {fail_to_pass[0].rsplit('::', 1)[0] if fail_to_pass else ''}",
        },
        "limits": {
            "max_iters": 5,
            "max_patch_bytes": 100000,
            "max_files_touched": 10,
            "max_new_files": 3,
            "max_runtime_sec": 1800,
        },
    }
    return task


def main():
    for task_id, info in TASK_MAP.items():
        print(f"Fetching {task_id} ({info['instance_id']})...")
        row = fetch_instance(info["query"], info["instance_id"])
        if not row:
            print(f"  ERROR: Could not find {info['instance_id']} in dataset!")
            continue

        print(f"  base_commit: {row['base_commit']}")
        print(f"  test_patch: {len(row.get('test_patch', ''))} chars")
        print(f"  FAIL_TO_PASS: {row['FAIL_TO_PASS'][:120]}")

        task = build_task(task_id, info, row)

        task_path = os.path.join(TASKS_DIR, f"task_{task_id}.json")
        with open(task_path, "w") as f:
            json.dump(task, f, indent=2)
        print(f"  Wrote {task_path}")
        print()


if __name__ == "__main__":
    main()
