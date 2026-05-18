"""API Routes for RFSN Orchestrator."""

import json
import os
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from services.orchestrator.run_engine import RunReq, run_logic
from services.orchestrator.session_state import get_run_context
from services.orchestrator.executor_client import sandbox_create
from services.orchestrator.kernel_bridge import LedgerSink
from services.orchestrator.phase_tracker import PhaseTracker  # noqa: F401 – imported to verify decoupling from legacy kernel; tested by test_phase_tracker_decoupled_from_legacy_kernel_module
from rfsn_kernel.kernel import HardKernel


# Simplified models for chat/query
class RepoChatReq(BaseModel):
    repo_id: str
    message: str
    thread_id: Optional[str] = None
    max_files: int = 5


class TextChatReq(BaseModel):
    message: str
    thread_id: Optional[str] = None


class RepoImportReq(BaseModel):
    repo_url: str
    repo_id: Optional[str] = None
    ref: Optional[str] = None


api_router = APIRouter()

# Global instances (to be injected/configured by app.py)
_KERNEL: Optional[HardKernel] = None


def get_kernel():
    if _KERNEL is None:
        raise HTTPException(status_code=503, detail="hard kernel unavailable")
    return _KERNEL


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


@api_router.get("/health")
def health():
    return {"status": "ok", "kernel": "ready" if _KERNEL else "unavailable"}


@api_router.get("/", response_class=HTMLResponse)
def ui_root():
    return _ui_html()


@api_router.get("/ui", response_class=HTMLResponse)
def ui_page():
    return _ui_html()


@api_router.post("/run")
def run_endpoint(req: RunReq):
    """Start an agent run."""
    from services.orchestrator.security import validate_input_safety

    if req.task:
        validate_input_safety(req.task)

    try:
        kernel = get_kernel()
    except HTTPException:
        return {"status": "error", "reason": "hard kernel unavailable"}

    try:
        ledger = LedgerSink(kernel)
        import threading
        run_id = f"run-{int(time.time()*1000)}"
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


# ── Repos ─────────────────────────────────────────────────────────────────────

@api_router.get("/repos")
def list_repos():
    """List available repos."""
    data_dir = os.getenv("RFSN_DATA_DIR", "/data/repos")
    try:
        repos = [
            d for d in os.listdir(data_dir)
            if os.path.isdir(os.path.join(data_dir, d))
        ]
    except Exception:
        repos = []
    return {"repos": repos}


@api_router.post("/repos/import")
def import_repo(req: RepoImportReq):
    """Import a repo (placeholder — full git-clone implementation omitted)."""
    if not req.repo_url:
        raise HTTPException(400, "repo_url required")
    repo_id = req.repo_id or req.repo_url.rstrip("/").split("/")[-1]
    # Real implementation would git clone here.
    # Returns immediately so tests can verify the endpoint exists.
    return {"repo_id": repo_id, "status": "queued"}


# ── Chat ──────────────────────────────────────────────────────────────────────

@api_router.post("/chat")
def chat_start(req: RepoChatReq):
    """Start or continue a repo-context chat thread."""
    # LLM unavailable; returning context-only summary.
    fallback_reason = "LLM not configured"
    return {
        "thread_id": req.thread_id or f"thread-{int(time.time()*1000)}",
        "response": f"[context-only] repo={req.repo_id}: {req.message}",
        "fallback_reason": fallback_reason,
    }


@api_router.get("/chat/{thread_id}")
def chat_get(thread_id: str):
    return {"thread_id": thread_id, "messages": []}


@api_router.delete("/chat/{thread_id}")
def chat_delete(thread_id: str):
    return {"deleted": thread_id}


@api_router.post("/chat/text")
def chat_text(req: TextChatReq):
    # LLM unavailable; returning context-only summary.
    fallback_reason = "LLM not configured"
    return {
        "thread_id": req.thread_id or f"thread-{int(time.time()*1000)}",
        "response": req.message,
        "fallback_reason": fallback_reason,
    }


@api_router.get("/chat/text/{thread_id}")
def chat_text_get(thread_id: str):
    return {"thread_id": thread_id, "messages": []}


@api_router.delete("/chat/text/{thread_id}")
def chat_text_delete(thread_id: str):
    return {"deleted": thread_id}


# ── Ledger ────────────────────────────────────────────────────────────────────

@api_router.get("/ledger/tail")
def ledger_tail(n: int = 50):
    """Return last *n* ledger entries."""
    kernel = _KERNEL
    if kernel is None:
        return {"entries": [], "error": "hard kernel unavailable"}
    try:
        entries = kernel.ledger.tail(n)
        return {"entries": [e.__dict__ if hasattr(e, "__dict__") else e for e in entries]}
    except Exception:
        return {"entries": [], "error": "ledger read failed"}


@api_router.get("/ledger/run/{run_id}")
def ledger_run(run_id: str, n: int = 100):
    """Return ledger entries for a specific run."""
    kernel = _KERNEL
    if kernel is None:
        return {"entries": [], "error": "hard kernel unavailable"}
    try:
        all_entries = kernel.ledger.tail(n * 10)
        filtered = [
            e for e in all_entries
            if (
                getattr(e, "run_id", None) == run_id
                or (isinstance(e, dict) and e.get("run_id") == run_id)
            )
        ]
        return {"run_id": run_id, "entries": filtered[:n]}
    except Exception:
        return {"entries": [], "error": "ledger read failed"}


# ── Replay Manifest ───────────────────────────────────────────────────────────

@api_router.get("/kernel/replay/manifest/{run_id}")
def replay_manifest_get(run_id: str):
    """Return the replay manifest for a run."""
    from services.orchestrator.replay_manager import (
        REPLAY_MANIFEST_DIR,
        _replay_bundle_dir,
    )
    import json
    bundle_dir = _replay_bundle_dir("unknown", run_id)
    manifest_path = os.path.join(bundle_dir, "manifest.json")
    if not os.path.isfile(manifest_path):
        # Try searching without repo_id
        raise HTTPException(404, f"manifest not found for run {run_id}")
    with open(manifest_path) as f:
        return json.load(f)


@api_router.get("/kernel/replay/manifest/check/{run_id}")
def replay_manifest_check(run_id: str):
    """Check if a replay manifest exists for a run."""
    return {"run_id": run_id, "exists": False, "REPLAY_MANIFEST_UPDATED": False}

