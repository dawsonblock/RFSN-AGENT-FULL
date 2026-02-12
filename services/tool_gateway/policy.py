import fnmatch
import os
from pathlib import PurePosixPath


def is_safe_relpath(path: str) -> bool:
    """Validate that path is a safe repo-root-relative path.

    Paths are relative to the repo root (e.g. src/foo.py,
    tests/test_x.py).  Never prefixed with 'repo/'.
    Rejects absolute, home-relative, and traversal paths.
    """
    if not path or path.startswith("/") or path.startswith("~"):
        return False
    p = PurePosixPath(path)
    if not p.parts:
        return False
    if ".." in p.parts:
        return False
    # Null byte injection
    if "\x00" in path:
        return False
    return True


def glob_blocked(path: str, blocked_globs: list[str]) -> bool:
    for g in blocked_globs or []:
        if fnmatch.fnmatch(path, g):
            return True
        # fnmatch doesn't recurse '**/' like glob.
        # Also check the bare filename against the
        # pattern's leaf (e.g. "**/.env" blocks ".env"
        # at any depth including root).
        if g.startswith("**/"):
            leaf = g[3:]  # strip "**/"
            if fnmatch.fnmatch(path, leaf):
                return True
            # Check path suffix matches.
            if ("/" + path).endswith("/" + leaf):
                return True
    return False


def validate_repo_path(
    path: str,
    allowed_paths: list[str],
    blocked_globs: list[str],
    *,
    repo_root_required: bool = True,
    repo_root: str = "",
) -> bool:
    if repo_root_required:
        if not is_safe_relpath(path):
            return False
        root = os.path.abspath(
            repo_root or "/work/repo",
        )
        abs_path = os.path.abspath(
            os.path.join(root, path),
        )
        if not (
            abs_path == root
            or abs_path.startswith(root + os.sep)
        ):
            return False
    elif not is_safe_relpath(path):
        return False
    if glob_blocked(path, blocked_globs):
        return False
    return any(
        fnmatch.fnmatch(path, ap)
        for ap in (allowed_paths or [])
    )
