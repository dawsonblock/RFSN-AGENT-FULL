from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXECUTOR_APP = ROOT / "services" / "executor" / "app.py"


def test_executor_has_repo_import_and_list_endpoints():
    src = EXECUTOR_APP.read_text(encoding="utf-8")
    assert '@app.get("/repos")' in src
    assert '@app.post("/repo/import")' in src
    assert "def _normalize_repo_url(" in src
    assert "def _derive_repo_id(" in src
