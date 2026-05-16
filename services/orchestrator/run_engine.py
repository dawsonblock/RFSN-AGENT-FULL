"""Core Run Engine for RFSN Agent.

Status: Prototype / repair-stage
---------------------------------
This engine runs a minimal bounded repair loop.  It does **not** contain a
real LLM planner.  Two operating modes are supported:

``dry_run`` (default when no planner is configured)
    Returns immediately with status ``dry_run`` and a clear message.
    Does not claim success or execute any steps.

``manual_plan``
    Accepts a pre-defined JSON list of step actions for integration testing.
    Used by ``tests/test_orchestrator_minimal_loop.py``.

Do NOT set ``status = "completed"`` without executing and verifying steps.
Do NOT use "command" as a tool type — it is not in the canonical registry.
"""

import json
import os
import time
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from rfsn_kernel.kernel import HardKernel
from rfsn_kernel.normalize import normalize
from rfsn_kernel.validate import validate
from rfsn_kernel.tool_registry import CANONICAL_TOOLS
from services.orchestrator.session_state import ensure_run_context, clear_run_context
from services.orchestrator.executor_client import sandbox_create, sandbox_destroy
from services.orchestrator.kernel_bridge import execute_approved_step, LedgerSink
from services.orchestrator.replay_manager import (
    init_replay_manifest,
    finalize_replay_manifest,
)

MAX_ITERATIONS = int(os.getenv("RFSN_MAX_ITERATIONS", "3"))
MAX_TEST_SECONDS = int(os.getenv("RFSN_MAX_TEST_SECONDS", "60"))
ALLOW_SEMANTIC_PATCH = os.getenv("RFSN_ALLOW_SEMANTIC_PATCH", "false").lower() == "true"
ALLOW_TRACE_EXECUTION = False  # Hard-disabled; do not make configurable.

# Sandbox mode: default is local_dev (trusted repos only).
# Set RFSN_SANDBOX_MODE=docker for Docker-based isolation.
SANDBOX_MODE = os.getenv("RFSN_SANDBOX_MODE", "local_dev")


class RunReq(BaseModel):
    repo_id: str
    task: str
    max_iters: int = MAX_ITERATIONS
    scenario: Optional[str] = None
    confidence_threshold: float = 0.7
    # Optional pre-defined plan for manual/test mode.
    # Each entry is a step dict compatible with the canonical tool registry.
    manual_plan: Optional[List[Dict[str, Any]]] = None


def _is_disabled_tool(step_type: str) -> bool:
    spec = CANONICAL_TOOLS.get(step_type)
    return spec is not None and not spec.enabled


def _validate_step(step: Dict[str, Any], run_id: str) -> Optional[str]:
    """Return an error string if the step is policy-blocked, else None."""
    step_type = step.get("type", "")
    # Non-canonical tool names (e.g. "command") are always rejected.
    if step_type not in CANONICAL_TOOLS:
        return f"UNKNOWN_TOOL: {step_type!r} is not in the canonical registry"
    if _is_disabled_tool(step_type):
        return f"TOOL_DISABLED: {step_type!r} is disabled"
    if step_type == "apply_semantic_patch" and not ALLOW_SEMANTIC_PATCH:
        return "TOOL_DISABLED: apply_semantic_patch is not enabled (RFSN_ALLOW_SEMANTIC_PATCH=false)"
    return None


