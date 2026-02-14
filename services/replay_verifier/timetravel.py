"""Time-travel debugging for replay chains.

Allows rewinding to any checkpoint in a sequence of replay bundles,
inspecting the manifest state at that point, and comparing against
the current state.
"""

import json
import os
from typing import Any, Dict, List, Optional


def list_checkpoints(replay_root: str) -> List[Dict[str, Any]]:
    """List all replay bundles as checkpoints, sorted by creation time.

    Returns:
        list of dicts with keys: index, name, path, timestamp
    """
    if not os.path.isdir(replay_root):
        return []

    entries = []
    for name in sorted(os.listdir(replay_root)):
        p = os.path.join(replay_root, name)
        if not os.path.isdir(p):
            continue
        manifest_path = os.path.join(p, "manifest.json")
        ts = None
        if os.path.exists(manifest_path):
            try:
                with open(manifest_path) as f:
                    m = json.load(f)
                ts = m.get("timestamp") or m.get("start_time")
            except Exception:
                pass
        if ts is None:
            ts = os.path.getmtime(p)
        entries.append({"name": name, "path": p, "timestamp": ts})

    # Sort by timestamp
    entries.sort(key=lambda e: e["timestamp"] or 0)
    for i, e in enumerate(entries):
        e["index"] = i

    return entries


def rewind_to(
    replay_root: str,
    checkpoint_index: int,
) -> Optional[Dict[str, Any]]:
    """Load the manifest at a specific checkpoint index.

    Args:
        replay_root: Root replay directory.
        checkpoint_index: 0-based index into the sorted checkpoint list.

    Returns:
        The manifest dict at that checkpoint, or None if not found.
    """
    checkpoints = list_checkpoints(replay_root)
    if checkpoint_index < 0 or checkpoint_index >= len(checkpoints):
        return None

    cp = checkpoints[checkpoint_index]
    manifest_path = os.path.join(cp["path"], "manifest.json")
    if not os.path.exists(manifest_path):
        return None

    with open(manifest_path) as f:
        return json.load(f)


def compare_checkpoints(
    replay_root: str,
    index_a: int,
    index_b: int,
) -> Dict[str, Any]:
    """Compare manifests at two checkpoint indices.

    Returns:
        dict with keys: a_index, b_index, diffs (field → {old, new})
    """
    a = rewind_to(replay_root, index_a)
    b = rewind_to(replay_root, index_b)

    if a is None or b is None:
        return {
            "error": f"Checkpoint not found (a={index_a}, b={index_b})",
        }

    all_keys = set(a.keys()) | set(b.keys())
    diffs = {}
    for key in sorted(all_keys):
        va = a.get(key)
        vb = b.get(key)
        if va != vb:
            diffs[key] = {"old": va, "new": vb}

    return {
        "a_index": index_a,
        "b_index": index_b,
        "diff_count": len(diffs),
        "diffs": diffs,
    }


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage:")
        print("  list    <replay_root>")
        print("  rewind  <replay_root> <index>")
        print("  compare <replay_root> <index_a> <index_b>")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "list" and len(sys.argv) == 3:
        cps = list_checkpoints(sys.argv[2])
        for cp in cps:
            print(f"  [{cp['index']}] {cp['name']}  ts={cp['timestamp']}")
    elif cmd == "rewind" and len(sys.argv) == 4:
        m = rewind_to(sys.argv[2], int(sys.argv[3]))
        print(json.dumps(m, indent=2) if m else "Not found")
    elif cmd == "compare" and len(sys.argv) == 5:
        result = compare_checkpoints(sys.argv[2], int(sys.argv[3]), int(sys.argv[4]))
        print(json.dumps(result, indent=2))
    else:
        print("Invalid usage")
        sys.exit(1)
