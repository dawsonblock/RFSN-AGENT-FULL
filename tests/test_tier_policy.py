import textwrap

import yaml

from rfsn_kernel.kernel import HardKernel
from rfsn_kernel.replay import ReplayRunner
from rfsn_kernel.state import Outcome
from rfsn_kernel.tier_policy import (
    pick_next_tier,
    step_touches,
    tier_allows_step,
)


def _sample_policy():
    return {
        "tiers": {
            0: {
                "name": "code-only",
                "allow": {
                    "edit_tests": False,
                    "edit_deps": False,
                    "edit_ci": False,
                },
            },
            1: {
                "name": "tests-allowed",
                "allow": {
                    "edit_tests": True,
                    "edit_deps": False,
                    "edit_ci": False,
                },
            },
            2: {
                "name": "deps-allowed",
                "allow": {
                    "edit_tests": True,
                    "edit_deps": True,
                    "edit_ci": False,
                },
            },
            3: {
                "name": "ci-allowed",
                "allow": {
                    "edit_tests": True,
                    "edit_deps": True,
                    "edit_ci": True,
                },
            },
        },
        "escalation_rules": {
            "to_tier_1": {
                "requires_any": [
                    {"failure_kind": "tests_failed"},
                ],
            },
            "to_tier_2": {
                "requires_any": [
                    {"failure_kind": "deps_install_failed"},
                ],
            },
            "to_tier_3": {
                "requires_any": [
                    {"failure_kind": "ci_failed"},
                ],
            },
        },
        "classifiers": {
            "tests_globs": ["**/tests/**", "**/test_*.py"],
            "deps_globs": ["**/requirements.txt"],
            "ci_globs": ["**/.github/workflows/**"],
        },
    }


def _patch_for(path: str) -> str:
    return textwrap.dedent(
        f"""\
        diff --git a/{path} b/{path}
        --- a/{path}
        +++ b/{path}
        @@ -1 +1 @@
        -old
        +new
        """
    )


def test_step_touches_extracts_paths_from_patch():
    step = {
        "type": "apply_patch",
        "patch": _patch_for("tests/test_demo.py"),
    }
    touched = step_touches(step)
    assert "tests/test_demo.py" in touched


def test_tier_allows_step_uses_file_classifiers():
    policy = _sample_policy()
    tier0 = policy["tiers"][0]
    tier1 = policy["tiers"][1]
    classifiers = policy["classifiers"]
    test_step = {
        "type": "apply_patch",
        "patch": _patch_for("tests/test_demo.py"),
    }
    dep_step = {
        "type": "apply_patch",
        "patch": _patch_for("requirements.txt"),
    }

    ok0, reason0 = tier_allows_step(
        test_step, tier0, classifiers,
    )
    ok1, reason1 = tier_allows_step(
        test_step, tier1, classifiers,
    )
    ok_dep, reason_dep = tier_allows_step(
        dep_step, tier1, classifiers,
    )
    assert not ok0
    assert "tests edits" in (reason0 or "")
    assert ok1
    assert reason1 is None
    assert not ok_dep
    assert "deps edits" in (reason_dep or "")


def test_pick_next_tier_is_deterministic():
    policy = _sample_policy()
    assert pick_next_tier(
        0, ["tests_failed"], policy,
    ).tier == 1
    assert pick_next_tier(
        1, ["deps_install_failed"], policy,
    ).tier == 2
    assert pick_next_tier(
        2, ["ci_failed"], policy,
    ).tier == 3
    assert pick_next_tier(
        3, ["tests_failed"], policy,
    ).tier == 3


