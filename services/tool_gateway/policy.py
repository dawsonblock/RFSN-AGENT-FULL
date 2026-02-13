from __future__ import annotations

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


def path_blocked_by_prefix_suffix(
    path: str,
    blocked_prefixes: list[str] | tuple[str, ...] | None = None,
    blocked_suffixes: list[str] | tuple[str, ...] | None = None,
) -> bool:
    norm = path.replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    norm = norm.lstrip("/")
    for pref in blocked_prefixes or []:
        p = str(pref or "").replace("\\", "/")
        while p.startswith("./"):
            p = p[2:]
        p = p.lstrip("/")
        if not p:
            continue
        if norm.startswith(p):
            return True
    for suff in blocked_suffixes or []:
        s = str(suff or "")
        if s and norm.endswith(s):
            return True
    return False


def extract_patch_touched_paths(patch_text: str) -> set[str]:
    touched: set[str] = set()
    for raw in (patch_text or "").splitlines():
        line = raw.strip()
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                for part in (parts[2], parts[3]):
                    p = part
                    if p.startswith("a/") or p.startswith("b/"):
                        p = p[2:]
                    if p and p != "/dev/null":
                        touched.add(p)
            continue
        if line.startswith("+++ ") or line.startswith("--- "):
            parts = line.split(maxsplit=1)
            if len(parts) != 2:
                continue
            p = parts[1].strip()
            if p in {"a/dev/null", "b/dev/null", "/dev/null"}:
                continue
            if p.startswith("a/") or p.startswith("b/"):
                p = p[2:]
            if p:
                touched.add(p)
    return touched


def validate_repo_path(
    path: str,
    allowed_paths: list[str],
    blocked_globs: list[str],
    *,
    repo_root_required: bool = True,
    repo_root: str = "",
    blocked_prefixes: list[str] | tuple[str, ...] | None = None,
    blocked_suffixes: list[str] | tuple[str, ...] | None = None,
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
    if path_blocked_by_prefix_suffix(
        path,
        blocked_prefixes=blocked_prefixes,
        blocked_suffixes=blocked_suffixes,
    ):
        return False
    return any(
        fnmatch.fnmatch(path, ap)
        for ap in (allowed_paths or [])
    )
