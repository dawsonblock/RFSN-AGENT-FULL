from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_executor_no_docker_socket_mount():
    compose = (ROOT / "docker-compose.yml").read_text(
        encoding="utf-8",
    )
    assert "/var/run/docker.sock" not in compose


def test_ci_sanity_script_registered():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8",
    )
    assert "scripts/ci_sanity.sh" in ci


def test_executor_defaults_to_docker_sandbox():
    compose = (ROOT / "docker-compose.yml").read_text(
        encoding="utf-8",
    )
    assert "RFSN_EXEC_USE_DOCKER: ${RFSN_EXEC_USE_DOCKER:-1}" in compose
    assert "RFSN_ALLOW_LOCAL_EXEC: ${RFSN_ALLOW_LOCAL_EXEC:-0}" in compose


def test_compose_defaults_to_fail_closed_auth_mode():
    compose = (ROOT / "docker-compose.yml").read_text(
        encoding="utf-8",
    )
    assert "RFSN_DEV_MODE: ${RFSN_DEV_MODE:-0}" in compose


def test_healthchecks_do_not_depend_on_curl_binary():
    compose = (ROOT / "docker-compose.yml").read_text(
        encoding="utf-8",
    )
    assert "curl -sf" not in compose


def test_orchestrator_has_kernel_mount():
    compose = (ROOT / "docker-compose.yml").read_text(
        encoding="utf-8",
    )
    assert "./rfsn_kernel:/app/rfsn_kernel:ro" in compose


def test_orchestrator_exposes_warm_sandbox_toggle():
    compose = (ROOT / "docker-compose.yml").read_text(
        encoding="utf-8",
    )
    assert "RFSN_WARM_SANDBOX: ${RFSN_WARM_SANDBOX:-1}" in compose


def test_executor_exposes_network_min_tier_toggle():
    compose = (ROOT / "docker-compose.yml").read_text(
        encoding="utf-8",
    )
    assert (
        "RFSN_NETWORK_MIN_TIER: ${RFSN_NETWORK_MIN_TIER:-2}"
        in compose
    )


def test_executor_exposes_strict_image_digest_toggle():
    compose = (ROOT / "docker-compose.yml").read_text(
        encoding="utf-8",
    )
    assert (
        "RFSN_STRICT_IMAGE_DIGEST: ${RFSN_STRICT_IMAGE_DIGEST:-1}"
        in compose
    )


def test_docker_up_script_pins_blessed_image_digest():
    script = (ROOT / "scripts" / "docker_up.sh").read_text(
        encoding="utf-8",
    )
    assert "BLESSED_IMAGE_REF" in script
    assert "docker image inspect" in script
    assert "RepoDigests" in script or "RepoDigests" in script.replace(" ", "")
    assert "upsert_env_kv \"BLESSED_IMAGE\"" in script
