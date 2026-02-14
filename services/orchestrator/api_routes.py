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


@api_router.get("/health")
def health():
    return {"status": "ok", "kernel": "ready" if _KERNEL else "unavailable"}


@api_router.post("/run")
def run_endpoint(req: RunReq):
    """Start an agent run."""
    try:
        kernel = get_kernel()
        # Create a ledger sink for this run
        ledger = LedgerSink(kernel)

        # Execute run logic (blocking for now, would be async in real world)
        result = run_logic(req, kernel, ledger)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@api_router.post("/query/repos")
def list_repos():
    # Placeholder implementation
    # In real app.py this scanned /data/repos
    return {"repos": ["demo-repo-1", "demo-repo-2"]}


@api_router.post("/query/chat")
def chat_repo(req: RepoChatReq):
    # Placeholder for repo chat logic
    return {"response": f"Echo: {req.message}", "context": [f"{req.repo_id}/README.md"]}
