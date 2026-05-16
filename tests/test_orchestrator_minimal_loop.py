"""tests/test_orchestrator_minimal_loop.py

Tests for the minimal bounded repair loop in run_engine.py.

Acceptance:
* No planner configured returns dry_run, not fake success.
* Manual plan can execute steps.
* Unsafe action stops with policy denial.
* No-op patch stops.
* Max iterations stops.
* Replay log is written (via ledger).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ROOT = str(Path(__file__).resolve().parent.parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from rfsn_kernel.kernel import HardKernel
from services.orchestrator.kernel_bridge import LedgerSink


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class CapturingLedger:
    """Simple in-memory ledger for testing."""

    def __init__(self):
        self.events = []

    def append(self, event: dict) -> None:
        self.events.append(event)

    def event_types(self):
        return [e.get("type") for e in self.events]


def _make_kernel(tmp_path):
    """Create a minimal HardKernel for testing with tmp ledger path."""
    return HardKernel(ledger_path=str(tmp_path / "kernel_ledger.jsonl"))


def _make_ledger(kernel=None):
    """Create a MagicMock sink backed by a CapturingLedger."""
    ledger = CapturingLedger()
    sink = MagicMock()
    sink.append = ledger.append
    sink._events = ledger
    return sink, ledger


def _noop_sandbox(*args, **kwargs):
    return None


# ---------------------------------------------------------------------------
# Shared autouse patch
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def patch_external():
    """Patch out sandbox, executor client, and replay manager calls."""
    with (
        patch("services.orchestrator.run_engine.sandbox_create", side_effect=_noop_sandbox),
        patch("services.orchestrator.run_engine.sandbox_destroy", side_effect=_noop_sandbox),
        patch("services.orchestrator.run_engine.init_replay_manifest", return_value={"run_id": "x"}),
        patch("services.orchestrator.run_engine.finalize_replay_manifest"),
    ):
        yield


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_no_planner_returns_dry_run(self, tmp_path):
        """Without manual_plan, engine must return dry_run status."""
        from services.orchestrator.run_engine import run_logic, RunReq

        kernel = _make_kernel(tmp_path)
        sink, ledger = _make_ledger(kernel)
        req = RunReq(repo_id="repo1", task="fix the bug")
        result = run_logic("run-dry-001", req, kernel, sink)
        assert result["status"] == "dry_run"
        assert "dry" in result["reason"].lower() or "planner" in result["reason"].lower()

    def test_dry_run_writes_event_to_ledger(self, tmp_path):
        """dry_run must emit a DRY_RUN event to the ledger."""
        from services.orchestrator.run_engine import run_logic, RunReq

        kernel = _make_kernel(tmp_path)
        sink, ledger = _make_ledger(kernel)
        req = RunReq(repo_id="repo1", task="fix the bug")
        run_logic("run-dry-002", req, kernel, sink)
        assert "DRY_RUN" in ledger.event_types()

    def test_dry_run_does_not_claim_completed(self, tmp_path):
        """Dry-run must never return status='completed'."""
        from services.orchestrator.run_engine import run_logic, RunReq

        kernel = _make_kernel(tmp_path)
        sink, _ = _make_ledger(kernel)
        req = RunReq(repo_id="repo1", task="fix the bug")
        result = run_logic("run-dry-003", req, kernel, sink)
        assert result["status"] != "completed"


class TestManualPlan:
    def _mock_execute_ok(self, **kwargs):
        return {"ok": True, "output": "done"}

    def _mock_execute_noop(self, **kwargs):
        return {"ok": False, "reason": "no_op patch detected"}

    def test_manual_plan_read_step_executes(self, tmp_path):
        """A valid read_file step in manual_plan should execute."""
        from services.orchestrator.run_engine import run_logic, RunReq

        kernel = _make_kernel(tmp_path)
        sink, ledger = _make_ledger(kernel)
        req = RunReq(
            repo_id="repo1",
            task="read the file",
            manual_plan=[{"type": "read_file", "path": "src/main.py"}],
        )
        with patch(
            "services.orchestrator.run_engine.execute_approved_step",
            side_effect=self._mock_execute_ok,
        ):
            result = run_logic("run-plan-001", req, kernel, sink)

        assert result["status"] == "completed"
        assert "STEP_OK" in ledger.event_types()
        assert "STEP_FAILED" not in ledger.event_types()

    def test_replay_log_is_written(self, tmp_path):
        """RUN_START event must always be written."""
        from services.orchestrator.run_engine import run_logic, RunReq

        kernel = _make_kernel(tmp_path)
        sink, ledger = _make_ledger(kernel)
        req = RunReq(repo_id="repo1", task="dummy", manual_plan=[])
        run_logic("run-log-001", req, kernel, sink)
        assert "RUN_START" in ledger.event_types()


class TestPolicyDenial:
    def test_unknown_tool_stops_with_policy_denied(self, tmp_path):
        """A step with an unknown tool type must stop with policy_denied."""
        from services.orchestrator.run_engine import run_logic, RunReq

        kernel = _make_kernel(tmp_path)
        sink, ledger = _make_ledger(kernel)
        req = RunReq(
            repo_id="repo1",
            task="hack",
            manual_plan=[{"type": "command", "cmd": "echo done"}],
        )
        result = run_logic("run-deny-001", req, kernel, sink)
        assert result["status"] == "policy_denied"
        assert "POLICY_DENIED" in ledger.event_types()

    def test_trace_execution_stops_with_policy_denied(self, tmp_path):
        """trace_execution (disabled) must stop with policy_denied."""
        from services.orchestrator.run_engine import run_logic, RunReq

        kernel = _make_kernel(tmp_path)
        sink, ledger = _make_ledger(kernel)
        req = RunReq(
            repo_id="repo1",
            task="trace",
            manual_plan=[{"type": "trace_execution", "path": "x.py"}],
        )
        result = run_logic("run-deny-002", req, kernel, sink)
        assert result["status"] == "policy_denied"
        assert "trace_execution" in result["reason"]


class TestNoOpStopped:
    def test_noop_patch_stops_loop(self, tmp_path):
        """A no-op patch result must stop the loop with no_op_stopped."""
        from services.orchestrator.run_engine import run_logic, RunReq

        kernel = _make_kernel(tmp_path)
        sink, ledger = _make_ledger(kernel)
        req = RunReq(
            repo_id="repo1",
            task="fix",
            manual_plan=[{"type": "apply_patch", "patch": "--- a\n+++ b\n"}],
        )

        def _noop_result(**kwargs):
            return {"ok": False, "reason": "no_op patch: content unchanged"}

        with patch(
            "services.orchestrator.run_engine.execute_approved_step",
            side_effect=_noop_result,
        ):
            result = run_logic("run-noop-001", req, kernel, sink)

        assert result["status"] == "no_op_stopped"


class TestMaxIterations:
    def test_max_iterations_stops_loop(self, tmp_path):
        """Loop must stop at max_iters even if plan has more steps."""
        from services.orchestrator.run_engine import run_logic, RunReq

        kernel = _make_kernel(tmp_path)
        sink, ledger = _make_ledger(kernel)
        # 5 steps but max_iters=2.
        plan = [{"type": "read_file", "path": "x.py"}] * 5
        req = RunReq(repo_id="repo1", task="read a lot", manual_plan=plan, max_iters=2)

        call_count = {"n": 0}

        def _counting_execute(**kwargs):
            call_count["n"] += 1
            return {"ok": True}

        with patch(
            "services.orchestrator.run_engine.execute_approved_step",
            side_effect=_counting_execute,
        ):
            result = run_logic("run-maxiter-001", req, kernel, sink)

        assert call_count["n"] <= 2
        assert result["status"] in ("max_iterations", "completed")
