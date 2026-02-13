from services.tool_gateway.policy import (
    extract_patch_touched_paths,
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


def test_validate_repo_path_blocks_read_prefix_and_suffix():
    assert not validate_repo_path(
        "scripts/bootstrap.sh",
        ["**"],
        [],
        repo_root_required=True,
        repo_root="/data/repos/demo",
        blocked_prefixes=["scripts/"],
        blocked_suffixes=[],
    )
    assert not validate_repo_path(
        "src/secret.pem",
        ["**"],
        [],
        repo_root_required=True,
        repo_root="/data/repos/demo",
        blocked_prefixes=[],
        blocked_suffixes=[".pem"],
    )
    assert not validate_repo_path(
        ".env",
        ["**"],
        [],
        repo_root_required=True,
        repo_root="/data/repos/demo",
        blocked_prefixes=[],
        blocked_suffixes=[".env"],
    )


def test_extract_patch_touched_paths_captures_header_only_unified_diff():
    patch = (
        "--- a/src/a.py\n"
        "+++ b/src/a.py\n"
        "@@ -1 +1 @@\n"
        "-x\n"
        "+y\n"
        "--- a/src/b.py\n"
        "+++ b/src/b.py\n"
        "@@ -1 +1 @@\n"
        "-m\n"
        "+n\n"
    )
    touched = extract_patch_touched_paths(patch)
    assert touched == {"src/a.py", "src/b.py"}
