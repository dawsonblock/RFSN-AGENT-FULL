import fnmatch
from pathlib import PurePosixPath


def is_under_repo(path: str) -> bool:
    if not path or path.startswith("/") or path.startswith("~"):
        return False
    p = PurePosixPath(path)
    parts = p.parts
    if not parts:
        return False
    if parts[0] != "repo":
        return False
    if ".." in parts:
        return False
    return True


def glob_blocked(path: str, blocked_globs: list[str]) -> bool:
    for g in blocked_globs or []:
        if fnmatch.fnmatch(path, g):
            return True
    return False


def validate_repo_path(
    path: str,
    allowed_paths: list[str],
    blocked_globs: list[str],
) -> bool:
    if not is_under_repo(path):
        return False
    if glob_blocked(path, blocked_globs):
        return False
    return any(
        fnmatch.fnmatch(path, ap)
        for ap in (allowed_paths or [])
    )
