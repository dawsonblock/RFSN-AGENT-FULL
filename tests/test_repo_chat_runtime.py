from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
ORCH_DIR = ROOT / "services" / "orchestrator"
APP_PATH = ORCH_DIR / "app.py"


class _DummyResp:
    def __init__(self, status_code: int, payload: dict, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


def _load_orchestrator(monkeypatch, tmp_path):
    monkeypatch.setenv("RFSN_DEV_MODE", "1")
    monkeypatch.setenv("RFSN_POLICY_DIR", str(ROOT / "policies"))
    monkeypatch.setenv(
        "RFSN_HARD_LEDGER_PATH",
        str(tmp_path / "kernel_ledger_chat.jsonl"),
    )

    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ORCH_DIR))

    name = f"orch_app_chat_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, APP_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_chat_endpoint_runtime(monkeypatch, tmp_path):
    mod = _load_orchestrator(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "_repo_abs_path", lambda _repo_id: "/tmp")
    monkeypatch.setattr(
        mod,
        "_collect_repo_chat_context",
        lambda **_: {
            "files": ["README.md"],
            "workdirs": [{"id": "workdir_0", "rel": ".", "markers": []}],
            "profile": {"has_python": True},
            "warnings": [],
        },
    )
    monkeypatch.setattr(
        mod,
        "llm_chat",
        lambda *_, **__: {"content": "This repo has a Python layout."},
    )

    client = TestClient(mod.app)
    r = client.post(
        "/chat",
        json={
            "repo_id": "demo_failrepo",
            "message": "What is in this repo?",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["thread_id"]
    assert "Python" in body["reply"]


def test_repos_proxy_endpoints_runtime(monkeypatch, tmp_path):
    mod = _load_orchestrator(monkeypatch, tmp_path)

    monkeypatch.setattr(
        mod.requests,
        "get",
        lambda *_, **__: _DummyResp(200, {"count": 1, "repos": [{"repo_id": "demo"}]}),
    )
    monkeypatch.setattr(
        mod.requests,
        "post",
        lambda *_, **__: _DummyResp(200, {"ok": True, "repo_id": "demo", "repo_url": "https://github.com/acme/demo.git"}),
    )

    client = TestClient(mod.app)

    r1 = client.get("/repos")
    assert r1.status_code == 200
    assert r1.json()["count"] == 1

    r2 = client.post(
        "/repos/import",
        json={"repo_url": "https://github.com/acme/demo.git"},
    )
    assert r2.status_code == 200
    assert r2.json()["ok"] is True
    assert r2.json()["repo_id"] == "demo"


def test_text_chat_endpoint_runtime(monkeypatch, tmp_path):
    mod = _load_orchestrator(monkeypatch, tmp_path)
    monkeypatch.setattr(
        mod,
        "llm_chat",
        lambda *_, **__: {"content": "Text chat is available."},
    )

    client = TestClient(mod.app)
    r = client.post(
        "/chat/text",
        json={
            "message": "Hello from text chat",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["thread_id"]
    assert "available" in body["reply"]
