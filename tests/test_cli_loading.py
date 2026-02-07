"""Tests for CLI task loading and proposer selection."""
import json
import os
import sys

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), ".."),
)

from rfsn_swebench.cli import load_task  # noqa: E402


# ── task loading ─────────────────────────────


def test_load_task_valid(tmp_path):
    task = {
        "task_id": "test__test-1",
        "repo_url": "https://example.com/repo.git",
        "workdir": "/tmp/test",
        "issue_text": "Fix the bug",
        "hints": {
            "failing_tests": [
                "tests/test_x.py::test_one",
            ],
            "focus_files": ["src/x.py"],
        },
        "commands": {
            "setup": ["pip install -e ."],
            "test_quick": "pytest -x -q",
            "test_full": "pytest -x -q",
        },
        "limits": {
            "max_iters": 3,
            "max_patch_bytes": 50000,
            "max_files_touched": 5,
            "max_new_files": 2,
            "max_runtime_sec": 600,
        },
    }
    p = tmp_path / "task.json"
    p.write_text(json.dumps(task))
    t = load_task(str(p))
    assert t.task_id == "test__test-1"
    assert t.limits.max_iters == 3
    assert len(t.hints.failing_tests) == 1
    assert t.commands.test_quick == "pytest -x -q"


def test_load_task_defaults(tmp_path):
    task = {
        "task_id": "test__test-2",
        "repo_url": "https://example.com/repo.git",
        "workdir": "/tmp/test",
        "issue_text": "Fix the bug",
    }
    p = tmp_path / "task.json"
    p.write_text(json.dumps(task))
    t = load_task(str(p))
    assert t.limits.max_iters == 8
    assert t.limits.max_runtime_sec == 1800


def test_load_task_with_test_patch(tmp_path):
    task = {
        "task_id": "test__test-3",
        "repo_url": "https://example.com/r.git",
        "workdir": "/tmp/test",
        "issue_text": "Fix it",
        "hints": {
            "test_patch": (
                "--- a/tests/t.py\n"
                "+++ b/tests/t.py\n"
                "@@ -1 +1 @@\n"
                "-old\n+new\n"
            ),
        },
    }
    p = tmp_path / "task.json"
    p.write_text(json.dumps(task))
    t = load_task(str(p))
    assert "--- a/tests" in t.hints.test_patch


# ── existing task files are valid ────────────

TASKS_DIR = os.path.join(
    os.path.dirname(__file__),
    "..", "data", "tasks",
)


def _task_files():
    if not os.path.isdir(TASKS_DIR):
        return []
    return [
        os.path.join(TASKS_DIR, f)
        for f in sorted(os.listdir(TASKS_DIR))
        if f.startswith("task_") and f.endswith(".json")
    ]


@pytest.mark.parametrize("path", _task_files())
def test_all_task_files_load(path):
    """Every task JSON under data/tasks/ must parse
    and have required fields."""
    t = load_task(path)
    assert t.task_id
    assert t.repo_url
    assert t.issue_text
    assert t.limits.max_iters > 0
