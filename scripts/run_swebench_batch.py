#!/usr/bin/env python3
"""Batch runner for SWE-bench tasks.

Iterates over task JSON files produced by ``convert_swebench_tasks.py``,
runs the RFSN bench CLI on each, and collects results into a summary.

Usage
-----
    # Run all tasks in data/tasks/ using the direct proposer
    python scripts/run_swebench_batch.py \\
        --tasks data/tasks \\
        --results data/results \\
        --proposer direct --model deepseek-reasoner

    # Run only tasks listed in the manifest
    python scripts/run_swebench_batch.py \\
        --tasks data/tasks \\
        --results data/results \\
        --manifest data/tasks/_manifest.json \\
        --proposer direct --model deepseek-reasoner

    # Run via the full RFSN stack (orchestrator proposer)
    python scripts/run_swebench_batch.py \\
        --tasks data/tasks \\
        --results data/results \\
        --proposer orchestrator

    # Dry run — show what would be run without executing
    python scripts/run_swebench_batch.py \\
        --tasks data/tasks --results data/results --dry-run

    # Resume an interrupted batch (skips tasks with existing results)
    python scripts/run_swebench_batch.py \\
        --tasks data/tasks --results data/results --resume \\
        --proposer direct --model deepseek-reasoner
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Any, Optional


def _find_task_files(
    tasks_dir: str, manifest_path: Optional[str] = None
) -> list[str]:
    """Discover task JSON files, optionally filtered by manifest."""
    if manifest_path:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        task_ids = manifest.get("task_ids", [])
        paths = []
        for tid in task_ids:
            p = os.path.join(tasks_dir, f"task_{tid}.json")
            if os.path.isfile(p):
                paths.append(p)
            else:
                print(
                    f"  WARN: manifest lists {tid}"
                    f" but {p} not found",
                    file=sys.stderr,
                )
        return sorted(paths)

    # No manifest — glob all task_*.json files
    paths = []
    for fname in sorted(os.listdir(tasks_dir)):
        if fname.startswith("task_") and fname.endswith(".json"):
            paths.append(os.path.join(tasks_dir, fname))
    return paths


def _result_path(results_dir: str, task_path: str) -> str:
    """Derive the result file path from a task file path."""
    fname = os.path.basename(task_path).replace("task_", "result_", 1)
    return os.path.join(results_dir, fname)


def _build_cli_args(
    task_path: str,
    result_path: str,
    args: argparse.Namespace,
) -> list[str]:
    """Build the CLI command to run a single task."""
    cmd = [
        sys.executable, "-m", "rfsn_swebench.cli",
        "--task", task_path,
        "--out", result_path,
        "--proposer", args.proposer,
    ]

    if args.replay_base:
        cmd.extend(["--replay-base", args.replay_base])

    if args.proposer == "direct":
        if args.model:
            cmd.extend(["--model", args.model])
        if args.base_url:
            cmd.extend(["--base-url", args.base_url])
        if args.api_key:
            cmd.extend(["--api-key", args.api_key])

    if args.proposer == "orchestrator":
        if args.orchestrator_url:
            cmd.extend(["--orchestrator-url", args.orchestrator_url])

    if args.executor_url:
        cmd.extend(["--executor-url", args.executor_url])
    if args.gateway_url:
        cmd.extend(["--gateway-url", args.gateway_url])
    if args.data_dir:
        cmd.extend(["--data-dir", args.data_dir])

    return cmd


def _load_result(path: str) -> dict:
    """Load a result JSON file."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def main(argv: Optional[list[str]] = None) -> None:
    ap = argparse.ArgumentParser(
        description="Batch runner for RFSN SWE-bench tasks",
    )
    ap.add_argument(
        "--tasks", required=True,
        help="Directory containing task_*.json files",
    )
    ap.add_argument(
        "--results", required=True,
        help="Output directory for result JSON files",
    )
    ap.add_argument(
        "--manifest", default=None,
        help="Path to _manifest.json (optional)",
    )
    ap.add_argument(
        "--replay-base", default=None,
        help="Replay artifacts directory",
    )
    ap.add_argument(
        "--proposer",
        choices=["direct", "orchestrator", "placeholder"],
        default="direct",
    )
    ap.add_argument(
        "--model", default="deepseek-reasoner",
        help="Model for direct proposer",
    )
    ap.add_argument("--base-url", default=None, help="LLM API base URL")
    ap.add_argument("--api-key", default=None, help="LLM API key")
    ap.add_argument(
        "--orchestrator-url", default=None,
        help="Orchestrator URL",
    )
    ap.add_argument("--executor-url", default=None, help="Executor URL")
    ap.add_argument("--gateway-url", default=None, help="Tool Gateway URL")
    ap.add_argument("--data-dir", default=None, help="Shared data dir")
    ap.add_argument(
        "--resume", action="store_true",
        help="Skip tasks with existing results",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Print commands without executing",
    )
    ap.add_argument(
        "--timeout", type=int, default=900,
        help="Per-task timeout in seconds (default: 900)",
    )
    ap.add_argument(
        "--parallel", type=int, default=1,
        help="Number of parallel workers (default: 1)",
    )

    args = ap.parse_args(argv)

    # Discover tasks
    task_files = _find_task_files(args.tasks, args.manifest)
    if not task_files:
        print(
            "No task files found. "
            "Run convert_swebench_tasks.py first.",
            file=sys.stderr,
        )
        sys.exit(1)

    os.makedirs(args.results, exist_ok=True)
    if args.replay_base:
        os.makedirs(args.replay_base, exist_ok=True)

    # Filter for resume
    if args.resume:
        original = len(task_files)
        task_files = [
            tf for tf in task_files
            if not os.path.isfile(_result_path(args.results, tf))
        ]
        done = original - len(task_files)
        print(
            f"Resume: {done} already done, "
            f"{len(task_files)} remaining"
        )

    total = len(task_files)
    print(f"\n{'='*60}")
    print("RFSN SWE-bench Batch Runner")
    print(f"  Tasks:    {total}")
    print(f"  Proposer: {args.proposer} ({args.model})")
    print(f"  Results:  {args.results}")
    print(f"  Timeout:  {args.timeout}s per task")
    print(f"{'='*60}\n")

    if args.dry_run:
        for tf in task_files:
            rp = _result_path(args.results, tf)
            cmd = _build_cli_args(tf, rp, args)
            print(" ".join(cmd))
        print(f"\n[DRY RUN] Would run {total} tasks")
        return

    # Run tasks sequentially (parallel support can be added later)
    summary: dict[str, Any] = {
        "total": total,
        "pass": 0,
        "fail": 0,
        "abort": 0,
        "error": 0,
        "results": [],
    }
    batch_start = time.time()

    for i, task_file in enumerate(task_files, 1):
        result_file = _result_path(args.results, task_file)
        task_id = (
            os.path.basename(task_file)
            .replace("task_", "")
            .replace(".json", "")
        )

        print(f"[{i}/{total}] {task_id}", end=" ", flush=True)
        cmd = _build_cli_args(task_file, result_file, args)

        t0 = time.time()
        try:
            proc = subprocess.run(
                cmd,
                cwd=os.path.dirname(
                    os.path.dirname(
                        os.path.abspath(__file__)
                    )
                ),
                capture_output=True,
                text=True,
                timeout=args.timeout,
            )
            dt = time.time() - t0

            if proc.returncode == 0:
                result = _load_result(result_file)
                status = result.get("status", "UNKNOWN")
                iters = result.get("iters", "?")
                risk = result.get("risk", {}).get("decision", "?")
                print(f"→ {status} (iters={iters}, risk={risk}, {dt:.0f}s)")

                if status == "PASS":
                    summary["pass"] += 1
                elif status == "FAIL":
                    summary["fail"] += 1
                elif status == "ABORT":
                    summary["abort"] += 1
                else:
                    summary["error"] += 1

                summary["results"].append({
                    "task_id": task_id,
                    "status": status,
                    "iters": iters,
                    "risk": risk,
                    "duration_sec": round(dt, 1),
                })
            else:
                dt = time.time() - t0
                print(f"→ ERROR (exit={proc.returncode}, {dt:.0f}s)")
                stderr_tail = (proc.stderr or "")[-500:]
                summary["error"] += 1
                summary["results"].append({
                    "task_id": task_id,
                    "status": "ERROR",
                    "exit_code": proc.returncode,
                    "stderr_tail": stderr_tail,
                    "duration_sec": round(dt, 1),
                })

        except subprocess.TimeoutExpired:
            dt = time.time() - t0
            print(f"→ TIMEOUT ({dt:.0f}s)")
            summary["error"] += 1
            summary["results"].append({
                "task_id": task_id,
                "status": "TIMEOUT",
                "duration_sec": round(dt, 1),
            })

        except Exception as exc:
            dt = time.time() - t0
            print(f"→ EXCEPTION: {exc}")
            summary["error"] += 1
            summary["results"].append({
                "task_id": task_id,
                "status": "EXCEPTION",
                "error": str(exc),
                "duration_sec": round(dt, 1),
            })

    # Summary
    batch_dt = time.time() - batch_start
    summary["total_duration_sec"] = round(batch_dt, 1)

    print(f"\n{'='*60}")
    print(f"BATCH COMPLETE — {batch_dt:.0f}s total")
    print(f"  PASS:  {summary['pass']}/{total}")
    print(f"  FAIL:  {summary['fail']}/{total}")
    print(f"  ABORT: {summary['abort']}/{total}")
    print(f"  ERROR: {summary['error']}/{total}")
    if total > 0:
        print(f"  Resolve rate: {summary['pass']/total*100:.1f}%")
    print(f"{'='*60}")

    # Write summary
    summary_path = os.path.join(args.results, "_summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"\nSummary written to {summary_path}")


if __name__ == "__main__":
    main()
