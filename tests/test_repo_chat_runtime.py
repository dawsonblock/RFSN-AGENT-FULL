"""Runtime integration tests for the repo-chat and repos proxy endpoints.

NOTE: These tests were updated from the monolithic app.py design to the
modular api_routes.py design.  The old app.py-based tests patched module-
level functions (llm_chat, _repo_abs_path, requests) that no longer exist in
app.py.  The updated tests use api_routes.api_router directly with TestClient
and check the current (LLM-less, non-proxying) response contracts.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.orchestrator.api_routes import api_router


def _make_client() -> TestClient:
    app = FastAPI()
    app.include_router(api_router)
    return TestClient(app)


def test_chat_endpoint_runtime(monkeypatch, tmp_path):
    """POST /chat returns 200 with thread_id and response fields.

    Updated: no LLM in current design; endpoint returns context-only fallback.
    The old test patched mod.llm_chat which no longer exists in app.py.
    """
    client = _make_client()
    r = client.post(
        "/chat",
        json={
            "repo_id": "demo_failrepo",
            "message": "What is in this repo?",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert "thread_id" in body
    # Either standard or legacy key name is acceptable.
    assert "response" in body or "reply" in body


def test_repos_proxy_endpoints_runtime(monkeypatch, tmp_path):
    """GET /repos and POST /repos/import return valid responses.

    Updated: endpoints no longer proxy to a backend service;
    they return local data directly.
    The old test patched mod.requests.get/post which no longer exists in app.py.
    """
    client = _make_client()

    r1 = client.get("/repos")
    assert r1.status_code == 200
    body = r1.json()
    assert "repos" in body
    assert isinstance(body["repos"], list)

    r2 = client.post(
        "/repos/import",
        json={"repo_url": "https://github.com/acme/demo.git"},
    )
    assert r2.status_code == 200
    body2 = r2.json()
    assert "repo_id" in body2 or "status" in body2


def test_text_chat_endpoint_runtime(monkeypatch, tmp_path):
    """POST /chat/text returns 200 with thread_id and response fields.

    Updated: no LLM in current design; endpoint returns context-only fallback.
    The old test patched mod.llm_chat which no longer exists in app.py.
    """
    client = _make_client()
    r = client.post(
        "/chat/text",
        json={
            "message": "Hello from text chat",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert "thread_id" in body
    assert "response" in body or "reply" in body

