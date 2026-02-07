"""Git repo management: clone, checkout, reset, diff."""
from __future__ import annotations

import os
from typing import Optional

from .util import run_cmd


def ensure_clean_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def git_clone(repo_url: str, workdir: str) -> None:
    ensure_clean_dir(workdir)
    if os.path.isdir(os.path.join(workdir, ".git")):
        return
    # If workdir exists but isn't a git repo, clear it out first so
    # ``git clone ... .`` doesn't fail with "directory not empty".
    if os.listdir(workdir):
        import shutil
        shutil.rmtree(workdir)
        os.makedirs(workdir, exist_ok=True)
    code, out, err, _ = run_cmd(
        ["git", "clone", repo_url, "."], cwd=workdir, timeout_sec=600
    )
    if code != 0:
        raise RuntimeError(f"git clone failed: {err}\n{out}")


def git_checkout(workdir: str, ref: Optional[str]) -> None:
    if not ref:
        return
    # Validate ref: only allow safe branch/tag names (no shell metacharacters)
    import re as _re
    if not _re.fullmatch(r"[A-Za-z0-9_./-]{1,256}", ref):
        raise RuntimeError(
            "git checkout: ref contains disallowed"
            f" characters: {ref!r}"
        )
    code, out, err, _ = run_cmd(
        ["git", "checkout", ref], cwd=workdir, timeout_sec=120
    )
    if code != 0:
        raise RuntimeError(f"git checkout failed: {err}\n{out}")


def git_status_porcelain(workdir: str) -> str:
    code, out, err, _ = run_cmd(
        ["git", "status", "--porcelain"], cwd=workdir, timeout_sec=60
    )
    if code != 0:
        raise RuntimeError(f"git status failed: {err}\n{out}")
    return out


def git_diff_unified(
    workdir: str,
    *,
    exclude_paths: list[str] | None = None,
) -> str:
    cmd = ["git", "diff"]
    if exclude_paths:
        # Use pathspec exclude magic to omit test_patch files from the diff
        cmd.append("--")
        cmd.append(".")
        for p in exclude_paths:
            cmd.append(f":!{p}")
    code, out, err, _ = run_cmd(cmd, cwd=workdir, timeout_sec=60)
    if code != 0:
        raise RuntimeError(f"git diff failed: {err}\n{out}")
    return out


def git_reset_hard(workdir: str) -> None:
    run_cmd(["git", "reset", "--hard"], cwd=workdir, timeout_sec=120)
    # Exclude replays/ — the replay event log lives inside the workdir
    # and must survive across iterations.
    run_cmd(
        ["git", "clean", "-fd", "--exclude=replays"],
        cwd=workdir, timeout_sec=120,
    )
