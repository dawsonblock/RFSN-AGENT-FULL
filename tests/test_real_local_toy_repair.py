"""tests/test_real_local_toy_repair.py

Phase 10 — Real local toy-repair integration test.

Tests the full repair loop end-to-end without HTTP services:
  1. A toy repo is created in ``tmp_path`` with a seeded arithmetic bug.
  2. The orchestrator's ``run_logic`` is called with a ``manual_plan``.
  3. The actual ``rfsn_swebench.patcher`` and subprocess ``pytest`` run —
     no mocks for the patcher, test runner, or filesystem changes.
  4. The test verifies that:
     - the source file was modified (before_hash != after_hash)
     - the tests pass after the repair
     - the replay ledger contains the required events

Allowed mocks
-------------
- ``executor_client.run_step`` → replaced by ``local_executor.make_local_run_step``
  (HTTP transport only; real patch + test execution still occurs)
- ``sandbox_create`` / ``sandbox_destroy`` → no-ops (no Docker in CI)
- ``init_replay_manifest`` / ``finalize_replay_manifest`` → captured in-memory

Not mocked
----------
- ``rfsn_swebench.patcher.apply_unified_diff``  (real file write)
- ``subprocess.run`` for pytest  (real test execution)
- Filesystem reads / writes in ``tmp_path``
"""

from __future__ import annotations

import hashlib
import os
import sys
import textwrap
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch, MagicMock

import pytest

# Ensure repo root is on sys.path.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rfsn_kernel.kernel import HardKernel
from rfsn_kernel.local_executor import make_local_run_step
from services.orchestrator.run_engine import RunReq, run_logic
from services.orchestrator.kernel_bridge import LedgerSink


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        h.update(fh.read())
    return h.hexdigest()


class CapturingLedger:
    def __init__(self):
        self.events: List[Dict[str, Any]] = []

    def append(self, event: dict) -> None:
        self.events.append(event)

    def event_types(self) -> List[str]:
        return [e.get("type", "") for e in self.events]


def _make_kernel(tmp_path: Path) -> HardKernel:
    return HardKernel(ledger_path=str(tmp_path / "kernel_ledger.jsonl"))


def _make_ledger_sink(kernel: HardKernel) -> tuple:
    capturing = CapturingLedger()
    sink = MagicMock(spec=LedgerSink)
    sink.append = capturing.append
    return sink, capturing


def _build_toy_repo(repo_root: Path) -> None:
    """Create a minimal toy repo with a seeded bug."""
    src_dir = repo_root / "src"
    tests_dir = repo_root / "tests"
    src_dir.mkdir(parents=True)
    tests_dir.mkdir(parents=True)

    # Buggy source file: returns `a - b` instead of `a + b`.
    (src_dir / "math_bug.py").write_text(textwrap.dedent("""\
        def add(a, b):
            return a - b
    """))

    # Test file that asserts correct addition.
    (tests_dir / "test_math_bug.py").write_text(textwrap.dedent("""\
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
        from math_bug import add


        def test_add():
            assert add(2, 3) == 5, "expected 5"
            assert add(0, 0) == 0, "expected 0"
            assert add(-1, 1) == 0, "expected 0"
    """))


