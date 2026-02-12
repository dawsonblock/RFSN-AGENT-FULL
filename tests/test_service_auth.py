import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
AUTH_PATH = ROOT / "shared" / "auth.py"


def _load_auth_module(module_name: str):
    spec = importlib.util.spec_from_file_location(
        module_name,
        AUTH_PATH,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_auth_requires_token_outside_dev_mode(monkeypatch):
    monkeypatch.setenv("RFSN_DEV_MODE", "0")
    monkeypatch.delenv("RFSN_SERVICE_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        _load_auth_module("auth_require_token_case")


def test_auth_allows_empty_token_only_in_dev_mode(monkeypatch):
    monkeypatch.setenv("RFSN_DEV_MODE", "1")
    monkeypatch.delenv("RFSN_SERVICE_TOKEN", raising=False)
    mod = _load_auth_module("auth_dev_mode_case")
    assert mod.RFSN_DEV_MODE is True
    assert mod.get_service_token() == ""
    assert mod.auth_headers() == {}


def test_auth_headers_include_token_when_configured(monkeypatch):
    monkeypatch.setenv("RFSN_DEV_MODE", "0")
    monkeypatch.setenv("RFSN_SERVICE_TOKEN", "secret-token")
    mod = _load_auth_module("auth_token_case")
    assert mod.get_service_token() == "secret-token"
    assert mod.auth_headers() == {
        "Authorization": "Bearer secret-token",
    }


def test_health_not_public_when_auth_enabled(monkeypatch):
    monkeypatch.setenv("RFSN_DEV_MODE", "0")
    monkeypatch.setenv("RFSN_SERVICE_TOKEN", "secret-token")
    mod = _load_auth_module("auth_health_private_case")
    assert "/health" not in mod._PUBLIC_PATHS


def test_ui_paths_public_for_dashboard_bootstrap(monkeypatch):
    monkeypatch.setenv("RFSN_DEV_MODE", "0")
    monkeypatch.setenv("RFSN_SERVICE_TOKEN", "secret-token")
    mod = _load_auth_module("auth_ui_public_case")
    assert "/ui" in mod._PUBLIC_PATHS
    assert "/" in mod._PUBLIC_PATHS