def test_hard_kernel_tier_state_is_per_run(
    tmp_path, monkeypatch,
):
    tier_path = tmp_path / "tiers.yaml"
    tier_path.write_text(
        yaml.safe_dump(_sample_policy()),
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "RFSN_TIER_POLICY_PATH", str(tier_path),
    )

    ledger_path = str(tmp_path / "kernel_ledger.jsonl")
    hk = HardKernel(
        ledger_path=ledger_path,
        policy={
            "risk_max": 1.0,
            "success_min": 0.0,
            "loop_max": 1.0,
            "drift_max": 1.0,
            "rng_seed": 1,
        },
    )

    def fail_exec(_step):
        return Outcome(
            success=False,
            exit_code=1,
            logs="pytest failed with AssertionError",
        )

    def ok_exec(_step):
        return Outcome(
            success=True,
            exit_code=0,
            logs="ok",
        )

    # Escalate run-a from tier 0 -> 1
    hk.reset_for_run(run_id="run-a")
    kr_a1 = hk.kernel_step(
        {
            "id": "s1",
            "type": "repo_search",
            "pattern": "foo",
        },
        execute_fn=fail_exec,
        run_id="run-a",
    )
    assert kr_a1.approved
    assert hk.run_state.get("run-a").tier == 1

    test_patch = {
        "id": "s2",
        "type": "apply_patch",
        "patch": _patch_for("tests/test_demo.py"),
    }

    # run-a tier 1 allows tests edit
    kr_a2 = hk.kernel_step(
        test_patch,
        execute_fn=ok_exec,
        run_id="run-a",
    )
    assert kr_a2.approved

    # run-b still tier 0, should reject same test edit
    hk.reset_for_run(run_id="run-b")
    kr_b1 = hk.kernel_step(
        test_patch,
        execute_fn=ok_exec,
        run_id="run-b",
    )
    assert not kr_b1.approved
    assert kr_b1.decision is not None
    assert "tier forbids tests edits" in kr_b1.decision.reason

    # Replay verification ignores kernel event records.
    runner = ReplayRunner(ledger_path)
    replay_a = runner.replay_verify(run_id="run-a")
    assert replay_a.ok
    assert replay_a.total_steps == 2


def test_kernel_uses_learner_success_prior_to_gate(tmp_path):
    hk = HardKernel(
        ledger_path=str(
            tmp_path / "test_kernel_learner_prior.jsonl"
        ),
        policy={
            "risk_max": 1.0,
            "success_min": 0.7,
            "loop_max": 1.0,
            "drift_max": 1.0,
            "rng_seed": 1,
        },
    )
    hk.reset_for_run(run_id="run-prior")

    step = {
        "id": "s1",
        "type": "apply_patch",
        "patch": _patch_for("src/main.py"),
    }

    def ok_exec(_step):
        return Outcome(
            success=True,
            exit_code=0,
            logs="ok",
        )

    no_prior = hk.kernel_step(
        step,
        execute_fn=ok_exec,
        run_id="run-prior",
    )
    assert not no_prior.approved

    with_prior = hk.kernel_step(
        step,
        execute_fn=ok_exec,
        run_id="run-prior",
        learner_evidence={
            "prior_success_prob": 0.95,
            "prior_trials": 50,
        },
    )
    assert with_prior.approved


def test_kernel_uses_learner_failure_clusters_for_loop_risk(tmp_path):
    hk = HardKernel(
        ledger_path=str(
            tmp_path / "test_kernel_learner_loop.jsonl"
        ),
        policy={
            "risk_max": 1.0,
            "success_min": 0.1,
            "loop_max": 0.6,
            "drift_max": 1.0,
            "rng_seed": 1,
        },
    )
    hk.reset_for_run(run_id="run-loop")

    step = {
        "id": "s1",
        "type": "repo_search",
        "pattern": "foo",
    }

    def ok_exec(_step):
        return Outcome(
            success=True,
            exit_code=0,
            logs="ok",
        )

    kr = hk.kernel_step(
        step,
        execute_fn=ok_exec,
        run_id="run-loop",
        learner_evidence={
            "prior_success_prob": 0.7,
            "prior_trials": 20,
            "failure_occurrence": 5,
            "failure_best_win_rate": 0.1,
        },
    )
    assert not kr.approved
    assert kr.decision is not None
    assert "loop_risk" in kr.decision.reason
