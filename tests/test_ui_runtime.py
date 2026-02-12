from __future__ import annotations

import importlib.util
import os
import sys
import uuid
from pathlib import Path

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
ORCH_DIR = ROOT / "services" / "orchestrator"
APP_PATH = ORCH_DIR / "app.py"


def _load_orchestrator_app(monkeypatch, tmp_path):
    monkeypatch.setenv("RFSN_DEV_MODE", "1")
    monkeypatch.setenv("RFSN_POLICY_DIR", str(ROOT / "policies"))
    monkeypatch.setenv(
        "RFSN_HARD_LEDGER_PATH",
        str(tmp_path / "kernel_ledger_runtime.jsonl"),
    )

    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ORCH_DIR))

    name = f"orch_app_runtime_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(
        name,
        APP_PATH,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module.app


def test_ui_routes_render_runtime(monkeypatch, tmp_path):
    app = _load_orchestrator_app(monkeypatch, tmp_path)
    client = TestClient(app)

    r_ui = client.get("/ui")
    assert r_ui.status_code == 200
    assert "RFSN Control Surface" in r_ui.text
    assert 'id="importForm"' in r_ui.text
    assert 'id="chatForm"' in r_ui.text
    assert 'id="textChatForm"' in r_ui.text

    r_root = client.get("/")
    assert r_root.status_code == 200
    assert "RFSN Control Surface" in r_root.text
