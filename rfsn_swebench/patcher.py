"""Unified-diff patch application via ``git apply``."""
from __future__ import annotations

import os
import tempfile

from .util import run_cmd


def apply_unified_diff(
    patch_text: str,
    workdir: str,
    *,
    strict: bool = False,
) -> None:
    """Write *patch_text* to a temp file and apply it with ``git apply``.

    When *strict* is False (the default for agent proposals), tries
    progressively more lenient strategies:
    1. ``git apply`` (strict)
    2. ``git apply`` with ``-C1`` (reduced context lines)
    3. ``git apply --3way`` (uses index for fuzzy matching)

    When *strict* is True (used for SWE-bench test_patch), uses strict
    mode only and fails immediately on error.
    """
    if not patch_text.strip():
        return
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".patch")
    try:
        tmp.write(patch_text.encode("utf-8", errors="replace"))
        tmp.close()

        strategies = [
            ["git", "apply", "--whitespace=fix", tmp.name],
        ]
        if not strict:
            strategies.extend([
                ["git", "apply", "-C1", "--whitespace=fix", tmp.name],
                ["git", "apply", "--3way", "--whitespace=fix", tmp.name],
            ])

        last_err = ""
        last_out = ""
        for cmd in strategies:
            code, out, err, _ = run_cmd(cmd, cwd=workdir, timeout_sec=120)
            if code == 0:
                # Clean up any .rej files from previous failed attempts
                _cleanup_rej_files(workdir)
                return
            last_err = err
            last_out = out
            # Reset after failed attempt so next strategy starts clean
            run_cmd(
                ["git", "checkout", "--", "."],
                cwd=workdir, timeout_sec=60,
            )

        raise RuntimeError(f"git apply failed:\n{last_err}\n{last_out}")
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


def _cleanup_rej_files(workdir: str) -> None:
    """Remove any .rej files left by git apply --reject."""
    import glob
    for rej in glob.glob(os.path.join(workdir, "**", "*.rej"), recursive=True):
        try:
            os.unlink(rej)
        except Exception:
            pass
