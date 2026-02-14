"""Core Run Engine for RFSN Agent."""

import time
import os
import json
from typing import Optional, List
from pydantic import BaseModel

from rfsn_kernel.kernel import HardKernel
from services.orchestrator.session_state import ensure_run_context, clear_run_context
from services.orchestrator.executor_client import sandbox_create, sandbox_destroy
from services.orchestrator.kernel_bridge import execute_approved_step, LedgerSink
from services.orchestrator.replay_manager import (
    init_replay_manifest,
    finalize_replay_manifest,
    capture_repo_snapshot,
)

# Placeholders for imported logic not fully extracted yet
# In a real full refactor, 'prompts.py' and 'llm_service' logic would be cleaner
# For now, we assume helper functions or mocked behavior for the parts we skipped.


class RunReq(BaseModel):
    repo_id: str
    task: str
    max_iters: int = 3
    scenario: Optional[str] = None


def run_logic(req: RunReq, kernel: HardKernel, ledger: LedgerSink) -> dict:
    """Main execution loop logic."""
    run_id = f"run-{int(time.time()*1000)}"
    repo_id = req.repo_id

    # 1. Init Session
    ensure_run_context(run_id)

    # 2. Init Ledger/Manifest
    manifest_base = init_replay_manifest(
        run_id=run_id,
        repo_id=repo_id,
        task=req.task,
        scenario=req.scenario or "",
        run_seed=12345,  # Placeholder
        env_snapshot={},
    )
    ledger.append({"type": "RUN_START", "run_id": run_id, "manifest": manifest_base})

    # 3. Sandbox
    sandbox_info = sandbox_create(run_id, repo_id)
    ledger.append({"type": "SANDBOX_INIT", "info": sandbox_info})

    # 4. Main Loop
    iters = 0
    status = "completed"
    reason = "max_iters"

    try:
        while iters < req.max_iters:
            iters += 1

            # Placeholder: In real logic, this calls LLM to get next step
            # Here we simulate a simplified step for structure demo
            step_intent = {"type": "command", "cmd": "ls -la"}

            # Execute via Kernel Bridge
            result = execute_approved_step(
                kernel=kernel,
                ledger=ledger,
                repo_id=repo_id,
                it=iters,
                step=step_intent,
                run_id=run_id,
                intent="Simulated step",
                tier_now=1,
            )

            if not result["ok"]:
                status = "failed"
                reason = f"Kernel rejected: {result.get('reason')}"
                break

            # Check for done signal (simplified)
            # if is_done(result): ...

    except Exception as e:
        status = "error"
        reason = str(e)
        ledger.append({"type": "RUN_ERROR", "error": str(e)})
    finally:
        # 5. Cleanup
        sandbox_destroy(run_id, repo_id)
        finalize_replay_manifest(
            run_id=run_id,
            repo_id=repo_id,
            manifest=manifest_base,
            status=status,
            reason=reason,
        )
        clear_run_context(run_id)

    return {"run_id": run_id, "status": status, "reason": reason}