def run_logic(run_id: str, req: RunReq, kernel: HardKernel, ledger: LedgerSink) -> dict:
    """Main execution loop logic.

    Returns a dict with keys: run_id, status, reason.

    Possible statuses
    -----------------
    ``dry_run``
        No planner configured and no manual_plan provided.
    ``completed``
        All steps in manual_plan were executed without error.
    ``policy_denied``
        A step was blocked by policy (disabled tool, unsafe action, etc.).
    ``no_op_stopped``
        A patch step produced no change.
    ``max_iterations``
        Loop hit the iteration ceiling without completing.
    ``error``
        Unexpected exception.
    """
    import threading

    repo_id = req.repo_id

    # ── 1. Init Session ────────────────────────────────────────────────────
    ctx = ensure_run_context(run_id)
    ctx["repo_id"] = repo_id
    ctx["approval_event"] = threading.Event()
    ctx["status"] = "running"

    # ── 2. Init Ledger / Manifest ──────────────────────────────────────────
    manifest_base = init_replay_manifest(
        run_id=run_id,
        repo_id=repo_id,
        task=req.task,
        scenario=req.scenario or "",
        run_seed=0,
        env_snapshot={
            "allow_semantic_patch": ALLOW_SEMANTIC_PATCH,
            "allow_trace_execution": ALLOW_TRACE_EXECUTION,
            "max_iterations": req.max_iters,
            "sandbox_mode": SANDBOX_MODE,
        },
    )
    ledger.append({"type": "RUN_START", "run_id": run_id, "manifest": manifest_base})

    # ── 3. Dry-run gate ────────────────────────────────────────────────────
    if req.manual_plan is None:
        ledger.append({
            "type": "DRY_RUN",
            "msg": "No planner configured. Dry-run only.",
        })
        finalize_replay_manifest(
            run_id=run_id,
            repo_id=repo_id,
            manifest=manifest_base,
            status="dry_run",
            reason="No planner configured. Dry-run only.",
        )
        clear_run_context(run_id)
        return {
            "run_id": run_id,
            "status": "dry_run",
            "reason": "No planner configured. Dry-run only.",
        }

    # ── 4. Sandbox ─────────────────────────────────────────────────────────
    sandbox_info = sandbox_create(run_id, repo_id)
    ledger.append({"type": "SANDBOX_INIT", "info": sandbox_info})

    # ── 5. Main Loop ───────────────────────────────────────────────────────
    plan = list(req.manual_plan)
    iters = 0
    status = "max_iterations"
    reason = "max_iterations"

    try:
        while iters < min(req.max_iters, MAX_ITERATIONS) and plan:
            iters += 1
            step_intent = plan.pop(0)

            ledger.append({
                "type": "STEP_PLAN",
                "iteration": iters,
                "step": step_intent,
            })

            # ── Policy check ──────────────────────────────────────────────
            policy_error = _validate_step(step_intent, run_id)
            if policy_error:
                status = "policy_denied"
                reason = policy_error
                ledger.append({
                    "type": "POLICY_DENIED",
                    "iteration": iters,
                    "reason": policy_error,
                    "step": step_intent,
                })
                break

            # ── Optional HITL confidence gate ─────────────────────────────
            # Steps may carry an optional "confidence" float (0–1).  When
            # below req.confidence_threshold, the loop pauses for human
            # approval before executing.  This requires a real planner to be
            # useful; manual_plan tests may include confidence explicitly.
            confidence = step_intent.get("confidence")
            if confidence is not None and float(confidence) < req.confidence_threshold:
                ctx["status"] = "paused"
                ledger.append({
                    "type": "HITL_PAUSE",
                    "iteration": iters,
                    "reason": f"Low confidence ({confidence} < {req.confidence_threshold})",
                    "step": step_intent,
                })
                approval_event = ctx.get("approval_event")
                if approval_event is not None:
                    approval_event.wait()
                    approval_event.clear()
                if ctx.get("approval_result") == "rejected":
                    status = "aborted"
                    reason = "User rejected step"
                    ledger.append({"type": "HITL_REJECT", "iteration": iters, "user": "human"})
                    break
                ledger.append({"type": "HITL_APPROVE", "iteration": iters, "user": "human"})
                ctx["status"] = "running"

            # ── Execute via Kernel Bridge ─────────────────────────────────
            result = execute_approved_step(
                kernel=kernel,
                ledger=ledger,
                repo_id=repo_id,
                it=iters,
                step=step_intent,
                run_id=run_id,
                intent=step_intent.get("intent", f"step {iters}"),
                tier_now=1,
            )

            step_record = {
                "iteration": iters,
                "step": step_intent,
                "result": result,
            }
            ctx.setdefault("steps", []).append(step_record)

            if not result.get("ok"):
                reason_str = result.get("reason") or result.get("error") or "unknown"
                # Detect no-op patches.
                if "no_op" in str(reason_str).lower() or "noop" in str(reason_str).lower():
                    status = "no_op_stopped"
                    reason = f"No-op patch detected at iteration {iters}: {reason_str}"
                else:
                    status = "error"
                    reason = f"Step failed at iteration {iters}: {reason_str}"
                ledger.append({
                    "type": "STEP_FAILED",
                    "iteration": iters,
                    "reason": reason,
                })
                break

            ledger.append({
                "type": "STEP_OK",
                "iteration": iters,
                "result": result,
            })

        else:
            # Loop exhausted without break.
            if not plan:
                # All planned steps completed.
                status = "completed"
                reason = f"All {iters} planned steps completed"
            # else: max_iterations is already the default.

    except Exception as exc:
        status = "error"
        reason = str(exc)
        ledger.append({"type": "RUN_ERROR", "error": str(exc)})

    finally:
        # ── Cleanup ───────────────────────────────────────────────────────
        try:
            sandbox_destroy(run_id, repo_id)
        except Exception:
            pass

        try:
            finalize_replay_manifest(
                run_id=run_id,
                repo_id=repo_id,
                manifest=manifest_base,
                status=status,
                reason=reason,
            )
        except Exception:
            pass

        # Best-effort trajectory harvesting (optional; fails silently).
        try:
            from services.learner_service.store_duckdb import DuckStore

            store = DuckStore("data/learner.duckdb")
            store.record_trajectory(
                run_id=run_id,
                repo_id=repo_id,
                task_hash=str(hash(req.task)),
                strategy_id="default_v1",
                success=(status == "completed"),
                steps=ctx.get("steps", []),
            )
        except Exception:
            pass  # Non-fatal: trajectory store is optional.

        clear_run_context(run_id)

    return {"run_id": run_id, "status": status, "reason": reason}
