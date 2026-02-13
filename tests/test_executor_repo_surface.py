from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXECUTOR_APP = ROOT / "services" / "executor" / "app.py"
SANDBOX_POOL = ROOT / "services" / "executor" / "sandbox_pool.py"


def test_executor_has_repo_import_and_list_endpoints():
    src = EXECUTOR_APP.read_text(encoding="utf-8")
    assert '@app.get("/repos")' in src
    assert '@app.post("/repo/import")' in src
    assert "def _normalize_repo_url(" in src
    assert "def _derive_repo_id(" in src


def test_executor_local_mode_is_dev_only():
    src = EXECUTOR_APP.read_text(encoding="utf-8")
    assert "RFSN_ALLOW_LOCAL_EXEC" in src
    assert "docker sandbox is required" in src
    assert "docker runtime is unavailable" in src


def test_executor_uses_valid_no_new_privileges_flag():
    app_src = EXECUTOR_APP.read_text(encoding="utf-8")
    pool_src = SANDBOX_POOL.read_text(encoding="utf-8")
    assert "--no-new-privileges" not in app_src
    assert "--no-new-privileges" not in pool_src
    assert "--security-opt\", \"no-new-privileges:true" in app_src
    assert "--security-opt\", \"no-new-privileges:true" in pool_src


def test_executor_local_mode_uses_container_data_paths():
    src = EXECUTOR_APP.read_text(encoding="utf-8")
    assert "if USE_DOCKER_SANDBOX:" in src
    assert "repo_exec = repo_local" in src
    assert "art_exec = art_local" in src
    assert "venv_exec = venv_local" in src
    assert "wheels_exec = wheels_local" in src


def test_executor_result_preserves_zero_status():
    src = EXECUTOR_APP.read_text(encoding="utf-8")
    assert "out.get(\"status\", 1) or 1" not in src
    assert "status_raw = out.get(\"status\", 1)" in src


def test_executor_local_mode_materializes_data_files():
    src = EXECUTOR_APP.read_text(encoding="utf-8")
    assert "mkdir -p {shlex.quote(cdir)}" in src
    assert "cp " in src
    assert "# local-mode data mounts" in src


def test_executor_local_mode_rewrites_template_relative_cd():
    src = EXECUTOR_APP.read_text(encoding="utf-8")
    assert "translated = translated.replace(" in src
    assert "\"cd repo\"" in src


def test_executor_health_reports_runtime_mode():
    src = EXECUTOR_APP.read_text(encoding="utf-8")
    assert "\"mode\": \"docker\" if USE_DOCKER_SANDBOX else \"local\"" in src
    assert "\"docker_runtime_available\": DOCKER_RUNTIME_AVAILABLE" in src
