"""Core bench-run loop: propose → apply → gate → test → iterate.

The *proposer* callback is the only integration point — it receives a
``BenchTask`` and the replay directory path and must return a unified-diff
string.  Two concrete proposers ship in ``cli.py``:

* **orchestrator_proposer** — calls the RFSN Orchestrator ``/run`` endpoint
* **direct_proposer** — calls DeepSeek (or compatible) API directly
* **placeholder** — raises immediately (forces the user to wire a real one)

When ``executor_url`` is provided, test execution is delegated to the RFSN
Executor service (Docker-sandboxed, venv-managed, network-disabled) instead
of running locally via subprocess.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Callable, Dict, Optional

import requests  # type: ignore[import-untyped]

from .contracts import BenchResult, BenchTask, TestRun
from .gate import patch_risk_gate
from .patcher import apply_unified_diff
from .repo import git_clone, git_checkout, git_diff_unified, git_reset_hard
from .replay import init_replay_dir, log_event, save_blob
from .testsel import choose_quick_tests
from .util import run_cmd

# Type alias for the patch-proposer callback.
Proposer = Callable[[BenchTask, str], str]


def _extract_diff_paths(unified_diff: str) -> list[str]:
    """Extract file paths touched by a unified diff (b/ side)."""
    paths = []
    for line in unified_diff.splitlines():
        if line.startswith("+++ b/"):
            paths.append(line[6:])
    return paths


def _tail(s: str, n: int = 4000) -> str:
    if len(s) <= n:
        return s
    return s[-n:]


def run_tests(cmd: str, workdir: str, timeout: int) -> TestRun:
    """Run tests locally via subprocess."""
    code, out, err, dt = run_cmd(cmd, cwd=workdir, timeout_sec=timeout)
    return TestRun(
        exit_code=code,
        stdout_tail=_tail(out),
        stderr_tail=_tail(err),
        duration_sec=round(dt, 3),
    )


def run_tests_via_executor(
    executor_url: str,
    repo_id: str,
    iteration: int,
    template_id: str,
    target: str,
    timeout: int,
    *,
    gateway_url: Optional[str] = None,
) -> TestRun:
    """Delegate test execution to the RFSN stack.

    Routing priority:
    1. If *gateway_url* is provided, POST to tool_gateway ``/run_step``
       (gets policy enforcement, budget tracking, diff-guard checks).
    2. Otherwise fall back to executor ``/run`` directly.

    The executor runs tests inside a Docker container with network disabled,
    using the venv prepared by ``ensure_deps``.  This gives reproducible,
    sandboxed test execution identical to the microservices stack.
    """
    t0 = time.time()
    step_payload = {
        "id": f"bench_test_{iteration}",
        "type": "run_tests",
        "template_id": template_id,
        "template_params": {"target": target},
        "timeout_s": timeout,
    }
    body = {
        "repo_id": repo_id,
        "iter": iteration,
        "step": step_payload,
    }

    # Choose endpoint: prefer tool_gateway for policy enforcement
    url = (
        f"{gateway_url.rstrip('/')}/run_step"
        if gateway_url
        else f"{executor_url.rstrip('/')}/run"
    )

    try:
        r = requests.post(
            url,
            json=body,
            timeout=timeout + 30,  # HTTP timeout > step timeout
        )
        dt = time.time() - t0
        if r.status_code != 200:
            return TestRun(
                exit_code=1,
                stdout_tail="",
                stderr_tail=_tail(f"executor HTTP {r.status_code}: {r.text}"),
                duration_sec=round(dt, 3),
            )
        data = r.json()
        return TestRun(
            exit_code=int(data.get("status", 1)),
            stdout_tail=_tail(data.get("logs", "")),
            stderr_tail="",
            duration_sec=round(data.get("seconds", dt), 3),
        )
    except Exception as exc:
        dt = time.time() - t0
        return TestRun(
            exit_code=1,
            stdout_tail="",
            stderr_tail=_tail(f"executor error: {exc}"),
            duration_sec=round(dt, 3),
        )


def _parse_test_cmd(cmd: str) -> tuple[str, str]:
    """Extract (template_id, target) from a test command for executor routing.

    Heuristic: if the command looks like ``pytest -q tests/foo.py::bar``,
    use template ``pytest_targeted`` with target = the node IDs.
    Falls back to ``pytest_suite`` with empty target.
    """
    cmd_stripped = cmd.strip()
    # Detect targeted pytest invocations
    if "pytest" in cmd_stripped:
        # Split off everything after the pytest flags
        parts = cmd_stripped.split()
        targets = [
            p for p in parts if "::" in p or (p.endswith(".py") and "test" in p.lower())
        ]
        if targets:
            return "pytest_targeted", " ".join(targets)
        return "pytest_suite", ""
    return "pytest_suite", ""


def bench_run(
    task: BenchTask,
    proposer: Proposer,
    *,
    replay_base: Optional[str] = None,
    ledger_path: Optional[str] = None,
    executor_url: Optional[str] = None,
    gateway_url: Optional[str] = None,
) -> BenchResult:
    """Execute the full propose→test loop for a single SWE-bench task."""
    t0 = time.time()

    # Clone + checkout FIRST — git_clone may wipe the workdir, so we must
    # not create the replay dir (which lives inside workdir) until after.
    git_clone(task.repo_url, task.workdir)
    git_checkout(task.workdir, task.repo_ref)
    # Ensure clean state (previous run may have left test_patch applied)
    git_reset_hard(task.workdir)

    # Apply test_patch (SWE-bench provides test code that must be present
    # before running — the FAIL_TO_PASS tests don't exist in the base commit)
    _test_patch_paths: list[str] = []
    if task.hints.test_patch:
        apply_unified_diff(task.hints.test_patch, task.workdir, strict=True)
        # Track which paths the test_patch touches so we can exclude them
        # from the agent's final diff.
        _test_patch_paths = _extract_diff_paths(task.hints.test_patch)

    replay_dir = init_replay_dir(
        replay_base or task.workdir,
        task.task_id,
        ledger_path=ledger_path,
    )

    log_event(
        replay_dir,
        {
            "type": "task_start",
            "task_id": task.task_id,
            "repo_url": task.repo_url,
        },
    )

    # Setup commands (install deps, etc.)
    for scmd in task.commands.setup:
        log_event(replay_dir, {"type": "setup_cmd", "cmd": scmd})
        timeout = min(600, task.limits.max_runtime_sec)
        tr = run_tests(scmd, task.workdir, timeout=timeout)
        log_event(
            replay_dir,
            {
                "type": "setup_result",
                "exit": tr.exit_code,
                "dt": tr.duration_sec,
            },
        )
        if tr.exit_code != 0:
            return BenchResult(
                task_id=task.task_id,
                status="ABORT",
                iters=0,
                final_patch_unified_diff="",
                tests={"setup": tr},
                risk=patch_risk_gate(
                    "",
                    task.limits.max_patch_bytes,
                    task.limits.max_files_touched,
                    task.limits.max_new_files,
                ),
                replay_dir=replay_dir,
            )

    quick_cmd = choose_quick_tests(task.hints, task.commands.test_quick)

    # Derive a repo_id for executor routing (sanitise task_id)
    repo_id = re.sub(r"[^A-Za-z0-9_.-]", "_", task.task_id) if executor_url else ""

    # ── Pre-diagnosis: run failing tests to capture stack traces ──
    # This gives the proposer real file:line pointers for localization,
    # dramatically improving fault localization accuracy.
    pre_diagnosis_output = ""
    if task.hints.failing_tests:
        log_event(replay_dir, {"type": "pre_diagnose_start"})
        diag_tr = run_tests(
            quick_cmd,
            task.workdir,
            timeout=min(120, task.limits.max_runtime_sec),
        )
        if diag_tr.exit_code != 0 and diag_tr.stdout_tail.strip():
            pre_diagnosis_output = diag_tr.stdout_tail
            # Save to replay dir so proposer can read it
            diag_path = os.path.join(replay_dir, "pre_diagnosis.txt")
            with open(diag_path, "w", encoding="utf-8") as f:
                f.write(pre_diagnosis_output)
        log_event(
            replay_dir,
            {
                "type": "pre_diagnose_result",
                "exit_code": diag_tr.exit_code,
                "has_traceback": bool(pre_diagnosis_output),
                "output_lines": len(pre_diagnosis_output.splitlines()),
            },
        )

    # Helper: persist iteration feedback for the proposer
    def _write_feedback(replay_dir: str, feedback: list) -> None:
        path = os.path.join(replay_dir, "feedback.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(feedback, f, ensure_ascii=False, indent=2)

    # Helper: run tests locally or via executor
    def _run(cmd: str, timeout: int, iteration: int) -> TestRun:
        if executor_url:
            template_id, target = _parse_test_cmd(cmd)
            return run_tests_via_executor(
                executor_url,
                repo_id,
                iteration,
                template_id,
                target,
                timeout,
                gateway_url=gateway_url,
            )
        return run_tests(cmd, task.workdir, timeout)

    final_patch = ""
    last_gate_rejected = False
    iter_feedback: list[dict] = []  # Accumulate ALL feedback for learning
    last_quick: Optional[TestRun] = None
    last_full: Optional[TestRun] = None

    for i in range(1, task.limits.max_iters + 1):
        elapsed = time.time() - t0
        if elapsed > task.limits.max_runtime_sec:
            log_event(replay_dir, {"type": "abort", "reason": "runtime_limit"})
            tests: Dict[str, TestRun] = {}
            if last_quick is not None:
                tests["quick"] = last_quick
            if last_full is not None:
                tests["full"] = last_full
            return BenchResult(
                task_id=task.task_id,
                status="ABORT",
                iters=i - 1,
                final_patch_unified_diff=final_patch,
                tests=tests,
                risk=patch_risk_gate(
                    final_patch,
                    task.limits.max_patch_bytes,
                    task.limits.max_files_touched,
                    task.limits.max_new_files,
                ),
                replay_dir=replay_dir,
            )

        # Clean workspace each iteration (SWE-bench-style reproducibility)
        git_reset_hard(task.workdir)
        # Re-apply test_patch after reset
        if task.hints.test_patch:
            apply_unified_diff(
                task.hints.test_patch,
                task.workdir,
                strict=True,
            )

        # ---- Propose ----
        log_event(replay_dir, {"type": "iter_start", "iter": i})
        try:
            patch = proposer(task, replay_dir)
        except Exception as exc:
            log_event(
                replay_dir,
                {"type": "propose_error", "iter": i, "error": str(exc)},
            )
            iter_feedback.append(
                {
                    "iter": i,
                    "type": "propose_error",
                    "error": str(exc)[:500],
                }
            )
            _write_feedback(replay_dir, iter_feedback)
            continue

        if not isinstance(patch, str):
            patch = str(patch)
        save_blob(
            replay_dir,
            f"proposal_iter{i}",
            patch.encode("utf-8", errors="replace"),
        )

        # ---- Apply ----
        try:
            apply_unified_diff(patch, task.workdir)
        except Exception as exc:
            log_event(
                replay_dir,
                {"type": "apply_fail", "iter": i, "error": str(exc)},
            )
            iter_feedback.append(
                {
                    "iter": i,
                    "type": "apply_fail",
                    "error": str(exc)[:500],
                }
            )
            _write_feedback(replay_dir, iter_feedback)
            continue

        # Capture the ground-truth diff actually applied, excluding
        # test_patch files (those came from SWE-bench, not the agent).
        applied = git_diff_unified(
            task.workdir,
            exclude_paths=_test_patch_paths,
        )
        final_patch = applied
        save_blob(
            replay_dir,
            f"applied_iter{i}",
            applied.encode("utf-8", errors="replace"),
        )

        # ---- Gate ----
        risk = patch_risk_gate(
            applied,
            task.limits.max_patch_bytes,
            task.limits.max_files_touched,
            task.limits.max_new_files,
        )
        log_event(
            replay_dir,
            {
                "type": "risk",
                "iter": i,
                "decision": risk.decision,
                "reasons": risk.reasons,
            },
        )
        if risk.decision == "REJECT":
            final_patch = ""  # Gate is final authority — clear rejected patch
            last_gate_rejected = True
            iter_feedback.append(
                {
                    "iter": i,
                    "type": "gate_reject",
                    "reasons": risk.reasons,
                }
            )
            _write_feedback(replay_dir, iter_feedback)
            continue
        last_gate_rejected = False

        # ---- Quick tests ----
        last_quick = _run(
            quick_cmd,
            timeout=min(600, task.limits.max_runtime_sec),
            iteration=i,
        )
        log_event(
            replay_dir,
            {
                "type": "quick_result",
                "iter": i,
                "exit": last_quick.exit_code,
                "dt": last_quick.duration_sec,
            },
        )
        save_blob(
            replay_dir,
            f"quick_stdout_iter{i}",
            last_quick.stdout_tail.encode("utf-8", errors="replace"),
        )
        save_blob(
            replay_dir,
            f"quick_stderr_iter{i}",
            last_quick.stderr_tail.encode("utf-8", errors="replace"),
        )

        if last_quick.exit_code != 0:
            # Quick tests failed — record feedback and try next iteration
            iter_feedback.append(
                {
                    "iter": i,
                    "type": "test_fail",
                    "stdout_tail": _tail(last_quick.stdout_tail, 2000),
                    "stderr_tail": _tail(last_quick.stderr_tail, 1000),
                }
            )
            _write_feedback(replay_dir, iter_feedback)
            continue

        # ---- Full tests ----
        last_full = _run(
            task.commands.test_full,
            timeout=min(1400, task.limits.max_runtime_sec),
            iteration=i,
        )
        log_event(
            replay_dir,
            {
                "type": "full_result",
                "iter": i,
                "exit": last_full.exit_code,
                "dt": last_full.duration_sec,
            },
        )
        save_blob(
            replay_dir,
            f"full_stdout_iter{i}",
            last_full.stdout_tail.encode("utf-8", errors="replace"),
        )
        save_blob(
            replay_dir,
            f"full_stderr_iter{i}",
            last_full.stderr_tail.encode("utf-8", errors="replace"),
        )

        if last_full.exit_code == 0:
            # Re-gate the final patch — gate is FINAL authority
            final_risk = patch_risk_gate(
                final_patch,
                task.limits.max_patch_bytes,
                task.limits.max_files_touched,
                task.limits.max_new_files,
            )
            if final_risk.decision == "REJECT":
                log_event(
                    replay_dir,
                    {
                        "type": "final_gate_reject",
                        "iter": i,
                        "reasons": final_risk.reasons,
                    },
                )
                final_patch = ""
                last_gate_rejected = True
                continue

            return BenchResult(
                task_id=task.task_id,
                status="PASS",
                iters=i,
                final_patch_unified_diff=final_patch,
                tests={"quick": last_quick, "full": last_full},
                risk=final_risk,
                replay_dir=replay_dir,
            )

    # Max iterations exhausted
    final_tests: Dict[str, TestRun] = {}
    if last_quick is not None:
        final_tests["quick"] = last_quick
    if last_full is not None:
        final_tests["full"] = last_full

    # Determine terminal status
    terminal_status: str = "GATE_REJECT" if last_gate_rejected else "FAIL"

    return BenchResult(
        task_id=task.task_id,
        status=terminal_status,  # type: ignore[arg-type]
        iters=task.limits.max_iters,
        final_patch_unified_diff=final_patch,
        tests=final_tests,
        risk=patch_risk_gate(
            final_patch,
            task.limits.max_patch_bytes,
            task.limits.max_files_touched,
            task.limits.max_new_files,
        ),
        replay_dir=replay_dir,
    )
