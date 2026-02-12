from __future__ import annotations

import json

from rfsn_kernel.command_infer import infer_commands
from rfsn_kernel.patch_minimize import minimize_unified_diff
from rfsn_kernel.repair_loop import next_phase, should_retry, update_state
from rfsn_kernel.scheduler import Scheduler
from rfsn_kernel.sim_cache import SimCache
from rfsn_kernel.kernel import HardKernel
from rfsn_kernel.state import Outcome, SystemState
from rfsn_kernel.normalize import normalize
from rfsn_kernel.validate import validate
from services.tool_gateway.workdir_store import WorkdirStore


def test_command_infer_prefers_root_workdir():
    profile = {
        "has_python": True,
        "has_node": False,
        "has_go": False,
        "has_rust": False,
        "has_make": True,
    }
    workdirs = [
        {"id": "workdir_0", "rel": ".", "markers": ["pyproject.toml"]},
        {"id": "workdir_1", "rel": "pkg", "markers": ["package.json"]},
    ]
    plan = infer_commands(profile, workdirs)
    assert plan["workdir_id"] == "workdir_0"
    assert "python:pytest" in plan["test_templates"]
    assert "make:test" in plan["test_templates"]


def test_patch_minimize_keeps_changed_hunks_only():
    diff = (
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -1 +1 @@\n"
        "-a\n"
        "+b\n"
    )
    out = minimize_unified_diff(diff)
    assert "+b" in out
    assert "-a" in out


def test_sim_cache_round_trip():
    cache = SimCache()
    step = {"type": "run_cmd_template", "template": "python:pytest"}
    key = cache.key(step, "workdir_0")
    cache.put(key, {"status": 0, "logs": "ok"})
    got = cache.get(key)
    assert got == {"status": 0, "logs": "ok"}


def test_scheduler_concurrency_and_budget():
    sched = Scheduler(max_concurrent=1)
    assert sched.start_run("r1", max_seconds=1)
    assert not sched.start_run("r2", max_seconds=1)
    assert sched.budget_ok("r1")
    sched.end_run("r1")
    assert sched.start_run("r2", max_seconds=1)


def test_repair_loop_state_machine():
    st = {"phase": "SEARCH", "attempt": 0, "max_attempts": 2, "last_status": 1}
    st = update_state(st, "PATCH", 1)
    assert st["attempt"] == 1
    assert should_retry(st)
    assert next_phase(st) == "VERIFY"


def test_workdir_store_round_trip():
    ws = WorkdirStore()
    ws.set_run_workdirs("run", {"workdir_0": "."})
    assert ws.get_rel("run", "workdir_0") == "."
    ws.clear("run")
    assert ws.get_rel("run", "workdir_0") is None


def test_validate_run_cmd_template_allowed_template():
    proposal = normalize({
        "type": "run_cmd_template",
        "template": "python:pytest",
        "workdir_id": "workdir_1",
    })
    result = validate(
        proposal,
        SystemState(),
        {"allowed_command_templates": ["python:pytest"]},
    )
    assert result.ok


def test_validate_format_fix_requires_fix_template():
    proposal = normalize({
        "type": "format_fix",
        "template": "python:ruff",
        "workdir_id": "workdir_1",
    })
    result = validate(
        proposal,
        SystemState(),
        {"allowed_command_templates": ["python:ruff"]},
    )
    assert not result.ok


def test_kernel_prefers_structured_failure_kind(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    hk = HardKernel(
        ledger_path=str(ledger),
        policy={
            "risk_max": 1.0,
            "success_min": 0.0,
            "allowed_command_templates": ["python:pytest"],
        },
    )

    def _exec(_step):
        payload = json.dumps({
            "status": 1,
            "failure_kind": "deps_install_failed",
        })
        return Outcome(
            success=False,
            exit_code=1,
            payload=payload,
            logs="generic failure",
            duration_sec=0.1,
        )

    res = hk.kernel_step(
        {
            "type": "run_cmd_template",
            "template": "python:pytest",
            "workdir_id": "workdir_0",
        },
        execute_fn=_exec,
        run_id="run-1",
    )
    assert res.approved
    rs = hk.run_state.get("run-1")
    assert "deps_install_failed" in rs.failure_kinds
    assert rs.tier >= 2
