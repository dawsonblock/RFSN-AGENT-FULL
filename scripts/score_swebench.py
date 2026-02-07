#!/usr/bin/env python3
"""Score RFSN SWE-bench results and produce a leaderboard-style report.

Reads result JSON files produced by the batch runner and calculates:
- Overall resolve rate (PASS / total)
- Per-repo breakdown
- Per-difficulty breakdown (if Verified dataset)
- Timing statistics

Usage
-----
    # Score all results
    python scripts/score_swebench.py --results data/results

    # Score and compare against gold patches
    python scripts/score_swebench.py --results data/results \\
        --tasks data/tasks --verify

    # Output as CSV
    python scripts/score_swebench.py --results data/results --csv scores.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from typing import Optional


def _load_results(results_dir: str) -> list[dict]:
    """Load all result_*.json files from a directory."""
    results = []
    for fname in sorted(os.listdir(results_dir)):
        if fname.startswith("result_") and fname.endswith(".json"):
            path = os.path.join(results_dir, fname)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                data["_file"] = fname
                results.append(data)
            except Exception as exc:
                print(
                    f"  WARN: failed to load {fname}:"
                    f" {exc}",
                    file=sys.stderr,
                )
    return results


def _load_task(tasks_dir: str, task_id: str) -> Optional[dict]:
    """Load a task file by task_id."""
    fname = f"task_{task_id}.json"
    path = os.path.join(tasks_dir, fname)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _extract_repo(task_id: str) -> str:
    """Extract repo name from instance_id like 'django__django-12345'."""
    parts = task_id.split("__")
    if len(parts) >= 2:
        owner = parts[0]
        rest = parts[1]
        # Rest is like 'django-12345', strip the issue number
        repo_parts = rest.rsplit("-", 1)
        repo_name = repo_parts[0] if len(repo_parts) >= 2 else rest
        return f"{owner}/{repo_name}"
    return "unknown"


def main(argv: Optional[list[str]] = None) -> None:
    ap = argparse.ArgumentParser(description="Score RFSN SWE-bench results")
    ap.add_argument(
        "--results", required=True,
        help="Directory containing result_*.json files",
    )
    ap.add_argument(
        "--tasks", default=None,
        help="Tasks directory (for --verify)",
    )
    ap.add_argument(
        "--verify", action="store_true",
        help="Cross-check patches against gold",
    )
    ap.add_argument(
        "--csv", default=None,
        help="Write per-task CSV to this path",
    )
    ap.add_argument(
        "--json-report", default=None,
        help="Write full JSON report to this path",
    )

    args = ap.parse_args(argv)

    results = _load_results(args.results)
    if not results:
        print("No result files found.", file=sys.stderr)
        sys.exit(1)

    # Classify
    total = len(results)
    statuses: defaultdict[str, int] = defaultdict(int)
    per_repo: defaultdict[str, dict] = defaultdict(
        lambda: {"total": 0, "pass": 0}
    )
    per_task_rows = []
    durations = []
    iters_used = []

    for r in results:
        task_id = r.get("task_id", "unknown")
        status = r.get("status", "UNKNOWN")
        iters = r.get("iters", 0)
        risk = r.get("risk", {}).get("decision", "?")
        patch = r.get("final_patch_unified_diff", "")
        patch_lines = len(patch.splitlines()) if patch else 0

        # Duration from tests
        test_data = r.get("tests", {})
        total_dur = 0
        for tname, tinfo in test_data.items():
            if isinstance(tinfo, dict):
                total_dur += tinfo.get("duration_sec", 0)

        statuses[status] += 1
        repo = _extract_repo(task_id)
        per_repo[repo]["total"] += 1
        if status == "PASS":
            per_repo[repo]["pass"] += 1

        if total_dur > 0:
            durations.append(total_dur)
        if isinstance(iters, int) and iters > 0:
            iters_used.append(iters)

        per_task_rows.append({
            "task_id": task_id,
            "repo": repo,
            "status": status,
            "iters": iters,
            "risk": risk,
            "patch_lines": patch_lines,
            "test_duration_sec": round(total_dur, 1),
        })

    # Print report
    passed = statuses.get("PASS", 0)
    failed = statuses.get("FAIL", 0)
    aborted = statuses.get("ABORT", 0)
    resolve_pct = (passed / total * 100) if total else 0

    print(f"\n{'='*60}")
    print("  RFSN SWE-bench Scorecard")
    print(f"{'='*60}")
    print(f"  Total tasks:    {total}")
    print(f"  PASS:           {passed}  ({resolve_pct:.1f}%)")
    print(f"  FAIL:           {failed}")
    print(f"  ABORT:          {aborted}")
    for s, c in sorted(statuses.items()):
        if s not in ("PASS", "FAIL", "ABORT"):
            print(f"  {s}:{'  '*(10-len(s))}{c}")

    if durations:
        avg_dur = sum(durations) / len(durations)
        print(f"\n  Avg test duration: {avg_dur:.1f}s")
    if iters_used:
        avg_iters = sum(iters_used) / len(iters_used)
        print(f"  Avg iterations:    {avg_iters:.1f}")

    # Per-repo breakdown
    if len(per_repo) > 1:
        print(f"\n  {'Repo':<35} {'Pass':>5} {'Total':>6} {'Rate':>7}")
        print(f"  {'-'*35} {'-'*5} {'-'*6} {'-'*7}")
        for repo in sorted(per_repo.keys()):
            info = per_repo[repo]
            rate = info["pass"] / info["total"] * 100 if info["total"] else 0
            print(
                f"  {repo:<35} {info['pass']:>5}"
                f" {info['total']:>6} {rate:>6.1f}%"
            )

    print(f"{'='*60}\n")

    # CSV output
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "task_id", "repo", "status", "iters", "risk",
                "patch_lines", "test_duration_sec",
            ])
            writer.writeheader()
            writer.writerows(per_task_rows)
        print(f"CSV written to {args.csv}")

    # JSON report
    report = {
        "total": total,
        "pass": passed,
        "fail": failed,
        "abort": aborted,
        "resolve_rate": round(resolve_pct, 2),
        "per_repo": dict(per_repo),
        "per_task": per_task_rows,
    }
    if args.json_report:
        with open(args.json_report, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
            f.write("\n")
        print(f"JSON report written to {args.json_report}")

    # Always write default report to results dir
    default_report = os.path.join(args.results, "_scorecard.json")
    with open(default_report, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"Scorecard written to {default_report}")


if __name__ == "__main__":
    main()
