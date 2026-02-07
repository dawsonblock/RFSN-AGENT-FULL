"""Unified-diff patch application via ``git apply``.

Includes multiple fallback strategies for LLM-generated patches
that may have wrong line numbers, context mismatches, or formatting
issues.
"""
from __future__ import annotations

import os
import re
import tempfile

from .util import run_cmd


# ---------------------------------------------------------------------------
# Line-number re-anchoring
# ---------------------------------------------------------------------------

_HUNK_RE = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$"
)


def _reanchor_patch(patch_text: str, workdir: str) -> str:
    """Rewrite hunk line-numbers by searching the actual file for context.

    LLMs frequently hallucinate line numbers.  For each hunk we extract
    the first few *context* lines (lines starting with ``' '``) and find
    their true offset in the file, then rewrite the ``@@`` header.
    """
    lines = patch_text.splitlines(keepends=True)
    # Collect file targets  ("+++ b/path")
    cur_file: str | None = None
    file_lines_cache: dict[str, list[str]] = {}
    result: list[str] = []

    for line in lines:
        stripped = line.rstrip("\n\r")

        if stripped.startswith("+++ b/"):
            cur_file = stripped[6:]
            result.append(line)
            continue

        if stripped.startswith("--- a/"):
            result.append(line)
            continue

        m = _HUNK_RE.match(stripped)
        if m and cur_file:
            # Collect context lines from this hunk
            ctx_lines: list[str] = []
            hunk_body: list[str] = []
            idx = lines.index(line)
            for body_line in lines[idx + 1:]:
                bs = body_line.rstrip("\n\r")
                if _HUNK_RE.match(bs) or bs.startswith("diff --git") or bs.startswith("--- a/") or bs.startswith("+++ b/"):
                    break
                hunk_body.append(body_line)
                if bs.startswith(" "):
                    ctx_lines.append(bs[1:])  # strip leading space

            if ctx_lines and len(ctx_lines) >= 1:
                # Load the real file
                if cur_file not in file_lines_cache:
                    real_path = os.path.join(workdir, cur_file)
                    if os.path.isfile(real_path):
                        with open(real_path, "r", errors="replace") as f:
                            file_lines_cache[cur_file] = f.readlines()
                    else:
                        file_lines_cache[cur_file] = []

                real_file = file_lines_cache[cur_file]
                # Search for the first context line in the real file
                needle = ctx_lines[0].rstrip()
                old_start = int(m.group(1))
                found_at = None
                for ri, rl in enumerate(real_file):
                    if rl.rstrip() == needle:
                        # Verify next context lines match too
                        ok = True
                        for ci, cl in enumerate(ctx_lines[1:4], 1):
                            if ri + ci < len(real_file):
                                if real_file[ri + ci].rstrip() != cl.rstrip():
                                    ok = False
                                    break
                            else:
                                ok = False
                                break
                        if ok:
                            found_at = ri + 1  # 1-based
                            break

                if found_at is not None and found_at != old_start:
                    old_cnt = m.group(2) or "1"
                    new_cnt = m.group(4) or "1"
                    tail = m.group(5)
                    new_start = found_at
                    # Adjust new-side start by same delta
                    delta = new_start - old_start
                    orig_new_start = int(m.group(3))
                    adj_new_start = orig_new_start + delta
                    line = f"@@ -{new_start},{old_cnt} +{adj_new_start},{new_cnt} @@{tail}\n"

        result.append(line)

    return "".join(result)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def apply_unified_diff(
    patch_text: str,
    workdir: str,
    *,
    strict: bool = False,
) -> None:
    """Write *patch_text* to a temp file and apply with escalating strategies.

    When *strict* is False (the default for agent proposals), tries
    progressively more lenient strategies:
    1. ``git apply`` (strict)
    2. ``git apply`` with ``-C1`` (reduced context)
    3. ``git apply -C0`` (zero context required)
    4. ``git apply --3way`` (uses index for fuzzy matching)
    5. Re-anchor line numbers then retry ``git apply -C1``
    6. ``patch -p1 --fuzz=3`` (Unix patch with fuzz)

    When *strict* is True (used for SWE-bench test_patch), uses strict
    mode only and fails immediately on error.
    """
    if not patch_text.strip():
        return

    def _write_tmp(text: str) -> str:
        t = tempfile.NamedTemporaryFile(delete=False, suffix=".patch")
        t.write(text.encode("utf-8", errors="replace"))
        t.close()
        return t.name

    tmp_path = _write_tmp(patch_text)
    tmp_paths = [tmp_path]  # track all temp files for cleanup

    try:
        strategies: list[list[str]] = [
            ["git", "apply", "--whitespace=fix", tmp_path],
        ]
        if not strict:
            strategies.extend([
                ["git", "apply", "-C1", "--whitespace=fix", tmp_path],
                ["git", "apply", "-C0", "--whitespace=fix", tmp_path],
                ["git", "apply", "--3way", "--whitespace=fix", tmp_path],
            ])

        last_err = ""
        last_out = ""
        for cmd in strategies:
            code, out, err, _ = run_cmd(cmd, cwd=workdir, timeout_sec=120)
            if code == 0:
                _cleanup_rej_files(workdir)
                return
            last_err = err
            last_out = out
            run_cmd(["git", "checkout", "--", "."], cwd=workdir, timeout_sec=60)

        if not strict:
            # Strategy 5: re-anchor line numbers and retry
            reanchored = _reanchor_patch(patch_text, workdir)
            if reanchored != patch_text:
                ra_path = _write_tmp(reanchored)
                tmp_paths.append(ra_path)
                for cmd in [
                    ["git", "apply", "--whitespace=fix", ra_path],
                    ["git", "apply", "-C1", "--whitespace=fix", ra_path],
                    ["git", "apply", "-C0", "--whitespace=fix", ra_path],
                ]:
                    code, out, err, _ = run_cmd(cmd, cwd=workdir, timeout_sec=120)
                    if code == 0:
                        _cleanup_rej_files(workdir)
                        return
                    last_err = err
                    last_out = out
                    run_cmd(["git", "checkout", "--", "."], cwd=workdir, timeout_sec=60)

            # Strategy 6: fall back to Unix `patch` with fuzz
            code, out, err, _ = run_cmd(
                f"patch -p1 --fuzz=3 --no-backup-if-mismatch < {tmp_path}",
                cwd=workdir, timeout_sec=120, shell=True,
            )
            if code == 0:
                _cleanup_rej_files(workdir)
                return
            last_err = err or last_err
            last_out = out or last_out
            run_cmd(["git", "checkout", "--", "."], cwd=workdir, timeout_sec=60)

        raise RuntimeError(f"git apply failed:\n{last_err}\n{last_out}")
    finally:
        for p in tmp_paths:
            try:
                os.unlink(p)
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