def _make_unified_diff(repo_root: Path) -> str:
    """Build the minimal unified diff that fixes the bug."""
    return textwrap.dedent(f"""\
        --- a/src/math_bug.py
        +++ b/src/math_bug.py
        @@ -1,2 +1,2 @@
         def add(a, b):
        -    return a - b
        +    return a + b
    """)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRealLocalToyRepair:
    """End-to-end repair loop using local dispatch (no HTTP services)."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        self.tmp_path = tmp_path
        self.repo_root = tmp_path / "toy_repo"
        self.repo_root.mkdir()
        _build_toy_repo(self.repo_root)
        self.src_file = self.repo_root / "src" / "math_bug.py"
        self.before_hash = _sha256(str(self.src_file))

    def _run_repair(self, plan: list) -> tuple:
        """Run run_logic with *plan* and the local executor wired in."""
        kernel = _make_kernel(self.tmp_path)
        sink, capturing = _make_ledger_sink(kernel)

        req = RunReq(
            repo_id="toy_repo",
            task="Fix the addition bug in src/math_bug.py",
            manual_plan=plan,
            max_iters=10,
        )

        local_run_step = make_local_run_step(str(self.repo_root))

        with (
            # run_step is imported directly into kernel_bridge; patch it there.
            patch("services.orchestrator.kernel_bridge.run_step", side_effect=local_run_step),
            patch("services.orchestrator.run_engine.sandbox_create", return_value=None),
            patch("services.orchestrator.run_engine.sandbox_destroy", return_value=None),
            patch("services.orchestrator.run_engine.init_replay_manifest", return_value={"run_id": "toy-001"}),
            patch("services.orchestrator.run_engine.finalize_replay_manifest"),
        ):
            result = run_logic("toy-repair-001", req, kernel, sink)

        return result, capturing

    def test_file_content_changes_after_repair(self):
        """After applying the patch, src/math_bug.py must differ from the original."""
        plan = [
            {"type": "apply_patch", "patch": _make_unified_diff(self.repo_root)},
        ]
        result, ledger = self._run_repair(plan)

        after_hash = _sha256(str(self.src_file))
        assert after_hash != self.before_hash, (
            "File hash unchanged — patch was not applied"
        )

    def test_file_no_longer_contains_bug(self):
        """After the patch, the buggy line must be replaced."""
        plan = [
            {"type": "apply_patch", "patch": _make_unified_diff(self.repo_root)},
        ]
        self._run_repair(plan)

        content = self.src_file.read_text()
        assert "return a + b" in content, "Fixed line not found in patched file"
        assert "return a - b" not in content, "Buggy line still present after patch"

    def test_tests_pass_after_repair(self):
        """pytest must pass on tests/test_math_bug.py after the fix."""
        plan = [
            {"type": "apply_patch", "patch": _make_unified_diff(self.repo_root)},
            {
                "type": "run_tests",
                "template_id": "pytest",
                "template_params": {"path": "tests/test_math_bug.py"},
                "timeout_s": 30,
            },
        ]
        result, ledger = self._run_repair(plan)

        # The run should complete, not error.
        assert result["status"] in ("completed", "max_iterations"), (
            f"Unexpected run status: {result['status']} — {result.get('reason')}"
        )

        # The run_tests step must have succeeded.
        step_oks = [
            e for e in ledger.events
            if e.get("type") == "STEP_OK"
            and e.get("result", {}).get("ok")
        ]
        assert len(step_oks) >= 2, (
            "Expected at least 2 STEP_OK events (apply_patch + run_tests)"
        )

    def test_replay_ledger_has_required_events(self):
        """Replay log must contain RUN_START and STEP_OK events."""
        plan = [
            {"type": "apply_patch", "patch": _make_unified_diff(self.repo_root)},
        ]
        result, ledger = self._run_repair(plan)

        event_types = ledger.event_types()
        assert "RUN_START" in event_types, "RUN_START event missing from ledger"
        assert "STEP_OK" in event_types, "STEP_OK event missing from ledger"

    def test_replay_ledger_has_sandbox_created_event(self):
        """Replay log must contain SANDBOX_CREATED event."""
        plan = [
            {"type": "apply_patch", "patch": _make_unified_diff(self.repo_root)},
        ]
        result, ledger = self._run_repair(plan)

        assert "SANDBOX_CREATED" in ledger.event_types(), (
            "SANDBOX_CREATED event missing from ledger"
        )

    def test_run_status_is_completed(self):
        """run_logic must return status='completed' when all steps succeed."""
        plan = [
            {"type": "apply_patch", "patch": _make_unified_diff(self.repo_root)},
        ]
        result, _ = self._run_repair(plan)

        assert result["status"] == "completed", (
            f"Expected 'completed', got {result['status']!r}: {result.get('reason')}"
        )

    def test_before_and_after_hashes_differ(self):
        """Explicit hash comparison: before_hash != after_hash."""
        plan = [
            {"type": "apply_patch", "patch": _make_unified_diff(self.repo_root)},
        ]
        self._run_repair(plan)

        after_hash = _sha256(str(self.src_file))
        assert self.before_hash != after_hash
        # Sanity: hash is deterministic.
        assert after_hash == _sha256(str(self.src_file))

    def test_partial_plan_stops_on_policy_violation(self):
        """A plan with an unknown tool type must stop with policy_denied."""
        plan = [
            {"type": "apply_patch", "patch": _make_unified_diff(self.repo_root)},
            {"type": "command", "cmd": "echo done"},  # Not in canonical registry.
        ]
        result, ledger = self._run_repair(plan)

        # First step (apply_patch) should succeed.
        # Second step (command) must trigger policy_denied.
        assert result["status"] == "policy_denied", (
            f"Expected policy_denied, got {result['status']!r}"
        )
        assert "POLICY_DENIED" in ledger.event_types()
