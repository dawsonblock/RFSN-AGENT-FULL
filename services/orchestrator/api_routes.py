"""API Routes for RFSN Orchestrator."""

from fastapi import APIRouter, HTTPException, Depends
from typing import Optional, List
from pydantic import BaseModel

from services.orchestrator.run_engine import RunReq, run_logic
from services.orchestrator.session_state import get_run_context
from services.orchestrator.executor_client import sandbox_create
from services.orchestrator.kernel_bridge import LedgerSink
from rfsn_kernel.kernel import HardKernel


# Simplified models for chat/query
class RepoChatReq(BaseModel):
    repo_id: str
    message: str
    thread_id: Optional[str] = None
    max_files: int = 5


api_router = APIRouter()

# Global instances (to be injected/configured by app.py)
_KERNEL: Optional[HardKernel] = None


def get_kernel():
    if _KERNEL is None:
        raise HTTPException(status_code=503, detail="Kernel not initialized")
    return _KERNEL


from fastapi.responses import HTMLResponse
import os


@api_router.get("/health")
def health():
    return {"status": "ok", "kernel": "ready" if _KERNEL else "unavailable"}


def _ui_html() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    ui_path = os.path.join(here, "ui", "index.html")
    try:
        with open(ui_path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return (
            "<!doctype html><html><body>"
            "<h1>RFSN UI unavailable</h1>"
            "<p>Missing services/orchestrator/ui/index.html</p>"
            "</body></html>"
        )


@api_router.get("/", response_class=HTMLResponse)
def ui_root():
    return _ui_html()


@api_router.get("/ui", response_class=HTMLResponse)
def ui_page():
    return _ui_html()


@api_router.post("/run")
def run_endpoint(req: RunReq):
    """Start an agent run."""
    # Phase 4.1: Firewall Check
    from services.orchestrator.security import validate_input_safety

    # Validate the task description/prompt
    if req.task:
        validate_input_safety(req.task)

    try:
        kernel = get_kernel()
        # Create a ledger sink for this run
        ledger = LedgerSink(kernel)

        # Generate run_id here
        import time, threading

        run_id = f"run-{int(time.time()*1000)}"

        # Execute in background thread
        t = threading.Thread(
            target=run_logic, args=(run_id, req, kernel, ledger), daemon=True
        )
        t.start()

        return {"run_id": run_id, "status": "starting"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@api_router.get("/run/{run_id}/status")
def run_status(run_id: str):
    ctx = get_run_context(run_id)
    if not ctx:
        raise HTTPException(404, "Run not found")

    return {
        "status": ctx.get("status", "unknown"),
        "approval_needed": ctx.get("status") == "paused",
        "last_step": ctx.get(
            "last_step_intent"
        ),  # We didn't save this yet, but good for UI
    }


@api_router.post("/run/{run_id}/approve")
def approve_step(run_id: str):
    ctx = get_run_context(run_id)
    if not ctx:
        raise HTTPException(404, "Run not found")

    if ctx.get("status") != "paused":
        raise HTTPException(400, "Run is not paused")

    ctx["approval_result"] = "approved"
    if ctx.get("approval_event"):
        ctx["approval_event"].set()

    return {"status": "approved"}


@api_router.post("/run/{run_id}/reject")
def reject_step(run_id: str):
    ctx = get_run_context(run_id)
    if not ctx:
        raise HTTPException(404, "Run not found")

    if ctx.get("status") != "paused":
        raise HTTPException(400, "Run is not paused")

    ctx["approval_result"] = "rejected"
    if ctx.get("approval_event"):
        ctx["approval_event"].set()

    return {"status": "rejected"}


@api_router.post("/query/repos")
def list_repos():
    # Placeholder implementation
    # In real app.py this scanned /data/repos
    return {"repos": ["demo-repo-1", "demo-repo-2"]}


@api_router.post("/query/chat")
def chat_repo(req: RepoChatReq):
    # Placeholder for repo chat logic
    return {"response": f"Echo: {req.message}", "context": [f"{req.repo_id}/README.md"]}
