#!/usr/bin/env python3
"""Convert upstream SWE-bench instances into RFSN task.json files.

Reads from HuggingFace ``princeton-nlp/SWE-bench_Lite`` (300 tasks) or
``princeton-nlp/SWE-bench_Verified`` (500 human-validated tasks) and writes
one ``task_<instance_id>.json`` per instance into the output directory.

Usage
-----
    # Convert all of SWE-bench_Lite
    python scripts/convert_swebench_tasks.py --dataset Lite --out data/tasks

    # Convert SWE-bench_Verified, filter to only sympy tasks
    python scripts/convert_swebench_tasks.py \\
        --dataset Verified --out data/tasks \\
        --repo-filter sympy/sympy

    # Convert only a subset (first N)
    python scripts/convert_swebench_tasks.py \\
        --dataset Lite --out data/tasks --limit 10

    # Convert with a specific difficulty ceiling (Verified only)
    python scripts/convert_swebench_tasks.py \\
        --dataset Verified --out data/tasks \\
        --max-difficulty 15

Each output file conforms to RFSN's shared/task_schema.json.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Upstream field → RFSN mapping
# ---------------------------------------------------------------------------
# SWE-bench columns:
#   repo, instance_id, base_commit, patch, test_patch,
#   problem_statement, hints_text, created_at, version,
#   FAIL_TO_PASS, PASS_TO_PASS, environment_setup_commit
#   difficulty  (Verified only)

# Known per-repo setup commands.  Real SWE-bench repos need complex conda
# environments.  For a lightweight first pass we use `pip install -e .[dev]`
# with a fallback to `pip install -e .`.  For repos that need specific extra
# setup (C extensions, etc.) callers can extend this mapping.
_REPO_SETUP: dict[str, list[str]] = {
    "django/django": [
        "pip install -e .[argon2,bcrypt]",
    ],
    "sympy/sympy": [
        "pip install -e .",
    ],
    "scikit-learn/scikit-learn": [
        "pip install -e .[tests]",
    ],
    "matplotlib/matplotlib": [
        "pip install -e .[dev]",
    ],
    "sphinx-doc/sphinx": [
        "pip install -e .[test]",
    ],
    "pytest-dev/pytest": [
        "pip install -e .[testing]",
    ],
    "astropy/astropy": [
        "pip install -e .[test]",
    ],
    "pydata/xarray": [
        "pip install -e .",
    ],
    "pylint-dev/pylint": [
        "pip install -e .[testutils]",
    ],
    "psf/requests": [
        "pip install -e .[socks]",
        "pip install pytest-httpbin trustme",
    ],
    "pallets/flask": [
        "pip install -e .[dev]",
    ],
    "mwaskom/seaborn": [
        "pip install -e .[dev]",
    ],
}

# Default generous limits for real SWE-bench tasks (much harder than demos).
_DEFAULT_LIMITS = {
    "max_iters": 3,
    "max_patch_bytes": 100_000,
    "max_files_touched": 10,
    "max_new_files": 3,
    "max_runtime_sec": 600,
}


def _sanitize_id(instance_id: str) -> str:
    """Make instance_id safe for filesystem and RFSN repo_id validation."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", instance_id)


