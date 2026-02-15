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
    confidence_threshold: float = 0.7


def run_logic(run_id: str, req: RunReq, kernel: HardKernel, ledger: LedgerSink) -> dict:
    """Main execution loop logic."""
    # run_id passed in from API
    repo_id = req.repo_id

    # 1. Init Session
    import threading

    ctx = ensure_run_context(run_id)
    ctx["repo_id"] = repo_id
    ctx["approval_event"] = threading.Event()
    ctx["status"] = "running"

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

            # Check for advice/feedback
            advisor = ctx.get("advisor")
            if advisor and advisor.has_pending_advice():
                pending = advisor.get_pending_advice()
                # Log advice to ledger
                ledger.append(
                    {
                        "type": "ADVICE_RECEIVED",
                        "advice": [
                            {
                                "source": a.source,
                                "content": a.content,
                                "timestamp": a.timestamp,
                            }
                            for a in pending
                        ],
                    }
                )
                # In a real run, we would pass 'pending' to the planner here.

            # In a real run, we would call LLM here.
            # For verification/demo of the Master Upgrade, we simulate a sequence:
            # 1. generate_repo_map (Phase 2.1)
            # 2. apply_semantic_patch (Phase 2.3)

            step_intent = {}
            if os.environ.get("RFSN_DEMO_MODE", "0") == "1":
                # Demo mode: hardcoded steps for development/testing
                if iters == 1:
                    step_intent = {
                        "type": "generate_repo_map",
                        "path": ".",
                        "focus": ["important_function"],
                    }
                elif iters == 2:
                    step_intent = {
                        "type": "apply_semantic_patch",
                        "path": "src/utils.py",
                        "patch": "<<<<<<< SEARCH\ndef foo(): pass\n=======\ndef foo(): return 1\n>>>>>>> REPLACE",
                    }
                else:
                    step_intent = {"type": "command", "cmd": "echo 'Done'"}
            else:
                # Production mode: call LLM service for next step
                try:
                    import requests

                    llm_url = os.environ.get("LLM_URL", "http://localhost:8001")
                    resp = requests.post(
                        f"{llm_url}/propose",
                        json={
                            "run_id": run_id,
                            "repo_id": repo_id,
                            "task": req.task,
                            "iteration": iters,
                            "context": ctx.get("steps", []),
                        },
                        timeout=120,
                    )
                    resp.raise_for_status()
                    step_intent = resp.json().get("step", {})
                except Exception as e:
                    ledger.append({"type": "LLM_ERROR", "error": str(e)})
                    status = "error"
                    reason = f"LLM proposal failed: {e}"
                    break

            # Confidence check (Phase 5.1)
            # In demo mode, force low confidence on step 2 to exercise HITL
            if os.environ.get("RFSN_DEMO_MODE", "0") == "1":
                confidence = 0.95 if iters != 2 else 0.5
            else:
                # In production, confidence comes from the LLM proposal
                confidence = float(step_intent.get("_confidence", 0.95))

            if confidence < req.confidence_threshold:
                ctx["status"] = "paused"
                ledger.append(
                    {
                        "type": "HITL_PAUSE",
                        "reason": f"Low confidence ({confidence} < {req.confidence_threshold})",
                        "step": step_intent,
                    }
                )
                # Wait for approval
                ctx["approval_event"].wait()
                ctx["approval_event"].clear()  # Reset for next time

                if ctx.get("approval_result") == "rejected":
                    status = "aborted"
                    reason = "User rejected step"
                    ledger.append({"type": "HITL_REJECT", "user": "human"})
                    break

                ledger.append({"type": "HITL_APPROVE", "user": "human"})
                ctx["status"] = "running"

            # Map V2 tool names if coming from LLM (pseudo-code/comment for implementation)
            # if "tool_name" in action:
            #     step_intent["type"] = action["tool_name"]
            #     step_intent.update(action.get("parameters", {}))

            # Execute via Kernel Bridge
            result = execute_approved_step(
                kernel=kernel,
                ledger=ledger,
                repo_id=repo_id,
                it=iters,
                step=step_intent,
                run_id=run_id,
                intent=f"Simulated Step {iters}",
                tier_now=1,
            )

            # Capture step for trajectory
            step_record = {
                "iteration": iters,
                "intent": step_intent,
                "confidence": confidence,
                "approval": ctx.get("approval_result", "auto"),
                "result": result,
            }
            ctx.setdefault("steps", []).append(step_record)

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

        # 6. Harvest Trajectory (Phase 6.3)
        try:
            from services.learner_service.store_duckdb import DuckStore

            # Ideally this path comes from config
            store = DuckStore("data/learner.duckdb")
            store.record_trajectory(
                run_id=run_id,
                repo_id=repo_id,
                task_hash=str(hash(req.task)),  # Simple hash for now
                strategy_id="default_v1",  # Placeholder
                success=(status == "completed"),
                steps=ctx.get("steps", []),
            )
            print(f"INFO: Trajectory recorded for {run_id}")
        except Exception as e:
            print(f"WARN: Failed to record trajectory: {e}")

        clear_run_context(run_id)

    return {"run_id": run_id, "status": status, "reason": reason}
