"""Cross-validate N replay bundles for determinism consensus.

Given multiple replay bundles (from repeated runs of the same task),
checks that all manifests agree on the key determinism fields.
Reports which bundles deviate from the majority.
"""

import json
import os
from collections import Counter
from typing import Any, Dict, List, Optional


_DETERMINISM_KEYS = [
    "patch_hash",
    "kernel_trace_hash",
    "artifact_hash",
    "deps",
]


def _load_manifest(bundle_dir: str) -> Optional[Dict[str, Any]]:
    """Load manifest.json from a bundle directory."""
    p = os.path.join(bundle_dir, "manifest.json")
    if not os.path.exists(p):
        return None
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return None


def _fingerprint(manifest: Dict[str, Any], keys: List[str]) -> str:
    """Create a determinism fingerprint from selected keys."""
    parts = []
    for k in keys:
        v = manifest.get(k)
        parts.append(json.dumps(v, sort_keys=True, separators=(",", ":")))
    return "|".join(parts)


def verify_mesh(
    bundle_dirs: List[str],
    keys: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Cross-validate multiple replay bundles for determinism.

    Args:
        bundle_dirs: List of replay bundle directory paths.
        keys: Manifest keys to check for agreement.

    Returns:
        dict with keys: ok, total, consensus_size, deviants, consensus_fingerprint
    """
    keys = keys or _DETERMINISM_KEYS
    fingerprints: Dict[str, str] = {}

    for bd in bundle_dirs:
        name = os.path.basename(bd)
        manifest = _load_manifest(bd)
        if manifest is None:
            fingerprints[name] = "__MISSING__"
            continue
        fingerprints[name] = _fingerprint(manifest, keys)

    # Find majority fingerprint
    counts = Counter(fingerprints.values())
    if not counts:
        return {"ok": False, "error": "no bundles", "total": 0}

    majority_fp, majority_count = counts.most_common(1)[0]

    deviants = [name for name, fp in fingerprints.items() if fp != majority_fp]

    return {
        "ok": len(deviants) == 0,
        "total": len(bundle_dirs),
        "consensus_size": majority_count,
        "deviant_count": len(deviants),
        "deviants": deviants,
        "consensus_fingerprint": majority_fp,
        "checked_keys": keys,
    }


def verify_mesh_from_root(
    replay_root: str,
    keys: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Verify all bundles under a root directory."""
    if not os.path.isdir(replay_root):
        return {"ok": False, "error": "directory not found"}

    bundle_dirs = [
        os.path.join(replay_root, d)
        for d in sorted(os.listdir(replay_root))
        if os.path.isdir(os.path.join(replay_root, d))
    ]
    return verify_mesh(bundle_dirs, keys)


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("Usage: python -m services.replay_verifier.mesh <replay_root>")
        sys.exit(1)

    result = verify_mesh_from_root(sys.argv[1])
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["ok"] else 1)