def _parse_fail_to_pass(raw: str) -> list[str]:
    """Parse FAIL_TO_PASS — stored as a JSON-encoded string list."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(t) for t in parsed]
    except (json.JSONDecodeError, TypeError):
        pass
    # Fallback: treat as comma-separated
    return [
        t.strip().strip('"').strip("'")
        for t in raw.split(",") if t.strip()
    ]


def _extract_test_files(test_nodes: list[str]) -> list[str]:
    """Extract unique test file paths from pytest node IDs.

    Handles multiple upstream formats:
    - Full path: ``astropy/modeling/tests/test_separable.py::test_foo``
    - Just function: ``test_ccode_sinc`` (sympy-style)
    - Unittest-style: ``test_foo (module.TestClass)`` (Django-style)
    """
    files = []
    seen = set()
    for node in test_nodes:
        if "::" in node:
            fpath = node.split("::")[0]
        elif ".py" in node:
            fpath = node.split(".py")[0] + ".py"
        else:
            # Bare function name or unittest-style — can't extract file
            continue
        if fpath not in seen:
            seen.add(fpath)
            files.append(fpath)
    return files


def _classify_test_format(failing_tests: list[str]) -> str:
    """Detect the test specification format used by the repo.

    Returns one of:
    - 'pytest_node': full ``path/test_file.py::TestClass::test_func``
    - 'bare_func':   just ``test_func_name`` (no file or class)
    - 'unittest':    ``test_method (module.TestClass)``
    - 'mixed':       couldn't classify uniformly
    """
    if not failing_tests:
        return "pytest_node"

    has_doublecolon = any("::" in t for t in failing_tests)
    has_paren = any("(" in t for t in failing_tests)
    has_file_path = any(
        ".py" in t.split("::")[0]
        for t in failing_tests if "::" in t
    )

    if has_doublecolon and has_file_path:
        return "pytest_node"
    if has_paren:
        return "unittest"
    return "bare_func"


def _build_quick_test_cmd(failing_tests: list[str], repo: str) -> str:
    """Build a targeted pytest command for the known-failing tests.

    Handles different test ID formats across SWE-bench repos.
    """
    if not failing_tests:
        return "python -m pytest -x -q"

    fmt = _classify_test_format(failing_tests)

    if fmt == "pytest_node":
        # Full node IDs — pass directly to pytest
        return f"python -m pytest -x -q {' '.join(failing_tests)}"

    if fmt == "unittest":
        # Django-style: ``test_method (app.tests.TestClass)``
        # Extract module paths for targeted test discovery
        modules = set()
        for t in failing_tests:
            if "(" in t:
                # "test_foo (app.tests.TestClass)" → "app.tests"
                inner = t.split("(")[1].rstrip(")")
                parts = inner.rsplit(".", 1)
                modules.add(parts[0] if len(parts) > 1 else inner)
        if modules:
            # Convert dotted module to path: app.tests → app/tests.py
            paths = []
            for m in modules:
                path = m.replace(".", "/") + ".py"
                paths.append(path)
            return f"python -m pytest -x -q {' '.join(paths)}"
        return "python -m pytest -x -q"

    # bare_func — use -k filter to find by name
    func_names = []
    for t in failing_tests:
        # Strip any whitespace, just keep the function name
        name = t.strip().split()[0]
        func_names.append(name)
    k_expr = " or ".join(func_names)
    return f"python -m pytest -x -q -k \"{k_expr}\""


def _build_full_test_cmd(failing_tests: list[str], repo: str) -> str:
    """Build the full test command — run the specific FAIL_TO_PASS tests.

    For SWE-bench evaluation, "full" means running the exact tests that must
    change from FAIL to PASS.  This is the acceptance criterion.
    """
    if not failing_tests:
        return "python -m pytest -x -q"

    fmt = _classify_test_format(failing_tests)

    if fmt == "pytest_node":
        return f"python -m pytest -x -q {' '.join(failing_tests)}"

    if fmt == "unittest":
        modules = set()
        for t in failing_tests:
            if "(" in t:
                inner = t.split("(")[1].rstrip(")")
                parts = inner.rsplit(".", 1)
                modules.add(parts[0] if len(parts) > 1 else inner)
        if modules:
            paths = [m.replace(".", "/") + ".py" for m in modules]
            return f"python -m pytest -x -q {' '.join(paths)}"
        return "python -m pytest -x -q"

    # bare_func
    func_names = [t.strip().split()[0] for t in failing_tests]
    k_expr = " or ".join(func_names)
    return f"python -m pytest -x -q -k \"{k_expr}\""


def _extract_focus_files(patch: str) -> list[str]:
    """Extract source files touched by the gold patch (for hints)."""
    files = []
    for line in patch.splitlines():
        if line.startswith("diff --git a/"):
            parts = line.split()
            if len(parts) >= 4:
                fpath = parts[2].removeprefix("a/")
                # Skip test files — focus_files should be source files
                if "test" not in fpath.lower():
                    files.append(fpath)
    return files


def convert_instance(row: dict, workdir_base: str) -> dict:
    """Convert a single SWE-bench instance to RFSN task.json format."""
    instance_id = row["instance_id"]
    safe_id = _sanitize_id(instance_id)
    repo = row["repo"]

    # Parse FAIL_TO_PASS (stored as JSON-encoded string list)
    failing_tests = _parse_fail_to_pass(row.get("FAIL_TO_PASS", ""))

    # Extract focus files from the gold patch
    focus_files = _extract_focus_files(row.get("patch", ""))

    # Setup commands
    setup_cmds = _REPO_SETUP.get(repo, ["pip install -e ."])

    task = {
        "task_id": safe_id,
        "repo_url": f"https://github.com/{repo}.git",
        "repo_ref": row.get("base_commit", ""),
        "workdir": os.path.join(workdir_base, safe_id),
        "issue_text": row.get("problem_statement", ""),
        "hints": {
            "failing_tests": failing_tests,
            "focus_files": focus_files,
            "test_patch": row.get("test_patch", ""),
        },
        "commands": {
            "setup": setup_cmds,
            "test_quick": _build_quick_test_cmd(failing_tests, repo),
            "test_full": _build_full_test_cmd(failing_tests, repo),
        },
        "limits": dict(_DEFAULT_LIMITS),
    }

    return task


def main(argv: Optional[list[str]] = None) -> None:
    ap = argparse.ArgumentParser(
        description=(
            "Convert upstream SWE-bench instances"
            " to RFSN task.json files"
        ),
    )
    ap.add_argument(
        "--dataset",
        choices=["Lite", "Verified"],
        default="Lite",
        help="Which SWE-bench split to use (default: Lite)",
    )
    ap.add_argument(
        "--out",
        required=True,
        help="Output directory for task JSON files",
    )
    ap.add_argument(
        "--workdir-base",
        default="/tmp/swebench_work",
        help="Base directory for task workdirs (default: /tmp/swebench_work)",
    )
    ap.add_argument(
        "--repo-filter",
        default=None,
        help="Only convert tasks for this repo (e.g. sympy/sympy)",
    )
    ap.add_argument(
        "--instance-filter",
        default=None,
        help="Only convert a specific instance_id",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Convert at most N tasks",
    )
    ap.add_argument(
        "--max-difficulty",
        type=int,
        default=None,
        help="Only convert tasks with difficulty ≤ N (Verified only)",
    )
    ap.add_argument(
        "--max-iters",
        type=int,
        default=None,
        help="Override max_iters in limits",
    )
    ap.add_argument(
        "--max-runtime",
        type=int,
        default=None,
        help="Override max_runtime_sec in limits",
    )

    args = ap.parse_args(argv)

    # Load dataset
    try:
        from datasets import load_dataset  # type: ignore[import-not-found]
    except ImportError:
        print(
            "ERROR: 'datasets' package required. "
            "Install: pip install datasets",
            file=sys.stderr,
        )
        sys.exit(1)

    dataset_name = f"princeton-nlp/SWE-bench_{args.dataset}"
    print(f"Loading {dataset_name} ...", flush=True)
    ds = load_dataset(dataset_name, split="test")
    print(f"  {len(ds)} instances loaded", flush=True)

    # Filter
    instances: list[dict[str, Any]] = [dict(r) for r in ds]
    if args.repo_filter:
        instances = [r for r in instances if r["repo"] == args.repo_filter]
        print(
            f"  {len(instances)} after repo filter: "
            f"{args.repo_filter}",
            flush=True,
        )

    if args.instance_filter:
        instances = [
            r for r in instances
            if r["instance_id"] == args.instance_filter
        ]
        print(
            f"  {len(instances)} after instance filter: "
            f"{args.instance_filter}",
            flush=True,
        )

    if args.max_difficulty is not None:
        instances = [
            r for r in instances
            if r.get("difficulty", 0) <= args.max_difficulty
        ]
        print(
            f"  {len(instances)} after difficulty filter:"
            f" ≤{args.max_difficulty}",
            flush=True,
        )

    if args.limit:
        instances = instances[: args.limit]
        print(f"  limited to {len(instances)} tasks", flush=True)

    if not instances:
        print("No instances match filters. Exiting.", file=sys.stderr)
        sys.exit(1)

    # Convert and write
    os.makedirs(args.out, exist_ok=True)
    written = 0
    for row in instances:
        task = convert_instance(
            row, args.workdir_base,
        )

        # Override limits if requested
        if args.max_iters:
            task["limits"]["max_iters"] = args.max_iters
        if args.max_runtime:
            task["limits"]["max_runtime_sec"] = args.max_runtime

        safe_id = _sanitize_id(row["instance_id"])
        out_path = os.path.join(args.out, f"task_{safe_id}.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(task, f, indent=2, ensure_ascii=False)
            f.write("\n")
        written += 1

    print(f"\nWrote {written} task files to {args.out}/", flush=True)

    # Also write a manifest for the batch runner
    manifest = {
        "dataset": dataset_name,
        "filters": {
            "repo": args.repo_filter,
            "instance": args.instance_filter,
            "limit": args.limit,
            "max_difficulty": args.max_difficulty,
        },
        "count": written,
        "task_ids": [_sanitize_id(r["instance_id"]) for r in instances],
    }
    manifest_path = os.path.join(args.out, "_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"Wrote manifest to {manifest_path}", flush=True)


if __name__ == "__main__":
    main()
