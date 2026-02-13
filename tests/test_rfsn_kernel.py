"""Comprehensive tests for the rfsn_kernel package.

Covers every module: state, normalize, validate, simulate,
risk, decide, verify, hard_ledger, kernel, replay, memory,
planner.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

# ── Make rfsn_kernel importable ───────────────────────
ROOT = str(Path(__file__).resolve().parent.parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from rfsn_kernel.state import (
    Proposal,
    SystemState,
    Outcome,
)
from rfsn_kernel.normalize import (
    normalize,
    proposal_to_step,
)
from rfsn_kernel.validate import (
    validate,
    ValidationResult,
    VALID_ACTIONS,
)
from rfsn_kernel.simulate import (
    simulate,
    SimResult,
    OutcomeHistory,
)
from rfsn_kernel.risk import (
    risk_score,
    RiskBreakdown,
)
from rfsn_kernel.decide import (
    decide,
    Decision,
)
from rfsn_kernel.verify import (
    verify,
    VerificationResult,
)
from rfsn_kernel.hard_ledger import (
    HardLedger,
    LedgerRecord,
)
from rfsn_kernel.kernel import (
    HardKernel,
    KernelStepResult,
)
from rfsn_kernel.replay import (
    ReplayRunner,
    snapshot_environment,
)
from rfsn_kernel.memory import (
    MemoryImmuneSystem,
    MemoryEntry,
    MemoryDecision,
)
from rfsn_kernel.planner import (
    HierarchicalPlanner,
    Subgoal,
    TacticalPlan,
    StrategicState,
)


# ══════════════════════════════════════════════════════
# FIXTURES
# ══════════════════════════════════════════════════════


@pytest.fixture
def default_policy():
    return {
        "risk_max": 0.65,
        "success_min": 0.15,
        "loop_max": 0.8,
        "drift_max": 0.85,
        "risk_lambda": 0.7,
        "max_total_steps": 200,
        "history_max": 500,
        "fail_cluster_threshold": 8,
        "rng_seed": 42,
        "policy_hash": "test_hash",
    }


@pytest.fixture
def basic_state():
    return SystemState(rng_seed=42)


@pytest.fixture
def basic_proposal():
    """Proposal using a VALID action type."""
    return Proposal(
        action="repo_read_range",
        params={"path": "src/main.py", "line_start": 1, "line_end": 50},
        context_hash="ctx_abc",
        planner_hash="plan_xyz",
        intent="read source file",
        bundle_id="b1",
    )


@pytest.fixture
def basic_outcome():
    return Outcome(
        success=True,
        exit_code=0,
        payload="file contents here",
        logs="read 200 lines",
        duration_sec=0.5,
    )


@pytest.fixture
def failing_outcome():
    return Outcome(
        success=False,
        exit_code=1,
        payload="",
        logs="FileNotFoundError: ...",
        duration_sec=0.1,
        error="not found",
    )


@pytest.fixture
def tmp_ledger(tmp_path):
    return str(tmp_path / "test_ledger.jsonl")


# ══════════════════════════════════════════════════════
# 1. STATE MODULE
# ══════════════════════════════════════════════════════


class TestState:
    def test_proposal_deterministic_hash(
        self,
        basic_proposal,
    ):
        h1 = basic_proposal.deterministic_hash()
        h2 = basic_proposal.deterministic_hash()
        assert h1 == h2
        assert len(h1) == 64  # SHA-256

    def test_proposal_different_params_diff_hash(
        self,
    ):
        p1 = Proposal(
            action="repo_read_range",
            params={"path": "a.py"},
            context_hash="c1",
            planner_hash="p1",
        )
        p2 = Proposal(
            action="repo_read_range",
            params={"path": "b.py"},
            context_hash="c1",
            planner_hash="p1",
        )
        assert p1.deterministic_hash() != p2.deterministic_hash()

    def test_system_state_defaults(self, basic_state):
        assert basic_state.step_count == 0
        assert basic_state.iter_count == 0
        assert basic_state.total_cost == 0.0
        assert basic_state.safety_level == 0

    def test_system_state_record_action(
        self,
        basic_state,
    ):
        basic_state.record_action("repo_read_range")
        assert "repo_read_range" in basic_state.recent_actions

    def test_system_state_advance_step(
        self,
        basic_state,
    ):
        basic_state.advance_step()
        assert basic_state.step_count == 1

    def test_system_state_record_failure(
        self,
        basic_state,
    ):
        basic_state.record_failure()
        assert basic_state.recent_failures == 1

    def test_system_state_record_success(
        self,
        basic_state,
    ):
        basic_state.record_failure()
        basic_state.record_success()
        # record_success decrements recent_failures
        assert basic_state.recent_failures == 0

    def test_system_state_snapshot(self, basic_state):
        snap = basic_state.snapshot()
        assert "state_hash" in snap
        assert "step_count" in snap
        assert "rng_seed" in snap

    def test_system_state_deterministic_hash(
        self,
        basic_state,
    ):
        h1 = basic_state.deterministic_hash()
        h2 = basic_state.deterministic_hash()
        assert h1 == h2
        basic_state.advance_step()
        h3 = basic_state.deterministic_hash()
        assert h1 != h3

    def test_outcome_defaults(self, basic_outcome):
        assert basic_outcome.success is True
        assert basic_outcome.exit_code == 0

    def test_outcome_error(self, failing_outcome):
        assert failing_outcome.success is False
        assert failing_outcome.error == "not found"


# ══════════════════════════════════════════════════════
# 2. NORMALIZE MODULE
# ══════════════════════════════════════════════════════


class TestNormalize:
    def test_normalize_basic(self):
        raw = {
            "type": "repo_read_range",
            "path": "main.py",
            "line_start": 1,
            "line_end": 50,
        }
        p = normalize(raw, "read file", "ctx", "b1")
        assert isinstance(p, Proposal)
        assert p.action == "repo_read_range"
        assert p.params["path"] == "main.py"

    def test_normalize_strips_unknown_fields(self):
        raw = {
            "type": "repo_read_range",
            "path": "main.py",
            "unknown_field": "bad",
        }
        p = normalize(raw, "read", "c", "b")
        assert "unknown_field" not in p.params

    def test_normalize_unknown_action_passthrough(
        self,
    ):
        raw = {"type": "exotic_action", "x": 1}
        p = normalize(raw, "intent", "c", "b")
        assert p.action == "exotic_action"
        # Unknown actions have no allowed params,
        # so all extra fields are stripped.
        assert "x" not in p.params

    def test_proposal_to_step_roundtrip(self):
        raw = {
            "type": "repo_read_range",
            "path": "src/a.py",
            "line_start": 1,
            "line_end": 100,
        }
        p = normalize(raw, "intent", "c", "b")
        step = proposal_to_step(p)
        assert step["type"] == "repo_read_range"
        assert step["path"] == "src/a.py"

    def test_normalize_repo_search(self):
        raw = {
            "type": "repo_search",
            "pattern": "def foo",
            "timeout_s": 30,
        }
        p = normalize(raw, "search", "c", "b")
        assert p.action == "repo_search"
        assert p.params["pattern"] == "def foo"

    def test_normalize_apply_patch(self):
        raw = {
            "type": "apply_patch",
            "patch": "--- a/f.py\n+++ b/f.py\n",
            "timeout_s": 60,
        }
        p = normalize(raw, "patch", "c", "b")
        assert p.action == "apply_patch"
        assert "patch" in p.params


# ══════════════════════════════════════════════════════
# 3. VALIDATE MODULE
# ══════════════════════════════════════════════════════


class TestValidate:
    def test_validate_passes_normal_step(
        self,
        basic_state,
        default_policy,
    ):
        p = Proposal(
            action="repo_read_range",
            params={"path": "src/main.py", "line_start": 1, "line_end": 50},
            context_hash="c",
            planner_hash="p",
        )
        result = validate(
            p,
            basic_state,
            default_policy,
        )
        assert result.ok

    def test_validate_rejects_path_traversal(
        self,
        basic_state,
        default_policy,
    ):
        p = Proposal(
            action="repo_read_range",
            params={
                "path": "../../etc/passwd",
                "line_start": 1,
                "line_end": 10,
            },
            context_hash="c",
            planner_hash="p",
        )
        result = validate(
            p,
            basic_state,
            default_policy,
        )
        assert not result.ok
        assert any("PATH_TRAVERSAL" == e.get("code") for e in result.errors)

    def test_validate_rejects_over_budget(
        self,
        basic_state,
        default_policy,
    ):
        default_policy["max_total_steps"] = 5
        basic_state.step_count = 5
        p = Proposal(
            action="repo_read_range",
            params={"path": "a.py", "line_start": 1, "line_end": 10},
            context_hash="c",
            planner_hash="p",
        )
        result = validate(
            p,
            basic_state,
            default_policy,
        )
        assert not result.ok

    def test_validate_rejects_empty_patch(
        self,
        basic_state,
        default_policy,
    ):
        p = Proposal(
            action="apply_patch",
            params={
                "patch": "",
            },
            context_hash="c",
            planner_hash="p",
        )
        result = validate(
            p,
            basic_state,
            default_policy,
        )
        assert not result.ok

    def test_validate_safety_lockout(
        self,
        basic_state,
        default_policy,
    ):
        basic_state.safety_level = 2
        p = Proposal(
            action="repo_read_range",
            params={"path": "a.py", "line_start": 1, "line_end": 10},
            context_hash="c",
            planner_hash="p",
        )
        result = validate(
            p,
            basic_state,
            default_policy,
        )
        assert not result.ok

    def test_validate_rejects_unknown_action(
        self,
        basic_state,
        default_policy,
    ):
        p = Proposal(
            action="unknown_action",
            params={},
            context_hash="c",
            planner_hash="p",
        )
        result = validate(
            p,
            basic_state,
            default_policy,
        )
        assert not result.ok
        assert result.errors[0]["code"] == "UNKNOWN_ACTION"

    def test_validate_repo_search_long_pattern(
        self,
        basic_state,
        default_policy,
    ):
        p = Proposal(
            action="repo_search",
            params={"pattern": "x" * 600},
            context_hash="c",
            planner_hash="p",
        )
        result = validate(
            p,
            basic_state,
            default_policy,
        )
        assert not result.ok

    # ── forbid flags ──────────────────────────────

    def test_forbid_test_edits_rejects_patch(
        self,
        basic_state,
        default_policy,
    ):
        policy = {**default_policy, "forbid_test_edits": True}
        patch = (
            "diff --git a/tests/test_foo.py b/tests/test_foo.py\n"
            "--- a/tests/test_foo.py\n"
            "+++ b/tests/test_foo.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )
        p = Proposal(
            action="apply_patch",
            params={"patch": patch},
            context_hash="c",
            planner_hash="p",
        )
        result = validate(p, basic_state, policy)
        assert not result.ok
        assert any(e.get("code") == "FORBID_TEST_EDITS" for e in result.errors)

    def test_forbid_ci_edits_rejects_patch(
        self,
        basic_state,
        default_policy,
    ):
        policy = {**default_policy, "forbid_ci_edits": True}
        patch = (
            "diff --git a/.github/workflows/ci.yml"
            " b/.github/workflows/ci.yml\n"
            "--- a/.github/workflows/ci.yml\n"
            "+++ b/.github/workflows/ci.yml\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )
        p = Proposal(
            action="apply_patch",
            params={"patch": patch},
            context_hash="c",
            planner_hash="p",
        )
        result = validate(p, basic_state, policy)
        assert not result.ok
        assert any(e.get("code") == "FORBID_CI_EDITS" for e in result.errors)

    def test_forbid_dep_manifest_edits_rejects_patch(
        self,
        basic_state,
        default_policy,
    ):
        policy = {
            **default_policy,
            "forbid_dep_manifest_edits": True,
        }
        patch = (
            "diff --git a/pyproject.toml b/pyproject.toml\n"
            "--- a/pyproject.toml\n"
            "+++ b/pyproject.toml\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )
        p = Proposal(
            action="apply_patch",
            params={"patch": patch},
            context_hash="c",
            planner_hash="p",
        )
        result = validate(p, basic_state, policy)
        assert not result.ok
        assert any(e.get("code") == "FORBID_DEP_MANIFEST_EDITS" for e in result.errors)

    # ── patch budget enforcement ──────────────────

    def test_patch_too_many_files_rejected(
        self,
        basic_state,
        default_policy,
    ):
        policy = {**default_policy, "max_patch_files": 1}
        patch = (
            "diff --git a/foo.py b/foo.py\n"
            "--- a/foo.py\n+++ b/foo.py\n"
            "@@ -1 +1 @@\n-a\n+b\n"
            "diff --git a/bar.py b/bar.py\n"
            "--- a/bar.py\n+++ b/bar.py\n"
            "@@ -1 +1 @@\n-c\n+d\n"
        )
        p = Proposal(
            action="apply_patch",
            params={"patch": patch},
            context_hash="c",
            planner_hash="p",
        )
        result = validate(p, basic_state, policy)
        assert not result.ok
        assert any(e.get("code") == "PATCH_TOO_MANY_FILES" for e in result.errors)

    # ── allowed test templates ────────────────────

    def test_allowed_test_templates_rejects_unknown(
        self,
        basic_state,
        default_policy,
    ):
        policy = {
            **default_policy,
            "allowed_test_templates": [
                "pytest_targeted",
                "pytest_suite",
            ],
        }
        p = Proposal(
            action="run_tests",
            params={"template_id": "evil_template"},
            context_hash="c",
            planner_hash="p",
        )
        result = validate(p, basic_state, policy)
        assert not result.ok
        assert any(e.get("code") == "UNKNOWN_TEST_TEMPLATE" for e in result.errors)

    def test_allowed_test_templates_accepts_known(
        self,
        basic_state,
        default_policy,
    ):
        policy = {
            **default_policy,
            "allowed_test_templates": [
                "pytest_targeted",
                "pytest_suite",
            ],
        }
        p = Proposal(
            action="run_tests",
            params={"template_id": "pytest_targeted"},
            context_hash="c",
            planner_hash="p",
        )
        result = validate(p, basic_state, policy)
        assert result.ok


# ══════════════════════════════════════════════════════
# 4. SIMULATE MODULE
# ══════════════════════════════════════════════════════


class TestSimulate:
    def test_simulate_state_decay(self):
        s = SystemState()
        s.recent_failures = 6
        p = Proposal(action="run_tests", params={})
        history = OutcomeHistory(max_entries=100)
        res = simulate(p, s, history, "ctx")
        # Should be penalized heavily
        assert res.success_prob <= 0.25

    def test_simulate_with_known_traps(self):
        s = SystemState()
        p = Proposal(action="run_tests", params={})
        history = OutcomeHistory(max_entries=100)
        # Baseline
        res1 = simulate(p, s, history, "ctx")
        base_prob = res1.success_prob

        # With trap
        res2 = simulate(p, s, history, "ctx", known_traps=["action:run_tests"])
        assert res2.success_prob < base_prob
        assert res2.success_prob <= (base_prob * 0.15)  # allow some float margin

    def test_simulate_basic(
        self,
        basic_proposal,
        basic_state,
    ):
        history = OutcomeHistory(max_entries=100)
        result = simulate(
            basic_proposal,
            basic_state,
            history,
            "ctx",
        )
        assert isinstance(result, SimResult)
        assert 0.0 <= result.success_prob <= 1.0
        assert result.cost_est >= 0

    def test_simulate_with_prior_failures(
        self,
        basic_proposal,
        basic_state,
    ):
        history = OutcomeHistory(max_entries=100)
        # Record many failures.
        for _ in range(10):
            history.record(
                "repo_read_range",
                "ctx_abc",
                False,
                0.1,
            )
        result = simulate(
            basic_proposal,
            basic_state,
            history,
            "ctx_abc",
        )
        # Prob should be lower after failures.
        fresh = simulate(
            basic_proposal,
            basic_state,
            OutcomeHistory(max_entries=100),
            "ctx2",
        )
        assert result.success_prob <= fresh.success_prob

    def test_outcome_history_record_and_lookup(
        self,
    ):
        h = OutcomeHistory(max_entries=50)
        h.record("repo_read_range", "ctx1", True, 0.5)
        h.record(
            "repo_read_range",
            "ctx1",
            False,
            0.1,
        )
        stats = h.lookup("repo_read_range", "ctx1")
        assert stats is not None
        assert stats.n == 2
        assert stats.success == 1
        assert stats.fail == 1

    def test_sim_result_to_dict(
        self,
        basic_proposal,
        basic_state,
    ):
        history = OutcomeHistory(max_entries=100)
        result = simulate(
            basic_proposal,
            basic_state,
            history,
            "c",
        )
        d = result.to_dict()
        assert "success_prob" in d
        assert "failure_mode" in d
        assert "loop_risk" in d

    def test_loop_detection_rises(self):
        history = OutcomeHistory(max_entries=100)
        state = SystemState(rng_seed=1)
        # Repeat same action many times.
        for _ in range(15):
            state.record_action("repo_read_range")
            history.record(
                "repo_read_range",
                "ctx",
                False,
                0.1,
            )
        p = Proposal(
            action="repo_read_range",
            params={"path": "a.py"},
            context_hash="ctx",
            planner_hash="p",
        )
        r = simulate(p, state, history, "ctx")
        assert r.loop_risk > 0.3


# ══════════════════════════════════════════════════════
# 5. RISK MODULE
# ══════════════════════════════════════════════════════


class TestRisk:
    def test_risk_score_basic(
        self,
        basic_proposal,
        default_policy,
    ):
        sim = SimResult(
            success_prob=0.8,
            failure_mode=None,
            cost_est=0.01,
            drift_risk=0.1,
            loop_risk=0.05,
        )
        state = SystemState(rng_seed=42)
        breakdown = risk_score(
            basic_proposal,
            sim,
            state,
            default_policy,
        )
        assert isinstance(breakdown, RiskBreakdown)
        assert 0.0 <= breakdown.total_risk <= 1.0
        assert breakdown.effective_risk <= 1.0

    def test_risk_to_dict(
        self,
        basic_proposal,
        default_policy,
    ):
        sim = SimResult(
            success_prob=0.7,
            failure_mode=None,
            cost_est=0.1,
            drift_risk=0.2,
            loop_risk=0.1,
        )
        state = SystemState(rng_seed=42)
        b = risk_score(
            basic_proposal,
            sim,
            state,
            default_policy,
        )
        d = b.to_dict()
        assert "execution_risk" in d
        assert "environment_risk" in d
        assert "effective_risk" in d
        assert "ev_bonus" in d

    def test_high_failure_increases_risk(
        self,
        basic_proposal,
        default_policy,
    ):
        low_fail = SimResult(
            success_prob=0.9,
            failure_mode=None,
            cost_est=0.01,
            drift_risk=0.0,
            loop_risk=0.0,
        )
        high_fail = SimResult(
            success_prob=0.1,
            failure_mode="cluster_failure",
            cost_est=0.5,
            drift_risk=0.8,
            loop_risk=0.7,
        )
        state = SystemState(rng_seed=42)
        r_low = risk_score(
            basic_proposal,
            low_fail,
            state,
            default_policy,
        )
        r_high = risk_score(
            basic_proposal,
            high_fail,
            state,
            default_policy,
        )
        assert r_high.total_risk > r_low.total_risk


# ══════════════════════════════════════════════════════
# 6. DECIDE MODULE
# ══════════════════════════════════════════════════════


class TestDecide:
    def test_decide_approves_low_risk(
        self,
        default_policy,
    ):
        sim = SimResult(
            success_prob=0.8,
            failure_mode=None,
            cost_est=0.01,
            drift_risk=0.1,
            loop_risk=0.05,
        )
        risk = RiskBreakdown(
            execution_risk=0.1,
            environment_risk=0.1,
            uncertainty_risk=0.1,
            cost_risk=0.05,
            loop_risk=0.05,
            total_risk=0.1,
            effective_risk=0.1,
            ev_bonus=0.2,
        )
        p = Proposal(
            action="repo_read_range",
            params={"path": "a.py"},
            context_hash="c",
            planner_hash="p",
        )
        d = decide(p, sim, risk, default_policy)
        assert isinstance(d, Decision)
        assert d.approved is True

    def test_decide_rejects_high_risk(
        self,
        default_policy,
    ):
        sim = SimResult(
            success_prob=0.05,
            failure_mode="cluster_failure",
            cost_est=1.0,
            drift_risk=0.95,
            loop_risk=0.95,
        )
        risk = RiskBreakdown(
            execution_risk=0.9,
            environment_risk=0.9,
            uncertainty_risk=0.9,
            cost_risk=0.9,
            loop_risk=0.95,
            total_risk=0.92,
            effective_risk=0.9,
            ev_bonus=0.01,
        )
        p = Proposal(
            action="apply_patch",
            params={"patch": "x"},
            context_hash="c",
            planner_hash="p",
        )
        d = decide(p, sim, risk, default_policy)
        assert d.approved is False
        assert d.reason  # has a reason string

    def test_decide_rejects_loop(
        self,
        default_policy,
    ):
        sim = SimResult(
            success_prob=0.5,
            failure_mode=None,
            cost_est=0.1,
            drift_risk=0.1,
            loop_risk=0.95,
        )
        risk = RiskBreakdown(
            execution_risk=0.2,
            environment_risk=0.2,
            uncertainty_risk=0.2,
            cost_risk=0.1,
            loop_risk=0.95,
            total_risk=0.4,
            effective_risk=0.3,
            ev_bonus=0.1,
        )
        p = Proposal(
            action="repo_read_range",
            params={"path": "a.py"},
            context_hash="c",
            planner_hash="p",
        )
        d = decide(p, sim, risk, default_policy)
        assert d.approved is False
        assert "loop" in d.reason.lower()


# ══════════════════════════════════════════════════════
# 7. VERIFY MODULE
# ══════════════════════════════════════════════════════


class TestVerify:
    def test_verify_success(
        self,
        basic_proposal,
        basic_outcome,
        basic_state,
        default_policy,
    ):
        result = verify(
            basic_proposal,
            basic_outcome,
            basic_state,
            default_policy,
        )
        assert isinstance(result, VerificationResult)
        assert result.ok

    def test_verify_detects_failure_cluster(
        self,
        basic_proposal,
        failing_outcome,
        basic_state,
        default_policy,
    ):
        # Load up many recent failures.
        for _ in range(12):
            basic_state.record_failure()
        result = verify(
            basic_proposal,
            failing_outcome,
            basic_state,
            default_policy,
        )
        # Should flag the cluster.
        assert not result.ok
        assert any(e.get("code") == "FAILURE_CLUSTER" for e in result.violations)

    def test_verify_duration_exceeded(
        self,
        basic_proposal,
        basic_state,
        default_policy,
    ):
        long = Outcome(
            success=True,
            exit_code=0,
            payload="",
            logs="",
            duration_sec=99999,
        )
        result = verify(
            basic_proposal,
            long,
            basic_state,
            default_policy,
        )
        # Extremely long duration -> violation.
        assert any(e.get("code") == "DURATION_EXCEEDED" for e in result.violations)


# ══════════════════════════════════════════════════════
# 8. HARD LEDGER MODULE
# ══════════════════════════════════════════════════════


class TestHardLedger:
    def test_append_and_read(self, tmp_ledger):
        ledger = HardLedger(tmp_ledger)
        rec = LedgerRecord(
            proposal_hash="phash1",
            simulation={"prob": 0.9},
            risk={"total": 0.1},
            decision="APPROVE",
            decision_reason="low risk",
            outcome_hash="ohash1",
            state_hash="shash1",
            verification={"ok": True},
        )
        ledger.append(rec)
        all_recs = ledger.read_all()
        assert len(all_recs) == 1
        assert all_recs[0].proposal_hash == "phash1"

    def test_chain_integrity(self, tmp_ledger):
        ledger = HardLedger(tmp_ledger)
        for i in range(5):
            rec = LedgerRecord(
                proposal_hash=f"p{i}",
                simulation={"prob": 0.9},
                risk={"total": 0.1},
                decision="APPROVE",
                decision_reason="ok",
                outcome_hash=f"o{i}",
                state_hash=f"s{i}",
                verification={"ok": True},
            )
            ledger.append(rec)
        result = ledger.verify_chain()
        assert result["ok"] is True
        assert result["entries"] == 5

    def test_chain_detects_tampering(
        self,
        tmp_ledger,
    ):
        ledger = HardLedger(tmp_ledger)
        for i in range(3):
            rec = LedgerRecord(
                proposal_hash=f"p{i}",
                simulation={},
                risk={},
                decision="APPROVE",
                decision_reason="ok",
                outcome_hash=f"o{i}",
                state_hash=f"s{i}",
                verification={"ok": True},
            )
            ledger.append(rec)
        # Corrupt prev_chain_hash on the last line
        # to break the chain.
        with open(tmp_ledger, "r") as f:
            lines = f.readlines()
        if len(lines) >= 3:
            obj = json.loads(lines[2])
            obj["prev_chain_hash"] = "corrupt"
            lines[2] = json.dumps(obj) + "\n"
            with open(tmp_ledger, "w") as f:
                f.writelines(lines)
        ledger2 = HardLedger(tmp_ledger)
        result = ledger2.verify_chain()
        assert result["ok"] is False

    def test_ledger_count(self, tmp_ledger):
        ledger = HardLedger(tmp_ledger)
        assert ledger.count == 0
        for i in range(3):
            ledger.append(
                LedgerRecord(
                    proposal_hash=f"p{i}",
                    simulation={},
                    risk={},
                    decision="APPROVE",
                    decision_reason="ok",
                    outcome_hash=f"o{i}",
                    state_hash=f"s{i}",
                    verification={"ok": True},
                )
            )
        assert ledger.count == 3

    def test_read_all_filtered_by_run_id(
        self,
        tmp_ledger,
    ):
        ledger = HardLedger(tmp_ledger)
        ledger.append(
            LedgerRecord(
                proposal_hash="p-a",
                simulation={},
                risk={},
                decision="APPROVE",
                decision_reason="ok",
                outcome_hash="o-a",
                state_hash="s-a",
                verification={"ok": True},
                metadata={"run_id": "run-a"},
            )
        )
        ledger.append(
            LedgerRecord(
                proposal_hash="p-b",
                simulation={},
                risk={},
                decision="APPROVE",
                decision_reason="ok",
                outcome_hash="o-b",
                state_hash="s-b",
                verification={"ok": True},
                metadata={"run_id": "run-b"},
            )
        )

        all_recs = ledger.read_all()
        run_a = ledger.read_all(run_id="run-a")
        run_b = ledger.read_all(run_id="run-b")
        run_c = ledger.read_all(run_id="run-c")

        assert len(all_recs) == 2
        assert len(run_a) == 1
        assert run_a[0].proposal_hash == "p-a"
        assert len(run_b) == 1
        assert run_b[0].proposal_hash == "p-b"
        assert run_c == []


# ══════════════════════════════════════════════════════
# 9. KERNEL MODULE (FULL PIPELINE)
# ══════════════════════════════════════════════════════


class TestHardKernel:
    def test_kernel_step_approve_and_execute(
        self,
        tmp_ledger,
        default_policy,
    ):
        hk = HardKernel(
            tmp_ledger,
            policy=default_policy,
        )
        # Use a VALID action type.
        raw_step = {
            "type": "repo_read_range",
            "path": "src/main.py",
            "line_start": 1,
            "line_end": 50,
        }

        def exec_fn(s):
            return Outcome(
                success=True,
                exit_code=0,
                payload="ok",
                logs="read done",
                duration_sec=0.1,
            )

        kr = hk.kernel_step(
            raw_step,
            execute_fn=exec_fn,
            context="ctx1",
            intent="read source",
            bundle_id="b1",
        )
        assert isinstance(kr, KernelStepResult)
        assert kr.approved is True
        assert kr.success is True
        assert kr.phase == "COMMIT"

    def test_kernel_step_reject_unknown_action(
        self,
        tmp_ledger,
        default_policy,
    ):
        hk = HardKernel(
            tmp_ledger,
            policy=default_policy,
        )
        raw_step = {
            "type": "unknown_action",
            "path": "src/main.py",
        }

        def exec_fn(s):
            return Outcome(
                success=True,
                exit_code=0,
                payload="ok",
                logs="",
                duration_sec=0.1,
            )

        kr = hk.kernel_step(
            raw_step,
            execute_fn=exec_fn,
            context="ctx",
            intent="read",
            bundle_id="b",
        )
        assert kr.approved is False
        assert kr.phase == "VALIDATE"

    def test_kernel_get_stats(
        self,
        tmp_ledger,
        default_policy,
    ):
        hk = HardKernel(
            tmp_ledger,
            policy=default_policy,
        )
        stats = hk.get_stats()
        assert "step_count" in stats
        assert "safety_level" in stats

    def test_kernel_adaptive_tighten(
        self,
        tmp_ledger,
        default_policy,
    ):
        hk = HardKernel(
            tmp_ledger,
            policy=default_policy,
        )
        orig_risk = hk.policy.get(
            "risk_max",
            0.65,
        )
        hk._adaptive_tighten()
        assert hk.state.safety_level > 0
        assert hk.policy["risk_max"] < orig_risk

    def test_kernel_adaptive_relax(
        self,
        tmp_ledger,
        default_policy,
    ):
        hk = HardKernel(
            tmp_ledger,
            policy=default_policy,
        )
        hk._adaptive_tighten()
        sl_before = hk.state.safety_level
        hk.adaptive_relax()
        assert hk.state.safety_level <= sl_before

    def test_kernel_reset_for_iteration(
        self,
        tmp_ledger,
        default_policy,
    ):
        hk = HardKernel(
            tmp_ledger,
            policy=default_policy,
        )
        hk.state.advance_step()
        hk.reset_for_iteration()
        assert hk.state.iter_count >= 1

    def test_kernel_step_records_to_ledger(
        self,
        tmp_ledger,
        default_policy,
    ):
        hk = HardKernel(
            tmp_ledger,
            policy=default_policy,
        )
        hk.kernel_step(
            {"type": "repo_search", "pattern": "def foo"},
            execute_fn=lambda s: Outcome(
                success=True,
                exit_code=0,
                payload="found",
                logs="",
                duration_sec=0.1,
            ),
            context="ctx",
            intent="search",
            bundle_id="b",
        )
        assert hk.ledger.count >= 1


# ══════════════════════════════════════════════════════
# 10. REPLAY MODULE
# ══════════════════════════════════════════════════════


class TestReplay:
    def test_replay_empty_ledger(self, tmp_ledger):
        # Create empty file.
        Path(tmp_ledger).touch()
        runner = ReplayRunner(tmp_ledger)
        result = runner.replay_verify()
        assert result.ok

    def test_replay_with_records(
        self,
        tmp_ledger,
        default_policy,
    ):
        # Create some kernel steps first.
        hk = HardKernel(
            tmp_ledger,
            policy=default_policy,
        )
        for i in range(3):
            hk.kernel_step(
                {"type": "repo_search", "pattern": f"pattern_{i}"},
                execute_fn=lambda s: Outcome(
                    success=True,
                    exit_code=0,
                    payload="ok",
                    logs="",
                    duration_sec=0.1,
                ),
                context=f"ctx{i}",
                intent="search",
                bundle_id=f"b{i}",
            )
        runner = ReplayRunner(tmp_ledger)
        chain = runner.verify_chain()
        assert chain["ok"] is True

    def test_extract_decision_trace(
        self,
        tmp_ledger,
        default_policy,
    ):
        hk = HardKernel(
            tmp_ledger,
            policy=default_policy,
        )
        hk.kernel_step(
            {"type": "repo_search", "pattern": "def main"},
            execute_fn=lambda s: Outcome(
                success=True,
                exit_code=0,
                payload="ok",
                logs="",
                duration_sec=0.1,
            ),
            context="ctx",
            intent="search",
            bundle_id="b",
        )
        runner = ReplayRunner(tmp_ledger)
        trace = runner.extract_decision_trace()
        assert isinstance(trace, list)
        assert len(trace) >= 1

    def test_replay_verify_with_run_filter(
        self,
        tmp_ledger,
        default_policy,
    ):
        hk = HardKernel(
            tmp_ledger,
            policy=default_policy,
        )
        hk.kernel_step(
            {"type": "repo_search", "pattern": "alpha"},
            execute_fn=lambda s: Outcome(
                success=True,
                exit_code=0,
                payload="ok",
                logs="",
                duration_sec=0.1,
            ),
            context="ctx-a",
            intent="search",
            bundle_id="b-a",
            run_id="run-a",
        )
        hk.kernel_step(
            {"type": "repo_search", "pattern": "beta"},
            execute_fn=lambda s: Outcome(
                success=True,
                exit_code=0,
                payload="ok",
                logs="",
                duration_sec=0.1,
            ),
            context="ctx-b",
            intent="search",
            bundle_id="b-b",
            run_id="run-b",
        )

        runner = ReplayRunner(tmp_ledger)
        all_result = runner.replay_verify()
        a_result = runner.replay_verify(
            run_id="run-a",
        )
        b_result = runner.replay_verify(
            run_id="run-b",
        )
        c_result = runner.replay_verify(
            run_id="run-c",
        )

        assert all_result.total_steps == 2
        assert a_result.ok
        assert a_result.total_steps == 1
        assert b_result.ok
        assert b_result.total_steps == 1
        assert c_result.ok
        assert c_result.total_steps == 0

    def test_replay_verify_with_run_filter_interleaved(
        self,
        tmp_ledger,
        default_policy,
    ):
        hk = HardKernel(
            tmp_ledger,
            policy=default_policy,
        )
        # Interleave two runs so per-run replay cannot assume
        # contiguous prev pointers within filtered records.
        hk.kernel_step(
            {"type": "repo_search", "pattern": "a1"},
            execute_fn=lambda s: Outcome(
                success=True,
                exit_code=0,
                payload="ok",
                logs="",
                duration_sec=0.1,
            ),
            context="ctx-a1",
            intent="search",
            bundle_id="b-a1",
            run_id="run-a",
        )
        hk.kernel_step(
            {"type": "repo_search", "pattern": "b1"},
            execute_fn=lambda s: Outcome(
                success=True,
                exit_code=0,
                payload="ok",
                logs="",
                duration_sec=0.1,
            ),
            context="ctx-b1",
            intent="search",
            bundle_id="b-b1",
            run_id="run-b",
        )
        hk.kernel_step(
            {"type": "repo_search", "pattern": "a2"},
            execute_fn=lambda s: Outcome(
                success=True,
                exit_code=0,
                payload="ok",
                logs="",
                duration_sec=0.1,
            ),
            context="ctx-a2",
            intent="search",
            bundle_id="b-a2",
            run_id="run-a",
        )

        runner = ReplayRunner(tmp_ledger)
        all_result = runner.replay_verify()
        run_a = runner.replay_verify(run_id="run-a")
        run_b = runner.replay_verify(run_id="run-b")

        assert all_result.ok
        assert all_result.total_steps == 3
        assert run_a.ok
        assert run_a.total_steps == 2
        assert run_b.ok
        assert run_b.total_steps == 1

    def test_extract_decision_trace_with_run_filter(
        self,
        tmp_ledger,
        default_policy,
    ):
        hk = HardKernel(
            tmp_ledger,
            policy=default_policy,
        )
        hk.kernel_step(
            {"type": "repo_search", "pattern": "alpha"},
            execute_fn=lambda s: Outcome(
                success=True,
                exit_code=0,
                payload="ok",
                logs="",
                duration_sec=0.1,
            ),
            context="ctx-a",
            intent="search",
            bundle_id="b-a",
            run_id="run-a",
        )
        hk.kernel_step(
            {"type": "repo_search", "pattern": "beta"},
            execute_fn=lambda s: Outcome(
                success=True,
                exit_code=0,
                payload="ok",
                logs="",
                duration_sec=0.1,
            ),
            context="ctx-b",
            intent="search",
            bundle_id="b-b",
            run_id="run-b",
        )

        runner = ReplayRunner(tmp_ledger)
        trace_all = runner.extract_decision_trace()
        trace_a = runner.extract_decision_trace(
            run_id="run-a",
        )
        trace_b = runner.extract_decision_trace(
            run_id="run-b",
        )

        assert len(trace_all) == 2
        assert len(trace_a) == 1
        assert len(trace_b) == 1
        assert trace_a[0]["run_id"] == "run-a"
        assert trace_b[0]["run_id"] == "run-b"

    def test_snapshot_environment(self, tmp_path):
        repo = str(tmp_path / "repo")
        os.makedirs(repo, exist_ok=True)
        snap = snapshot_environment(repo, 42)
        assert "env_hash" in snap
        assert snap["seed"] == 42


# ══════════════════════════════════════════════════════
# 11. MEMORY IMMUNE SYSTEM
# ══════════════════════════════════════════════════════


class TestMemoryImmuneSystem:
    def test_admit_good_entry(self):
        mem = MemoryImmuneSystem(
            quality_min=0.1,
            risk_max=0.9,
            contradiction_max=0.9,
        )
        entry = MemoryEntry(
            content="successful file read of main.py",
            source="kernel",
            entry_type="action_outcome",
        )
        result = mem.admit(entry)
        assert result.decision == MemoryDecision.ADMIT

    def test_admit_quarantine_low_quality(self):
        mem = MemoryImmuneSystem(
            quality_min=0.99,
            risk_max=0.9,
            contradiction_max=0.9,
        )
        entry = MemoryEntry(
            content="x",
            source="unknown",
            entry_type="action_outcome",
        )
        result = mem.admit(entry)
        assert result.decision in (
            MemoryDecision.QUARANTINE,
            MemoryDecision.REJECT,
        )

    def test_protect_axiom(self):
        mem = MemoryImmuneSystem()
        axiom_entry = MemoryEntry(
            content="never delete production data",
            source="core",
            entry_type="axiom",
        )
        mem.protect_axiom(axiom_entry)
        stats = mem.get_stats()
        assert stats["core_axioms"] >= 1

    def test_decay(self):
        mem = MemoryImmuneSystem(
            quality_min=0.1,
            max_entries=50,
        )
        for i in range(10):
            mem.admit(
                MemoryEntry(
                    content=f"entry {i} with data",
                    source="kernel",
                    entry_type="action_outcome",
                )
            )
        removed = mem.decay()
        stats = mem.get_stats()
        assert stats["active"] <= 10

    def test_record_outcome(self):
        mem = MemoryImmuneSystem(
            quality_min=0.1,
        )
        entry = MemoryEntry(
            content="test action result data",
            source="kernel",
            entry_type="action_outcome",
        )
        result = mem.admit(entry)
        if result.decision == MemoryDecision.ADMIT:
            # record_outcome takes provenance_hash.
            mem.record_outcome(
                entry.provenance_hash,
                True,
            )
            assert entry.success_count >= 1

    def test_lookup(self):
        mem = MemoryImmuneSystem(
            quality_min=0.1,
        )
        for i in range(5):
            mem.admit(
                MemoryEntry(
                    content=f"outcome data {i} info",
                    source="kernel",
                    entry_type="action_outcome",
                )
            )
        results = mem.lookup("action_outcome", 3)
        assert len(results) <= 3

    def test_get_stats(self):
        mem = MemoryImmuneSystem()
        stats = mem.get_stats()
        assert "active" in stats
        assert "core_axioms" in stats
        assert "quarantined" in stats

    def test_save_and_load(self, tmp_path):
        """Test JSONL persistence: save → load into fresh instance."""
        mem = MemoryImmuneSystem(quality_min=0.1)
        for i in range(5):
            mem.admit(
                MemoryEntry(
                    content=f"action=read_file success=True case {i}",
                    source="kernel",
                    entry_type="action_outcome",
                )
            )
        path = str(tmp_path / "memory.jsonl")
        saved = mem.save(path)
        assert saved == 5

        # Load into fresh instance.
        mem2 = MemoryImmuneSystem(quality_min=0.1)
        loaded = mem2.load(path)
        assert loaded == 5
        assert mem2.active_count == 5

    def test_elo_quality_adapts(self):
        """ELO scoring should raise quality on success, lower on failure."""
        mem = MemoryImmuneSystem(quality_min=0.1)
        entry = MemoryEntry(
            content="action=run_tests result data",
            source="kernel",
            entry_type="action_outcome",
        )
        mem.admit(entry)
        q_initial = entry.quality_score

        # Record successes → quality should increase.
        for _ in range(5):
            mem.record_outcome(entry.provenance_hash, True)
        assert entry.quality_score > q_initial

        q_after_success = entry.quality_score
        # Record failures → quality should decrease.
        for _ in range(10):
            mem.record_outcome(entry.provenance_hash, False)
        assert entry.quality_score < q_after_success

    def test_retrieve_relevant(self):
        """retrieve_relevant should rank by keyword overlap + quality."""
        mem = MemoryImmuneSystem(quality_min=0.1)
        mem.admit(
            MemoryEntry(
                content="action=apply_patch success=True django models",
                source="kernel",
                entry_type="action_outcome",
            )
        )
        mem.admit(
            MemoryEntry(
                content="action=run_tests success=False flask routes",
                source="kernel",
                entry_type="action_outcome",
            )
        )

        # Query about django should rank django entry first.
        results = mem.retrieve_relevant("django models patch")
        assert len(results) >= 1
        assert "django" in results[0].content


# ══════════════════════════════════════════════════════
# 12. HIERARCHICAL PLANNER
# ══════════════════════════════════════════════════════


class TestHierarchicalPlanner:
    def test_set_goal_creates_subgoals(self):
        p = HierarchicalPlanner()
        subgoals = p.set_goal(
            "Fix the failing test",
            "fix_test",
        )
        assert isinstance(subgoals, list)
        assert len(subgoals) >= 1
        assert all(isinstance(s, Subgoal) for s in subgoals)

    def test_current_subgoal(self):
        p = HierarchicalPlanner()
        p.set_goal("Fix import error", "fix_import")
        sg = p.current_subgoal()
        assert sg is not None
        assert isinstance(sg, Subgoal)

    def test_advance_subgoal(self):
        p = HierarchicalPlanner()
        p.set_goal("Fix syntax error", "fix_syntax")
        first = p.current_subgoal()
        advanced = p.advance_subgoal()
        if advanced:
            second = p.current_subgoal()
            assert second != first

    def test_record_no_progress(self):
        p = HierarchicalPlanner(max_stagnation=3)
        p.set_goal("Fix test", "fix_test")
        result = False
        for _ in range(3):
            result = p.record_no_progress()
        # After max_stagnation should be stagnant.
        assert result is True

    def test_escalate(self):
        p = HierarchicalPlanner(
            max_stagnation=2,
            max_escalations=3,
        )
        p.set_goal("Fix test", "fix_test")
        p.escalate()
        assert p.state.escalation_count == 1

    def test_tactical_plan(self):
        p = HierarchicalPlanner()
        p.set_goal("Fix the bug", "fix_generic")
        sg = p.current_subgoal()
        plan = p.tactical_plan(
            sg,
            ["repo_read_range", "apply_patch", "run_tests"],
        )
        assert isinstance(plan, TacticalPlan)
        assert len(plan.actions) >= 1

    def test_get_execution_context(self):
        p = HierarchicalPlanner()
        p.set_goal("Fix test", "fix_test")
        ctx = p.get_execution_context()
        assert "goal" in ctx
        assert "progress" in ctx

    def test_get_planner_guidance(self):
        p = HierarchicalPlanner()
        p.set_goal("Fix test", "fix_test")
        guidance = p.get_planner_guidance()
        assert isinstance(guidance, str)
        assert len(guidance) > 0

    def test_classify_task(self):
        p = HierarchicalPlanner()
        tt = p.classify_task(
            "test_foo is failing",
            "test_failure",
        )
        assert tt in (
            "fix_test",
            "fix_import",
            "fix_syntax",
            "fix_generic",
        )

    def test_get_stats(self):
        p = HierarchicalPlanner()
        p.set_goal("Fix test", "fix_test")
        stats = p.get_stats()
        assert "goal" in stats
        assert "progress" in stats

    def test_no_goal_returns_empty_guidance(self):
        p = HierarchicalPlanner()
        assert "All subgoals completed" in p.get_planner_guidance()

    def test_planner_guidance_with_error(self):
        p = HierarchicalPlanner()
        p.set_goal("Test task")
        guidance = p.get_planner_guidance(last_error="SyntaxError: invalid syntax")
        assert "## Self-Critique" in guidance
        assert "SyntaxError: invalid syntax" in guidance
        assert "Analysis: The previous action failed" in guidance


# ══════════════════════════════════════════════════════
# 13. INTEGRATION: FULL PIPELINE
# ══════════════════════════════════════════════════════


class TestIntegration:
    """End-to-end pipeline test: planner → kernel
    → memory → replay."""

    def test_full_pipeline(self, tmp_path):
        ledger_path = str(
            tmp_path / "int_ledger.jsonl",
        )
        policy = {
            "risk_max": 0.65,
            "success_min": 0.15,
            "loop_max": 0.8,
            "drift_max": 0.85,
            "risk_lambda": 0.7,
            "max_total_steps": 200,
            "history_max": 500,
            "fail_cluster_threshold": 8,
            "rng_seed": 42,
            "policy_hash": "test",
        }

        # 1. Planner decomposes task.
        planner = HierarchicalPlanner()
        subgoals = planner.set_goal(
            "Fix the failing test_foo",
            "fix_test",
        )
        assert len(subgoals) >= 1

        # 2. Kernel executes steps.
        kernel = HardKernel(
            ledger_path,
            policy=policy,
        )

        def exec_ok(s):
            return Outcome(
                success=True,
                exit_code=0,
                payload="done",
                logs="ok",
                duration_sec=0.2,
            )

        kr = kernel.kernel_step(
            {"type": "repo_search", "pattern": "test_foo"},
            execute_fn=exec_ok,
            context="test_ctx",
            intent="search for failing test",
            bundle_id="b1",
        )
        assert kr.approved
        assert kr.success

        # 3. Memory records outcome.
        memory = MemoryImmuneSystem(
            quality_min=0.1,
        )
        memory.admit(
            MemoryEntry(
                content=(f"search test_foo success" f" risk={kr.risk.total_risk:.2f}"),
                source="kernel",
                entry_type="action_outcome",
            )
        )
        assert memory.get_stats()["active"] >= 1

        # 4. Planner advances.
        planner.advance_subgoal()

        # 5. Replay verifies chain.
        runner = ReplayRunner(ledger_path)
        chain = runner.verify_chain()
        assert chain["ok"] is True

        trace = runner.extract_decision_trace()
        assert len(trace) >= 1

    def test_multi_step_pipeline(self, tmp_path):
        """Run multiple steps through the full
        pipeline and verify chain integrity."""
        ledger_path = str(
            tmp_path / "multi_ledger.jsonl",
        )
        policy = {
            "risk_max": 0.65,
            "success_min": 0.15,
            "loop_max": 0.8,
            "drift_max": 0.85,
            "risk_lambda": 0.7,
            "max_total_steps": 200,
            "history_max": 500,
            "fail_cluster_threshold": 8,
            "rng_seed": 42,
            "policy_hash": "test",
        }

        kernel = HardKernel(
            ledger_path,
            policy=policy,
        )
        memory = MemoryImmuneSystem(
            quality_min=0.1,
        )

        steps = [
            {"type": "repo_search", "pattern": "def main"},
            {
                "type": "repo_read_range",
                "path": "b.py",
                "line_start": 1,
                "line_end": 50,
            },
            {
                "type": "apply_patch",
                "patch": "--- a/a.py\n+++ b/a.py\n"
                "@@ -10,1 +10,1 @@\n"
                "-old line\n+new line\n",
            },
            {
                "type": "run_tests",
                "template_id": "pytest_targeted",
                "template_params": {"target": "test_foo"},
            },
        ]

        results = []
        for i, step in enumerate(steps):
            kr = kernel.kernel_step(
                step,
                execute_fn=lambda s: Outcome(
                    success=True,
                    exit_code=0,
                    payload="ok",
                    logs="",
                    duration_sec=0.1,
                ),
                context=f"ctx{i}",
                intent=f"step {i}",
                bundle_id=f"b{i}",
            )
            results.append(kr)
            if kr.approved and kr.success:
                memory.admit(
                    MemoryEntry(
                        content=f"step {i} ok data",
                        source="kernel",
                        entry_type="action_outcome",
                    )
                )

        # Verify chain integrity.
        runner = ReplayRunner(ledger_path)
        chain = runner.verify_chain()
        assert chain["ok"] is True

        # Count approved steps.
        approved = sum(1 for r in results if r.approved)
        assert approved >= 1

        stats = kernel.get_stats()
        assert stats["step_count"] >= 1
