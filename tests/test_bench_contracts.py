"""Tests for rfsn_swebench.contracts — dataclass serialization."""
from __future__ import annotations

from rfsn_swebench.contracts import (
    BenchResult,
    BenchTask,
    RiskReport,
    TaskCommands,
    TaskHints,
    TaskLimits,
    TestRun,
)


def test_bench_result_to_dict_roundtrip():
    result = BenchResult(
        task_id="t-001",
        status="PASS",
        iters=2,
        final_patch_unified_diff="--- a/f.py\n+++ b/f.py\n+x=1\n",
        tests={
            "quick": TestRun(
                exit_code=0, stdout_tail="ok",
                stderr_tail="", duration_sec=1.5,
            ),
            "full": TestRun(
                exit_code=0, stdout_tail="ok",
                stderr_tail="", duration_sec=10.2,
            ),
        },
        risk=RiskReport(decision="ALLOW", reasons=[]),
        replay_dir="/tmp/replays/t-001_1234",
    )
    d = result.to_dict()

    assert d["task_id"] == "t-001"
    assert d["status"] == "PASS"
    assert d["iters"] == 2
    assert "final_patch_unified_diff" in d
    assert d["tests"]["quick"]["exit_code"] == 0
    assert d["tests"]["full"]["duration_sec"] == 10.2
    assert d["risk"]["decision"] == "ALLOW"
    assert d["risk"]["reasons"] == []
    assert d["replay_dir"] == "/tmp/replays/t-001_1234"


def test_bench_result_fail_status():
    result = BenchResult(
        task_id="t-002",
        status="FAIL",
        iters=8,
        final_patch_unified_diff="",
        tests={"quick": TestRun(
            exit_code=1, stdout_tail="",
            stderr_tail="err", duration_sec=0.5,
        )},
        risk=RiskReport(decision="REJECT", reasons=["too large"]),
        replay_dir="/tmp/r",
    )
    d = result.to_dict()
    assert d["status"] == "FAIL"
    assert d["risk"]["decision"] == "REJECT"
    assert "too large" in d["risk"]["reasons"]


def test_bench_task_defaults():
    task = BenchTask(
        task_id="t-003",
        repo_url="https://github.com/x/y.git",
        workdir="/tmp/w",
        issue_text="fix it",
    )
    assert task.repo_ref is None
    assert task.hints.failing_tests == []
    assert task.hints.focus_files == []
    assert task.commands.test_quick == "pytest -q"
    assert task.commands.test_full == "pytest -q"
    assert task.limits.max_iters == 8
    assert task.limits.max_patch_bytes == 250_000


def test_task_limits_custom():
    lim = TaskLimits(
        max_iters=3, max_patch_bytes=100,
        max_files_touched=2, max_new_files=0,
        max_runtime_sec=60,
    )
    assert lim.max_iters == 3
    assert lim.max_runtime_sec == 60


def test_task_commands_custom():
    cmd = TaskCommands(
        setup=["pip install -e ."],
        test_quick="pytest -x",
        test_full="pytest --tb=short",
    )
    assert len(cmd.setup) == 1
    assert cmd.test_quick == "pytest -x"


def test_task_hints_custom():
    hints = TaskHints(
        failing_tests=["tests/test_a.py::test_b"],
        focus_files=["src/a.py"],
    )
    assert hints.failing_tests == ["tests/test_a.py::test_b"]
    assert hints.focus_files == ["src/a.py"]


def test_risk_report_reject():
    r = RiskReport(decision="REJECT", reasons=["banned", "too big"])
    assert r.decision == "REJECT"
    assert len(r.reasons) == 2


def test_risk_report_allow_default():
    r = RiskReport(decision="ALLOW")
    assert r.reasons == []
