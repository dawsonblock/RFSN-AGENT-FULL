"""Replay manifest and snapshot management."""

import os
import json
import hashlib
import tarfile
import time
import subprocess
from pathlib import Path
from typing import Optional

# Configuration
# Use local directory to avoid read-only FS errors in test env
REPLAY_BASE_DIR = os.getenv("RFSN_REPLAY_DIR", os.path.join(os.getcwd(), "data_replay"))
REPLAY_MANIFEST_DIR = os.path.join(REPLAY_BASE_DIR, "manifests")
_MAX_SNAPSHOT_FILE_BYTES = int(os.getenv("RFSN_MAX_SNAPSHOT_FILE_BYTES", "50000000"))
_MAX_SNAPSHOT_BYTES = int(os.getenv("RFSN_MAX_SNAPSHOT_BYTES", "250000000"))
_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

# Ensure directories exist
os.makedirs(REPLAY_MANIFEST_DIR, exist_ok=True)

_SNAPSHOT_EXCLUDE_DIRS = {
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
    ".tox",
    "secrets",
    "credentials",
    "private",
}

_SNAPSHOT_SECRET_PREFIXES = (".env", "id_rsa", "id_ed25519")


def _snapshot_tar_filter(ti: tarfile.TarInfo) -> Optional[tarfile.TarInfo]:
    if ti.name.split("/")[-1] in _SNAPSHOT_EXCLUDE_DIRS:
        return None
    for prefix in _SNAPSHOT_SECRET_PREFIXES:
        if ti.name.split("/")[-1].startswith(prefix):
            return None
    if ti.size > _MAX_SNAPSHOT_FILE_BYTES:
        return None
    return ti


def _file_sha256(path: str) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                h.update(chunk)
        return h.hexdigest()
    except FileNotFoundError:
        return ""


def _replay_bundle_dir(repo_id: str, run_id: str) -> str:
    path = os.path.join(REPLAY_BASE_DIR, "bundles", repo_id, run_id)
    os.makedirs(path, exist_ok=True)
    return path


def _repo_abs_path(repo_id: str) -> str:
    # Basic validation/path resolution logic (simplified from app.py)
    # Assumes local clone dir structure /data/repos/<repo_id>
    return os.path.join("/data/repos", repo_id)


def capture_repo_snapshot(repo_id: str, run_id: str, label: str) -> tuple[str, str]:
    """Capture a tarball snapshot of the repo execution state."""
    bundle_dir = _replay_bundle_dir(repo_id, run_id)
    out_path = os.path.join(bundle_dir, f"{label}.tar.gz")
    src = _repo_abs_path(repo_id)

    if not os.path.isdir(src):
        return "", "repo_not_found"

    try:
        with tarfile.open(out_path, "w:gz") as tar:
            tar.add(src, arcname=".", filter=_snapshot_tar_filter)

        size = os.path.getsize(out_path)
        if size > _MAX_SNAPSHOT_BYTES:
            os.remove(out_path)
            return "", "snapshot_too_large"

        return out_path, ""
    except Exception as e:
        print(f"ERROR: capture_repo_snapshot failed: {e}")
        return "", f"exception: {str(e)}"


def init_replay_manifest(
    *,
    run_id: str,
    repo_id: str,
    task: str,
    scenario: str,
    run_seed: int,
    env_snapshot: dict,
    sandbox_info: Optional[dict] = None,
) -> dict:
    return {
        "run_id": run_id,
        "repo_id": repo_id,
        "task": task,
        "scenario": scenario,
        "timestamp_start": time.time(),
        "seed": run_seed,
        "env_snapshot": env_snapshot,
        "sandbox_info": sandbox_info or {},
        "status": "running",
        "events": [],  # This will be populated by execution log later
    }


def finalize_replay_manifest(
    *,
    run_id: str,
    repo_id: str,
    manifest: dict,
    status: str,
    reason: str = "",
    results_count: int = 0,
):
    manifest["status"] = status
    manifest["timestamp_end"] = time.time()
    manifest["reason"] = reason
    manifest["results_count"] = results_count

    bundle_dir = _replay_bundle_dir(repo_id, run_id)
    manifest_path = os.path.join(bundle_dir, "manifest.json")

    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    return manifest_path
