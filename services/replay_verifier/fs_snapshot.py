"""Filesystem snapshot capture and comparison.

Captures a directory tree with per-file content hashes, then compares
two snapshots to find added/removed/modified files.
"""

import hashlib
import json
import os
from typing import Any, Dict, List, Optional, Set


def _hash_file(path: str, max_bytes: int = 50_000_000) -> Optional[str]:
    """SHA-256 hash of a file's contents. Returns None if too large or unreadable."""
    try:
        if os.path.getsize(path) > max_bytes:
            return None
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def capture(
    root: str,
    ignore: Optional[Set[str]] = None,
    max_file_bytes: int = 50_000_000,
) -> Dict[str, Any]:
    """Capture a snapshot of a directory tree.

    Returns:
        dict mapping relative paths to {"size": int, "hash": str|None}
    """
    ignore = ignore or {".git", "__pycache__", ".venv", "venv", "node_modules"}
    snapshot: Dict[str, Any] = {}

    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in ignore)
        for f in sorted(files):
            full = os.path.join(base, f)
            rel = os.path.relpath(full, root)
            try:
                size = os.path.getsize(full)
                file_hash = _hash_file(full, max_file_bytes)
                snapshot[rel] = {"size": size, "hash": file_hash}
            except Exception:
                snapshot[rel] = {"size": -1, "hash": None}

    return snapshot


def compare(
    old_snap: Dict[str, Any],
    new_snap: Dict[str, Any],
) -> Dict[str, Any]:
    """Compare two snapshots.

    Returns:
        dict with keys: added, removed, modified, unchanged_count, summary
    """
    old_keys = set(old_snap.keys())
    new_keys = set(new_snap.keys())

    added = sorted(new_keys - old_keys)
    removed = sorted(old_keys - new_keys)
    common = old_keys & new_keys

    modified = []
    unchanged = 0
    for f in sorted(common):
        old_h = old_snap[f].get("hash")
        new_h = new_snap[f].get("hash")
        if old_h != new_h:
            modified.append(f)
        else:
            unchanged += 1

    return {
        "added": added,
        "removed": removed,
        "modified": modified,
        "unchanged_count": unchanged,
        "summary": (
            f"{len(added)} added, {len(removed)} removed, "
            f"{len(modified)} modified, {unchanged} unchanged"
        ),
    }


def save(snapshot: Dict[str, Any], path: str) -> None:
    """Save a snapshot to a JSON file."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(snapshot, f, indent=2, sort_keys=True)


def load(path: str) -> Dict[str, Any]:
    """Load a snapshot from a JSON file."""
    with open(path) as f:
        return json.load(f)


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage:")
        print("  capture <dir> <out.json>")
        print("  compare <old.json> <new.json>")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "capture" and len(sys.argv) == 4:
        snap = capture(sys.argv[2])
        save(snap, sys.argv[3])
        print(f"Captured {len(snap)} files")
    elif cmd == "compare" and len(sys.argv) == 4:
        result = compare(load(sys.argv[2]), load(sys.argv[3]))
        print(json.dumps(result, indent=2))
    else:
        print("Invalid arguments")
        sys.exit(1)
