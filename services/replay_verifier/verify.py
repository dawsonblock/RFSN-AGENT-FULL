"""Deterministic Replay Verifier (DRV).

Compares two replay bundles to detect environment, dependency,
or behavioral drift that would break deterministic replay.

Usage:
    python -m services.replay_verifier.verify /path/to/bundleA /path/to/bundleB
"""

import hashlib
import json
import os
import sys


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_tree(root: str, ignore: set | None = None) -> str:
    """Stable hash of a directory tree (filenames + sizes)."""
    ignore = ignore or set()
    h = hashlib.sha256()
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in ignore)
        for f in sorted(files):
            p = os.path.join(base, f)
            try:
                h.update(f.encode())
                h.update(str(os.path.getsize(p)).encode())
            except Exception:
                pass
    return h.hexdigest()


def _load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _compare(a, b, field: str) -> str | None:
    if a != b:
        return f"MISMATCH in {field}: {a!r} != {b!r}"
    return None


def verify(old_bundle: str, new_bundle: str) -> list:
    """Compare two bundles. Returns list of mismatches (empty = OK)."""
    mismatches = []

    # Load manifests.
    old_manifest = _load_json(os.path.join(old_bundle, "manifest.json"))
    new_manifest = _load_json(os.path.join(new_bundle, "manifest.json"))

    # Compare dependency state.
    old_deps = old_manifest.get("deps_state", {})
    new_deps = new_manifest.get("deps_state", {})
    if old_deps and new_deps:
        m = _compare(
            old_deps.get("site_packages_hash"),
            new_deps.get("site_packages_hash"),
            "site_packages_hash",
        )
        if m:
            mismatches.append(m)

    # Compare env.
    old_env = old_manifest.get("executor_env_manifest", {})
    new_env = new_manifest.get("executor_env_manifest", {})
    for key in ("python_version", "blessed_image"):
        m = _compare(old_env.get(key), new_env.get(key), f"env.{key}")
        if m:
            mismatches.append(m)

    # Compare patch hash.
    m = _compare(
        old_manifest.get("patch_hash"),
        new_manifest.get("patch_hash"),
        "patch_hash",
    )
    if m:
        mismatches.append(m)

    # Compare kernel trace.
    m = _compare(
        old_manifest.get("kernel_trace"),
        new_manifest.get("kernel_trace"),
        "kernel_trace",
    )
    if m:
        mismatches.append(m)

    # Compare deps_state files if present.
    for fname in ("deps_state.json",):
        old_fp = os.path.join(old_bundle, fname)
        new_fp = os.path.join(new_bundle, fname)
        if os.path.isfile(old_fp) and os.path.isfile(new_fp):
            old_data = _load_json(old_fp)
            new_data = _load_json(new_fp)
            if old_data.get("site_packages_hash") != new_data.get("site_packages_hash"):
                mismatches.append("DEPS_STATE_MISMATCH")

    return mismatches


def main() -> None:
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <old_bundle> <new_bundle>")
        sys.exit(1)

    mismatches = verify(sys.argv[1], sys.argv[2])
    if mismatches:
        for m in mismatches:
            print(f"REPLAY_FAIL: {m}", flush=True)
        sys.exit(3)
    else:
        print("REPLAY_OK", flush=True)


if __name__ == "__main__":
    main()
