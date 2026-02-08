import yaml  # type: ignore[import-untyped]
from hypothesis import (  # type: ignore[import-not-found]
    given,
    strategies as st,
    settings,
    HealthCheck,
)
from services.tool_gateway.policy import (  # type: ignore[import-not-found]
    is_safe_relpath,
    validate_repo_path,
)

ALLOW = yaml.safe_load(
    open(
        "policies/tool_allowlist.yaml",
        "r", encoding="utf-8",
    )
)
ALLOWED_PATHS = ALLOW.get(
    "allowed_paths", ["**"],
)
BLOCKED_GLOBS = ALLOW.get("blocked_globs", [])

CHARS = st.characters(
    blacklist_categories=("Cs",),
    min_codepoint=32,
    max_codepoint=0x10FFFF,
)
PATH_STR = st.text(CHARS, min_size=0, max_size=120)


@settings(
    max_examples=2000,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(PATH_STR)
def test_is_safe_relpath_never_allows_absolute_or_traversal(p):
    if p.startswith("/") or p.startswith("~"):
        assert is_safe_relpath(p) is False
    if (
        "/../" in p
        or p.endswith("/..")
        or p.startswith("../")
        or p == ".."
    ):
        assert is_safe_relpath(p) is False
    if "\x00" in p:
        assert is_safe_relpath(p) is False


@settings(
    max_examples=2500,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(PATH_STR)
def test_validate_repo_path_rejects_unsafe_paths(p):
    ok = validate_repo_path(
        p, ALLOWED_PATHS, BLOCKED_GLOBS,
    )
    if ok:
        # Must be a safe relative path
        assert not p.startswith("/")
        assert not p.startswith("~")
        assert ".." not in p.split("/")


def test_validate_repo_path_allows_repo_relative():
    """Paths are now repo-root-relative, no repo/ prefix."""
    candidates = [
        "src/foo.py",
        "tests/test_bar.py",
        "lib/utils/helper.py",
    ]
    for c in candidates:
        assert validate_repo_path(
            c, ALLOWED_PATHS, BLOCKED_GLOBS,
        ) is True


def test_validate_repo_path_blocks_sensitive_globs():
    candidates = [
        ".env", "key.pem",
        ".git/config",
        "secrets/id_rsa",
    ]
    for c in candidates:
        assert validate_repo_path(
            c, ALLOWED_PATHS, BLOCKED_GLOBS,
        ) is False
