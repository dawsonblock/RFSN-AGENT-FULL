import json
import hmac
import hashlib
import pytest
from fastapi.testclient import TestClient
from services.orchestrator.app import app
from services.orchestrator.session_state import ensure_run_context, clear_run_context

client = TestClient(app)
SECRET = "dev-secret"


def sign_payload(payload: dict) -> str:
    # Use compact separators for signature calculation to match implementation expectations if needed
    # But standard json.dumps is usually fine if receiver parses standard JSON.
    # The key is to match what the 'signer' does.
    # In our tests, we sign the exact bytes we send.
    body = json.dumps(payload).encode("utf-8")
    hash_object = hmac.new(SECRET.encode("utf-8"), msg=body, digestmod=hashlib.sha256)
    return "sha256=" + hash_object.hexdigest()


def test_webhook_security():
    """Verify Phase 5.2: GitHub Webhook Security"""
    payload = {"zen": "Keep it simple."}
    body = json.dumps(payload).encode("utf-8")

    # valid sig
    hash_object = hmac.new(SECRET.encode("utf-8"), msg=body, digestmod=hashlib.sha256)
    valid_sig = "sha256=" + hash_object.hexdigest()

    # 1. Valid request
    resp = client.post(
        "/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "ping",
            "X-Hub-Signature-256": valid_sig,
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 200, f"Valid webhook failed: {resp.text}"
    assert resp.json().get("status") == "pong"

    # 2. Invalid sig
    resp = client.post(
        "/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "ping",
            "X-Hub-Signature-256": "sha256=bad",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 403, "Invalid signature should be 403"

    # 3. Missing sig
    resp = client.post(
        "/webhook",
        content=body,
        headers={"X-GitHub-Event": "ping", "Content-Type": "application/json"},
    )
    assert resp.status_code == 403, "Missing signature should be 403"


def test_pr_comment_flow():
    """Verify Phase 5.3: PR Comment to Advisor Flow"""
    run_id = "test-gitops-run"
    repo_id = "owner/gitops-repo"

    # Setup context
    ctx = ensure_run_context(run_id)
    ctx["repo_id"] = repo_id
    ctx["status"] = "running"

    payload = {
        "action": "created",
        "issue": {"number": 101},
        "comment": {"body": "RFSN: fix typo", "user": {"login": "human"}},
        "repository": {"full_name": repo_id},
    }
    body = json.dumps(payload).encode("utf-8")
    hash_object = hmac.new(SECRET.encode("utf-8"), msg=body, digestmod=hashlib.sha256)
    sig = "sha256=" + hash_object.hexdigest()

    resp = client.post(
        "/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "issue_comment",
            "X-Hub-Signature-256": sig,
            "Content-Type": "application/json",
        },
    )

    assert resp.status_code == 200
    assert resp.json()["action"] == "comment_ingested"

    # Check Advisor
    advisor = ctx.get("advisor")
    assert advisor is not None
    assert advisor.has_pending_advice()
    advice = advisor.get_pending_advice()[0]
    assert advice.content == "RFSN: fix typo"

    clear_run_context(run_id)
