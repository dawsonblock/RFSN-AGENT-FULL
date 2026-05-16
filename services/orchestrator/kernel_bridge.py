"""Bridge to the RFSN Hard Kernel.

Integration note
----------------
Tool execution here flows through ``executor_client.run_step()`` which
dispatches to the executor service (Docker sandbox or local dev mode).
The ``rfsn_kernel.dispatcher`` module provides the canonical ``ToolResult``
schema and disabled-tool gate.  Full service-path integration through
``dispatch_tool()`` is planned once sandbox testing is stable — see
``rfsn_kernel/dispatcher.py`` for the target design.
"""

import os
import json
import time
from typing import Optional

from rfsn_kernel.kernel import HardKernel
from rfsn_kernel.state import Outcome
from rfsn_kernel.hard_ledger import LedgerRecord
from rfsn_kernel.sim_cache import SimCache

from services.orchestrator.session_state import ensure_run_context
from services.orchestrator.executor_client import run_step

# Policy config
_KERNEL_REJECT_RISK_SCORE = 65.0  # Default
_MEMORY_PATH = os.getenv("RFSN_MEMORY_PATH", "/data/memory")

_HAS_HARD_KERNEL = True  # Assume true since we are in the bridge


class LedgerSink:
    """Route orchestrator events into the hard ledger chain."""

    def __init__(self, kernel: Optional[HardKernel]):
        self._kernel = kernel

    def append(self, event: dict) -> None:
        if not event or not self._kernel:
            return
        # Normalize event logic here if needed, or pass raw
        # Using a simplified version of _event_record logic
        # Map orchestrator events to metadata
        meta = {
            "type": event.get("type", "UNKNOWN"),
            "run_id": event.get("run_id", ""),
            "iter": event.get("iter", 0),
            "payload": event,
            "timestamp": time.time(),
        }

        rec = LedgerRecord(
            proposal_hash="",
            simulation={},
            risk={},
            decision="INFO",
            decision_reason="orchestrator_event",
            outcome_hash=None,
            state_hash="",
            verification=None,
            metadata=meta,
        )
        self._kernel.ledger.append(rec)


def execute_approved_step(
    kernel: HardKernel,
    ledger: LedgerSink,
    repo_id: str,
    it: int,
    step: dict,
    run_id: str,
    *,
    context_hash: str = "",
    intent: str = "",
    bundle_id: str = "",
    step_num: Optional[int] = None,
    learner_evidence: Optional[dict] = None,
    tier_now: int = 1,
) -> dict:
    """Execute a kernel-approved step through the hard kernel."""

    run_ctx = ensure_run_context(run_id)

    # Update kernel resource state if needed
    if kernel.state.resource_state.get("run_id") != run_id:
        kernel.state.resource_state["run_id"] = run_id

    exec_meta = {"cache_hit": False, "cache_key": ""}

    def _exec_step(s: dict) -> Outcome:
        """Execution callback for hard kernel."""
        cache = run_ctx.get("sim_cache")
        use_warm_step = not bool(run_ctx.get("force_cold_sandbox", False))

        # Check replaying constraint
        if not use_warm_step and str(s.get("type") or "") == "ensure_deps":
            # Logic for replay mode network restriction would go here
            pass

        cache_key = ""
        r: dict
        if isinstance(cache, SimCache):
            cache_key = cache.key(s, str(s.get("workdir_id") or ""))
            hit = cache.get(cache_key)
            if isinstance(hit, dict):
                exec_meta["cache_hit"] = True
                exec_meta["cache_key"] = cache_key
                r = hit
                ledger.append(
                    {
                        "type": "SIM_CACHE_HIT",
                        "run_id": run_id,
                        "iter": it,
                        "cache_key": cache_key,
                        "step_type": s.get("type", ""),
                    }
                )
            else:
                r = run_step(
                    repo_id, it, s, run_id, tier=tier_now, warm_sandbox=use_warm_step
                )
                cache.put(cache_key, r)
        else:
            r = run_step(
                repo_id, it, s, run_id, tier=tier_now, warm_sandbox=use_warm_step
            )

        ok = r.get("status", 1) == 0
        payload = json.dumps(r, default=str)

        return Outcome(
            success=ok,
            exit_code=r.get("status", 1),
            payload=payload[:30000],
            logs=str(r.get("logs", ""))[:5000],
            duration_sec=float(r.get("seconds", 0)),
        )

    # Ask Kernel to Execute
    kr = kernel.kernel_step(
        step,
        execute_fn=_exec_step,
        context=context_hash,
        intent=intent,
        bundle_id=bundle_id,
        run_id=run_id,
        learner_evidence=learner_evidence,
    )

    # Record to ledger
    hard_rec = {
        "type": "HARD_KERNEL_STEP",
        "run_id": run_id,
        "iter": it,
        "tier": tier_now,
        "phase": kr.phase,
        "approved": kr.approved,
        "success": kr.success,
        "error": kr.error,
        "reason": (kr.decision.reason if kr.decision else ""),
        "sim_cache_hit": bool(exec_meta.get("cache_hit", False)),
        "sim_cache_key": str(exec_meta.get("cache_key", "")),
    }
    ledger.append(hard_rec)

    if not kr.approved:
        return {
            "ok": False,
            "out": None,
            "reason": (
                kr.decision.reason if kr.decision else (kr.error or "kernel_reject")
            ),
            "hard_kernel": True,
        }

    # Parse Output
    out = {}
    if kr.outcome and kr.outcome.payload:
        try:
            out = json.loads(kr.outcome.payload)
        except json.JSONDecodeError:
            out = {}

    if not out and kr.outcome:
        out = {
            "status": kr.outcome.exit_code,
            "payload": kr.outcome.payload,
            "logs": kr.outcome.logs,
            "seconds": kr.outcome.duration_sec,
        }

    return {
        "ok": True,
        "out": out,
        "reason": "",
        "hard_kernel": True,
    }
