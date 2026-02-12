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


def test_executor_defaults_to_non_docker_mode():
    compose = (ROOT / "docker-compose.yml").read_text(
        encoding="utf-8",
    )
    assert "RFSN_EXEC_USE_DOCKER" in compose


def test_compose_defaults_to_fail_closed_auth_mode():
    compose = (ROOT / "docker-compose.yml").read_text(
        encoding="utf-8",
    )
    assert "RFSN_DEV_MODE: ${RFSN_DEV_MODE:-0}" in compose
