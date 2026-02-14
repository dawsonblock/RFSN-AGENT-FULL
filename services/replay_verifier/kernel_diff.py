"""Diff two kernel trace JSON files.

Kernel traces are lists of decision records produced by the RFSN
Hard Kernel during a run.  This module reports added, removed, and
changed decisions between an old and new trace.
"""

import json
from typing import Any, Dict, List, Tuple


def _decision_key(record: Dict[str, Any]) -> str:
    """Build a stable key for a decision record."""
    return f"{record.get('step', '?')}:{record.get('action', '?')}:{record.get('gate', '?')}"


def diff_kernels(
    old_trace: List[Dict[str, Any]],
    new_trace: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Diff two kernel decision traces.

    Args:
        old_trace: List of decision records from baseline run.
        new_trace: List of decision records from current run.

    Returns:
        dict with keys: added, removed, changed, unchanged_count, summary
    """
    old_by_key = {_decision_key(r): r for r in old_trace}
    new_by_key = {_decision_key(r): r for r in new_trace}

    old_keys = set(old_by_key.keys())
    new_keys = set(new_by_key.keys())

    added = sorted(new_keys - old_keys)
    removed = sorted(old_keys - new_keys)
    common = old_keys & new_keys

    changed = []
    unchanged = 0
    for key in sorted(common):
        old_rec = old_by_key[key]
        new_rec = new_by_key[key]
        if old_rec != new_rec:
            diffs = {}
            all_fields = set(old_rec.keys()) | set(new_rec.keys())
            for field in all_fields:
                ov = old_rec.get(field)
                nv = new_rec.get(field)
                if ov != nv:
                    diffs[field] = {"old": ov, "new": nv}
            changed.append({"key": key, "diffs": diffs})
        else:
            unchanged += 1

    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "unchanged_count": unchanged,
        "summary": (
            f"{len(added)} added, {len(removed)} removed, "
            f"{len(changed)} changed, {unchanged} unchanged"
        ),
    }


def diff_kernel_files(old_path: str, new_path: str) -> Dict[str, Any]:
    """Diff two kernel trace JSON files on disk."""
    with open(old_path) as f:
        old = json.load(f)
    with open(new_path) as f:
        new = json.load(f)

    # Handle both list-of-records and {trace: [...]} formats
    if isinstance(old, dict):
        old = old.get("trace", old.get("decisions", []))
    if isinstance(new, dict):
        new = new.get("trace", new.get("decisions", []))

    return diff_kernels(old, new)


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 3:
        print(
            "Usage: python -m services.replay_verifier.kernel_diff <old.json> <new.json>"
        )
        sys.exit(1)

    result = diff_kernel_files(sys.argv[1], sys.argv[2])
    print(json.dumps(result, indent=2))
