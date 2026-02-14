"""Drift detection between replay manifest snapshots.

Compares two manifest dicts (or JSON files) and produces a drift
score in [0.0, 1.0] where 0.0 = identical and 1.0 = completely divergent.
"""

import json
import os
from typing import Any, Dict, Optional, Tuple


_TRACKED_KEYS = [
    "deps",
    "env",
    "patch_hash",
    "kernel_trace_hash",
    "artifact_hash",
    "deps_state",
    "policy_hash",
]


def _deep_equal(a: Any, b: Any) -> bool:
    """Deep equality check that handles None gracefully."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if isinstance(a, dict) and isinstance(b, dict):
        keys = set(a.keys()) | set(b.keys())
        return all(_deep_equal(a.get(k), b.get(k)) for k in keys)
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return False
        return all(_deep_equal(x, y) for x, y in zip(a, b))
    return a == b


def compute_drift(
    old: Dict[str, Any],
    new: Dict[str, Any],
    tracked_keys: Optional[list] = None,
) -> Tuple[float, Dict[str, bool]]:
    """Compute drift score between two manifest snapshots.

    Returns:
        (drift_score, per_key_mismatch_map)

    drift_score is the fraction of tracked keys that differ [0.0–1.0].
    per_key_mismatch_map maps each key to True if it drifted.
    """
    keys = tracked_keys or _TRACKED_KEYS
    mismatches: Dict[str, bool] = {}
    drifted = 0

    for key in keys:
        old_val = old.get(key)
        new_val = new.get(key)
        if not _deep_equal(old_val, new_val):
            mismatches[key] = True
            drifted += 1
        else:
            mismatches[key] = False

    score = drifted / max(len(keys), 1)
    return score, mismatches


def check_drift(
    old_path: str,
    new_path: str,
    threshold: float = 0.0,
) -> Dict[str, Any]:
    """Compare two manifest JSON files and return a drift report.

    Args:
        old_path: Path to the baseline manifest.json
        new_path: Path to the current manifest.json
        threshold: Maximum acceptable drift score (0.0 = exact match)

    Returns:
        dict with keys: ok, score, mismatches, exceeded_threshold
    """
    with open(old_path) as f:
        old = json.load(f)
    with open(new_path) as f:
        new = json.load(f)

    score, mismatches = compute_drift(old, new)
    exceeded = score > threshold

    return {
        "ok": not exceeded,
        "score": round(score, 4),
        "mismatches": {k: v for k, v in mismatches.items() if v},
        "exceeded_threshold": exceeded,
        "threshold": threshold,
    }


def check_drift_from_dicts(
    old: Dict[str, Any],
    new: Dict[str, Any],
    threshold: float = 0.0,
) -> Dict[str, Any]:
    """Same as check_drift but accepts dicts directly."""
    score, mismatches = compute_drift(old, new)
    exceeded = score > threshold
    return {
        "ok": not exceeded,
        "score": round(score, 4),
        "mismatches": {k: v for k, v in mismatches.items() if v},
        "exceeded_threshold": exceeded,
        "threshold": threshold,
    }


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 3:
        print("Usage: python -m services.replay_verifier.drift <old.json> <new.json>")
        sys.exit(1)
    result = check_drift(sys.argv[1], sys.argv[2])
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["ok"] else 1)
