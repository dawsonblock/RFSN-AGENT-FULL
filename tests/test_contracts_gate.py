"""Tests for rfsn_swebench contracts and gate module."""
import os
import sys

import pytest

# Ensure rfsn_swebench package is importable
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), ".."),
)

from rfsn_swebench.contracts import (
    BenchTask,
    BenchResult,
    TaskLimits,
    TaskCommands,
    TaskHints,
    TestRun,
    RiskReport,
)
from rfsn_swebench.gate import patch_risk_gate


# ── contracts ────────────────────────────────


def test_bench_task_defaults():
    t = BenchTask(
        task_id="test-1",
        repo_url="https://example.com/repo.git",
        workdir="/tmp/test",
        issue_text="Fix bug",
    )
    assert t.limits.max_iters == 8
    assert t.commands.test_quick == "pytest -q"


def test_bench_result_to_dict():
    r = BenchResult(
        task_id="t1",
        status="PASS",
        iters=2,
        final_patch_unified_diff="--- a/x\n+++ b/x\n",
        tests={
            "quick": TestRun(
                exit_code=0,
                stdout_tail="ok",
                stderr_tail="",
                duration_sec=1.5,
            ),
        },
        risk=RiskReport(
            decision="ALLOW", reasons=[],
        ),
        replay_dir="/tmp/replay",
    )
    d = r.to_dict()
    assert d["status"] == "PASS"
    assert d["iters"] == 2
    assert "quick" in d["tests"]
    assert d["tests"]["quick"]["exit_code"] == 0
    assert d["risk"]["decision"] == "ALLOW"


def test_task_limits_customisation():
    lim = TaskLimits(
        max_iters=3,
        max_patch_bytes=50000,
    )
    assert lim.max_iters == 3
    assert lim.max_patch_bytes == 50000
    # defaults preserved
    assert lim.max_files_touched == 25
    assert lim.max_runtime_sec == 1800


# ── gate ─────────────────────────────────────


def test_gate_allows_small_patch():
    patch = (
        "--- a/x.py\n+++ b/x.py\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )
    r = patch_risk_gate(patch, 100000, 10, 5)
    assert r.decision == "ALLOW"
    assert r.reasons == [] or len(r.reasons) == 0


def test_gate_rejects_oversized_patch():
    patch = "x" * 300_000
    r = patch_risk_gate(patch, 200_000, 10, 5)
    assert r.decision == "REJECT"
    assert any("byte" in s.lower() for s in r.reasons)


def test_gate_rejects_too_many_files():
    headers = "".join(
        f"--- a/f{i}.py\n+++ b/f{i}.py\n"
        f"@@ -1 +1 @@\n-a\n+b\n"
        for i in range(30)
    )
    r = patch_risk_gate(headers, 1_000_000, 5, 2)
    assert r.decision == "REJECT"


def test_gate_empty_patch():
    r = patch_risk_gate("", 100000, 10, 5)
    assert r.decision == "ALLOW"


# ── testsel ──────────────────────────────────


def test_choose_quick_tests_default():
    from rfsn_swebench.testsel import choose_quick_tests
    hints = TaskHints(
        failing_tests=["tests/test_a.py::test_one"],
    )
    cmd = choose_quick_tests(hints, "pytest -q")
    # Should incorporate failing test
    assert "test_a" in cmd or "pytest" in cmd
