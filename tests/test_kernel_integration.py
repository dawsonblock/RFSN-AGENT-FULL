"""Integration tests for HardKernel execution gating."""

from pathlib import Path

from rfsn_kernel.kernel import HardKernel
from rfsn_kernel.state import Outcome


def _ok_exec(_step):
    return Outcome(
        success=True,
        exit_code=0,
        logs="ok",
    )


def _fail_exec(_step):
    return Outcome(
        success=False,
        exit_code=1,
        logs="pytest failed with AssertionError",
    )


def _patch(path: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )


def _kernel(tmp_path: Path) -> HardKernel:
    return HardKernel(
        ledger_path=str(tmp_path / "kernel_ledger.jsonl"),
        policy={
            "risk_max": 1.0,
            "success_min": 0.1,
            "loop_max": 1.0,
            "drift_max": 1.0,
            "rng_seed": 1,
        },
    )


def test_rejects_unknown_action(tmp_path):
    k = _kernel(tmp_path)
    k.reset_for_run(run_id="r1")
    d = k.kernel_step(
        {
            "id": "s1",
            "type": "unknown_action",
        },
        execute_fn=_ok_exec,
        run_id="r1",
    )
    assert not d.approved
    assert d.phase == "VALIDATE"
    assert d.validation is not None
    assert d.validation.errors
    assert d.validation.errors[0]["code"] == "UNKNOWN_ACTION"


def test_rejects_empty_patch(tmp_path):
    k = _kernel(tmp_path)
    k.reset_for_run(run_id="r1")
    d = k.kernel_step(
        {
            "id": "s1",
            "type": "apply_patch",
            "patch": "",
        },
        execute_fn=_ok_exec,
        run_id="r1",
    )
    assert not d.approved
    assert d.validation is not None
    assert any(
        e["code"] == "EMPTY_PATCH"
        for e in d.validation.errors
    )


def test_approves_simple_search(tmp_path):
    k = _kernel(tmp_path)
    k.reset_for_run(run_id="r1")
    d = k.kernel_step(
        {
            "id": "s1",
            "type": "repo_search",
            "pattern": "foo",
        },
        execute_fn=_ok_exec,
        run_id="r1",
    )
    assert d.approved
    assert d.success


def test_tier0_rejects_test_file_edits(tmp_path):
    k = _kernel(tmp_path)
    k.reset_for_run(run_id="r1")
    d = k.kernel_step(
        {
            "id": "s1",
            "type": "apply_patch",
            "patch": _patch("tests/test_demo.py"),
        },
        execute_fn=_ok_exec,
        run_id="r1",
    )
    assert not d.approved
    assert d.decision is not None
    assert "tier forbids tests edits" in d.decision.reason


def test_tier_escalation_is_per_run(tmp_path):
    k = _kernel(tmp_path)
    k.reset_for_run(run_id="run-a")
    d1 = k.kernel_step(
        {
            "id": "s1",
            "type": "repo_search",
            "pattern": "foo",
        },
        execute_fn=_fail_exec,
        run_id="run-a",
    )
    assert d1.approved
    assert k.run_state.get("run-a").tier == 1

    k.reset_for_run(run_id="run-b")
    assert k.run_state.get("run-b").tier == 0


def test_records_run_id_in_ledger_metadata(tmp_path):
    k = _kernel(tmp_path)
    k.reset_for_run(run_id="r-123")
    d = k.kernel_step(
        {
            "id": "s1",
            "type": "repo_search",
            "pattern": "foo",
        },
        execute_fn=_ok_exec,
        run_id="r-123",
    )
    assert d.ledger_record is not None
    assert d.ledger_record.metadata["run_id"] == "r-123"


def test_end_run_clears_per_run_state(tmp_path):
    k = _kernel(tmp_path)
    k.reset_for_run(run_id="r1")
    _ = k.run_state.get("r1")
    assert "r1" in k.run_state.snapshot()
    k.end_run("r1")
    assert "r1" not in k.run_state.snapshot()
