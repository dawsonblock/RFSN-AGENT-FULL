"""Cluster replay bundles by similarity.

Groups completed replay bundles by patch_hash and outcome so that
common patterns (e.g., same fix applied to different runs) are visible.
"""

import json
import os
from collections import defaultdict
from typing import Any, Dict, List, Optional


def _load_manifest(bundle_dir: str) -> Optional[Dict[str, Any]]:
    """Load manifest.json from a replay bundle directory."""
    p = os.path.join(bundle_dir, "manifest.json")
    if not os.path.exists(p):
        return None
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return None


def _cluster_key(manifest: Dict[str, Any]) -> str:
    """Build a clustering key from a manifest."""
    patch = manifest.get("patch_hash", "none")
    outcome = manifest.get("outcome", "unknown")
    return f"{patch}:{outcome}"


def cluster_bundles(replay_root: str) -> Dict[str, List[str]]:
    """Group replay bundles by (patch_hash, outcome).

    Args:
        replay_root: Root directory containing subdirectories per run.

    Returns:
        dict mapping cluster_key → list of bundle directory names.
    """
    clusters: Dict[str, List[str]] = defaultdict(list)

    if not os.path.isdir(replay_root):
        return dict(clusters)

    for entry in sorted(os.listdir(replay_root)):
        bundle_path = os.path.join(replay_root, entry)
        if not os.path.isdir(bundle_path):
            continue
        manifest = _load_manifest(bundle_path)
        if manifest is None:
            clusters["_no_manifest"].append(entry)
            continue
        key = _cluster_key(manifest)
        clusters[key].append(entry)

    return dict(clusters)


def summarize_clusters(clusters: Dict[str, List[str]]) -> List[Dict[str, Any]]:
    """Produce a summary list of clusters sorted by size (descending).

    Returns:
        list of dicts with keys: key, count, bundles
    """
    items = []
    for key, bundles in clusters.items():
        parts = key.split(":", 1)
        items.append(
            {
                "key": key,
                "patch_hash": parts[0] if len(parts) > 0 else "?",
                "outcome": parts[1] if len(parts) > 1 else "?",
                "count": len(bundles),
                "bundles": bundles,
            }
        )
    items.sort(key=lambda x: x["count"], reverse=True)
    return items


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("Usage: python -m services.replay_verifier.cluster <replay_root>")
        sys.exit(1)

    clusters = cluster_bundles(sys.argv[1])
    summary = summarize_clusters(clusters)
    print(json.dumps(summary, indent=2))
