"""GitHub Webhook Routes for RFSN Orchestrator."""

import os
import hmac
import hashlib
import json
from enum import Enum
from typing import Optional, Dict, Any

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel

# Configuration
GITHUB_SHARED_SECRET = os.getenv("GITHUB_APP_SECRET", "dev-secret")


class GithubEvent(str, Enum):
    PING = "ping"
    PUSH = "push"
    PULL_REQUEST = "pull_request"
    ISSUE_COMMENT = "issue_comment"
    # Add others as needed


github_router = APIRouter()


def verify_signature(payload: bytes, signature_header: str):
    """Verify that the payload was sent from GitHub by validating SHA256.

    Raises:
        HTTPException: If the signature is invalid.
    """
    if not signature_header:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="x-hub-signature-256 header is missing!",
        )

    hash_object = hmac.new(
        GITHUB_SHARED_SECRET.encode("utf-8"), msg=payload, digestmod=hashlib.sha256
    )
    expected_signature = "sha256=" + hash_object.hexdigest()

    if not hmac.compare_digest(expected_signature, signature_header):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Request signature failed verification",
        )


@github_router.post("/webhook")
async def github_webhook(
    request: Request,
    x_github_event: str = Header(...),
    x_hub_signature_256: str = Header(None),
):
    """Receive and process GitHub webhooks."""

    # 1. Read Payload
    payload_bytes = await request.body()

    # 2. Verify Signature
    if os.getenv("RFSN_DEV_MODE", "0") != "1":
        # In prod, strictly verify
        verify_signature(payload_bytes, x_hub_signature_256 or "")
    else:
        # In dev, only verify if secret is set to something other than default
        if GITHUB_SHARED_SECRET != "dev-secret":
            verify_signature(payload_bytes, x_hub_signature_256 or "")

    # 3. Parse JSON
    try:
        data = json.loads(payload_bytes)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # 4. Route Event
    print(f"INFO: Received GitHub Event: {x_github_event}")

    if x_github_event == GithubEvent.PING:
        return {"status": "pong", "zen": data.get("zen")}

    elif x_github_event == GithubEvent.PUSH:
        # Handle push (e.g. trigger run if configured)
        ref = data.get("ref", "")
        print(f"INFO: Push event to {ref}")
        return {"status": "ok", "action": "push_received"}

    elif x_github_event == GithubEvent.PULL_REQUEST:
        action = data.get("action")
        pr = data.get("pull_request", {})
        print(f"INFO: PR event {action} on #{pr.get('number')}")
        # In Phase 5.3, we will trigger validation here
        return {"status": "ok", "action": f"pr_{action}"}

    elif x_github_event == GithubEvent.ISSUE_COMMENT:
        action = data.get("action")
        issue = data.get("issue", {})
        comment = data.get("comment", {})
        repo = data.get("repository", {})

        print(f"INFO: Comment event {action} on issue #{issue.get('number')}")

        if action == "created":
            repo_full_name = repo.get("full_name")
            body = comment.get("body", "")
            user = comment.get("user", {}).get("login", "unknown")

            from services.orchestrator.session_state import (
                get_run_context,
                get_active_run_by_repo,
            )

            # Attempt to find active run for this repo
            run_id = get_active_run_by_repo(repo_full_name)
            if run_id:
                ctx = get_run_context(run_id)
                if ctx and ctx.get("advisor"):
                    ctx["advisor"].ingest_pr_comment(body, user)
                    print(
                        f"INFO: Ingested PR comment for {repo_full_name} in run {run_id}"
                    )
                    return {"status": "ok", "action": "comment_ingested"}
            else:
                print(f"INFO: No active run for {repo_full_name}")

        return {"status": "ok", "action": f"comment_{action}"}

    return {"status": "ignored", "reason": "unhandled_event_type"}
