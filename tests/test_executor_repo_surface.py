from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXECUTOR_APP = ROOT / "services" / "executor" / "app.py"
SANDBOX_POOL = ROOT / "services" / "executor" / "sandbox_pool.py"
CAPSULE_PY = ROOT / "services" / "executor" / "capsule.py"


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
    """Security flags must use the correct long-form: --security-opt no-new-privileges:true.

    Updated: flags live in capsule.py (shared by both app.py and sandbox_pool.py
    via Capsule.docker_args()).  Checking capsule.py is authoritative.
    """
    app_src = EXECUTOR_APP.read_text(encoding="utf-8")
    pool_src = SANDBOX_POOL.read_text(encoding="utf-8")
    capsule_src = CAPSULE_PY.read_text(encoding="utf-8")
    # Ensure the broken short-form is not present anywhere.
    assert "--no-new-privileges" not in app_src
    assert "--no-new-privileges" not in pool_src
    # Correct form must be in capsule.py (the single source of Docker security config).
    assert '"--security-opt"' in capsule_src
    assert '"no-new-privileges:true"' in capsule_src


def test_executor_uses_read_only_rootfs_and_tmpfs():
    """Root filesystem must be read-only with a noexec /tmp tmpfs.

    Updated: flags live in capsule.py (shared by app.py and sandbox_pool.py).
    """
    capsule_src = CAPSULE_PY.read_text(encoding="utf-8")
    assert '"--read-only"' in capsule_src
    assert '"--tmpfs"' in capsule_src
    assert "noexec" in capsule_src
    assert "HOME=/tmp" in capsule_src


def test_executor_enforces_digest_pinned_image_in_strict_mode():
    src = EXECUTOR_APP.read_text(encoding="utf-8")
    assert "RFSN_STRICT_IMAGE_DIGEST" in src
    assert "BLESSED_IMAGE must be digest-pinned" in src


def test_executor_exposes_env_manifest_endpoint():
    src = EXECUTOR_APP.read_text(encoding="utf-8")
    assert '@app.get("/env_manifest")' in src
    assert "\"strict_image_digest\": bool(STRICT_IMAGE_DIGEST)" in src


def test_executor_enforces_artifact_and_log_quotas():
    src = EXECUTOR_APP.read_text(encoding="utf-8")
    assert "max_step_log_bytes" in src
    assert "max_artifact_dir_bytes" in src
    assert "max_artifact_delta_bytes" in src
    assert "artifact_quota_exceeded" in src
    assert "\"logs_truncated\"" in src


def test_executor_runs_tests_in_scratch_repo():
    src = EXECUTOR_APP.read_text(encoding="utf-8")
    assert "/work/scratch_repo" in src
    assert "tracked files mutated during tests" in src


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
