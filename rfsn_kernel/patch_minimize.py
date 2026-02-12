from __future__ import annotations

from typing import List


def minimize_unified_diff(diff_text: str) -> str:
    """Deterministically trim empty/context-only hunks from a unified diff.

    This is conservative: it never rewrites changed lines, only drops context
    around hunks that have no +/- body.
    """
    if not diff_text:
        return ""

    lines = diff_text.splitlines()
    out: List[str] = []
    hunk: List[str] = []
    hunk_has_change = False

    def flush_hunk() -> None:
        nonlocal hunk, hunk_has_change
        if hunk and hunk_has_change:
            out.extend(hunk)
        hunk = []
        hunk_has_change = False

    for line in lines:
        if line.startswith("@@"):
            flush_hunk()
            hunk = [line]
            hunk_has_change = False
            continue

        if hunk:
            if line.startswith("+") and not line.startswith("+++"):
                hunk_has_change = True
            elif line.startswith("-") and not line.startswith("---"):
                hunk_has_change = True
            hunk.append(line)
            continue

        out.append(line)

    flush_hunk()

    minimized = "\n".join(out)
    if diff_text.endswith("\n"):
        minimized += "\n"
    return minimized
