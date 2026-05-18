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


def _get_repo_head(repo_path: str) -> str:
    """Return the current HEAD commit hash for a repo path.

    Falls back to parsing .git/packed-refs if git binary is unavailable.
    """
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "rev-parse", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    # Fallback without git binary: read .git/HEAD and resolve ref.
    git_dir = os.path.join(repo_path, ".git")
    try:
        with open(os.path.join(git_dir, "HEAD"), "r") as fh:
            ref_line = fh.read().strip()
        if ref_line.startswith("ref: "):
            ref_path = ref_line[5:]  # e.g. refs/heads/main
            full_ref_path = os.path.join(git_dir, ref_path)
            if os.path.isfile(full_ref_path):
                with open(full_ref_path) as fh:
                    return fh.read().strip()
            # Try packed-refs
            packed = os.path.join(git_dir, "packed-refs")
            if os.path.isfile(packed):
                with open(packed) as fh:
                    for line in fh:
                        parts = line.split()
                        if len(parts) == 2 and parts[1] == ref_path:
                            return parts[0]
        return ref_line  # detached HEAD — return SHA directly
    except Exception:
        return ""


def _capture_repo_snapshot(repo_id: str, run_id: str, label: str) -> dict:
    """Capture a lightweight repo snapshot record (path + head SHA)."""
    repo_path = _repo_abs_path(repo_id)
    head = _get_repo_head(repo_path)
    return {
        "label": label,
        "repo_id": repo_id,
        "head": head,
        "timestamp": time.time(),
    }


def _capture_requirements_lock(repo_id: str, run_id: str) -> dict:
    """Capture a hash of the requirements lockfile (if present)."""
    repo_path = _repo_abs_path(repo_id)
    for name in ("requirements.txt", "requirements-lock.txt", "Pipfile.lock", "poetry.lock"):
        path = os.path.join(repo_path, name)
        if os.path.isfile(path):
            sha = _file_sha256(path)
            return {"file": name, "sha256": sha, "timestamp": time.time()}
    return {"file": None, "sha256": None, "timestamp": time.time()}


def _capture_executor_env_manifest(repo_id: str, run_id: str) -> dict:
    """Capture executor environment manifest path placeholder."""
    bundle_dir = _replay_bundle_dir(repo_id, run_id)
    executor_env_manifest_path = os.path.join(bundle_dir, "executor_env.json")
    return {
        "executor_env_manifest_path": executor_env_manifest_path,
        "timestamp": time.time(),
    }


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
    repo_snapshot_start = _capture_repo_snapshot(repo_id, run_id, "start")
    requirements_lock = _capture_requirements_lock(repo_id, run_id)
    executor_env = _capture_executor_env_manifest(repo_id, run_id)
    executor_env_manifest_path = executor_env.get("executor_env_manifest_path", "")

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
        "events": [],
        "repo_snapshot_start": repo_snapshot_start,
        "repo_snapshot_end": None,  # populated by finalize_replay_manifest
        "requirements_lock": requirements_lock,
        "executor_env_manifest_path": executor_env_manifest_path,
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
    manifest["repo_snapshot_end"] = _capture_repo_snapshot(repo_id, run_id, "end")

    bundle_dir = _replay_bundle_dir(repo_id, run_id)
    manifest_path = os.path.join(bundle_dir, "manifest.json")

    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    return manifest_path
