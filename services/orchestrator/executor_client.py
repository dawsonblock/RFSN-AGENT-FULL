"""Client for interacting with Executor and Tool Gateway."""

import os
import requests
from fastapi import HTTPException
from typing import Optional, Dict

# Configuration
TOOL_GATEWAY_URL = os.getenv("TOOL_GATEWAY_URL", "http://tool_gateway:8002")
EXECUTOR_URL = os.getenv("EXECUTOR_URL", "http://executor:8003")
WARM_SANDBOX = os.getenv("RFSN_WARM_SANDBOX", "1") == "1"

# Auth helper (imported here to assume it's available in utils or similar,
# but for now we'll duplicate the simple logic or import from a shared util if it existed.
# app.py had `auth_headers` function. We should probably extract that too.
# For now, let's implement a simple version.)


def auth_headers() -> Dict[str, str]:
    token = os.getenv("RFSN_SERVICE_TOKEN", "")
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


def sandbox_create(run_id: str, repo_id: str) -> Optional[dict]:
    """Ask executor to spin up a warm sandbox."""
    if not WARM_SANDBOX:
        return None
    try:
        r = requests.post(
            f"{EXECUTOR_URL}/sandbox/create",
            json={"run_id": run_id, "repo_id": repo_id},
            headers=auth_headers(),
            timeout=30,
        )
        if r.status_code == 200:
            return r.json()
    except Exception as exc:
        print(f"WARN: sandbox create failed: {exc}", flush=True)
    return None


def sandbox_destroy(run_id: str, repo_id: str) -> Optional[dict]:
    """Tear down the warm sandbox for a run."""
    if not WARM_SANDBOX:
        return None
    try:
        r = requests.post(
            f"{EXECUTOR_URL}/sandbox/destroy",
            json={"run_id": run_id, "repo_id": repo_id},
            headers=auth_headers(),
            timeout=15,
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def run_step(
    repo_id: str,
    it: int,
    step: dict,
    run_id: Optional[str] = None,
    tier: Optional[int] = None,
    warm_sandbox: Optional[bool] = None,
):
    print(f"DEBUG: execute_step {step.get('type')} run={run_id}", flush=True)
    use_warm = WARM_SANDBOX if warm_sandbox is None else bool(warm_sandbox)
    payload = {
        "repo_id": repo_id,
        "iter": it,
        "step": step,
        "warm_sandbox": bool(use_warm),
    }
    if run_id:
        payload["run_id"] = run_id
    if tier is not None:
        payload["tier"] = int(tier)
    headers = dict(auth_headers())
    if tier is not None:
        headers["X-RFSN-Tier"] = str(int(tier))

    try:
        r = requests.post(
            f"{TOOL_GATEWAY_URL}/run_step",
            json=payload,
            headers=headers,
            timeout=(10, 300),
        )
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail=r.text)
        return r.json()
    except requests.RequestException as e:
        print(f"ERROR: run_step failed: {e}", flush=True)
        # Return a simulated failure response so logic can handle it
        return {
            "status": 1,
            "seconds": 0.0,
            "logs": f"Network error during step execution: {str(e)}",
            "failure_kind": "network_error",
        }
