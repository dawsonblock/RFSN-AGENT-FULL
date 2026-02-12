from services.tool_gateway.policy import (
    validate_repo_path,
)


def test_validate_repo_path_repo_root_required():
    assert validate_repo_path(
        "src/app.py",
        ["**"],
        [],
        repo_root_required=True,
        repo_root="/data/repos/demo",
    )
    assert not validate_repo_path(
        "../etc/passwd",
        ["**"],
        [],
        repo_root_required=True,
        repo_root="/data/repos/demo",
    )


def test_validate_repo_path_blocked_glob_still_applies():
    assert not validate_repo_path(
        ".env",
        ["**"],
        ["**/.env"],
        repo_root_required=True,
        repo_root="/data/repos/demo",
    )
