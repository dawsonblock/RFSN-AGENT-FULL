"""Physical Deterministic Replay — Phase 7.4

Captures and seals the full execution environment so that replays
are physically deterministic, not just logically deterministic.

This module captures:
  - Environment variables (sanitized)
  - Python/package versions
  - Random seeds
  - Filesystem snapshot hashes
  - Platform metadata (OS, arch, locale)
  - Clock source

This enables:
  - Exact run reconstruction
  - Drift detection between environments
  - Root-cause isolation for nondeterministic failures
"""

from __future__ import annotations

import hashlib
import json
import locale
import os
import platform
import random
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional


# ── Environment Snapshot ──────────────────────────────────────────────


@dataclass
class EnvironmentSnapshot:
    """Complete snapshot of the execution environment."""

    # Platform
    python_version: str = ""
    platform_system: str = ""
    platform_machine: str = ""
    platform_release: str = ""
    locale_setting: str = ""

    # Seeds
    random_seed: Optional[int] = None
    pythonhashseed: str = ""

    # Timestamps
    capture_time: float = 0.0
    monotonic_time: float = 0.0

    # Filesystem
    workspace_hash: str = ""
    file_manifest: Dict[str, str] = field(default_factory=dict)  # path -> sha256

    # Dependencies
    installed_packages: Dict[str, str] = field(default_factory=dict)  # pkg -> version

    # Environment variables (sanitized — no secrets)
    env_vars: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def fingerprint(self) -> str:
        """Stable fingerprint of the entire environment."""
        canonical = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:24]


# ── Sanitization ─────────────────────────────────────────────────────

_SECRET_PATTERNS = {"KEY", "SECRET", "TOKEN", "PASSWORD", "CREDENTIAL", "AUTH"}


def _sanitize_env(env: Dict[str, str]) -> Dict[str, str]:
    """Remove secrets from environment variables."""
    sanitized = {}
    for k, v in env.items():
        if any(pat in k.upper() for pat in _SECRET_PATTERNS):
            sanitized[k] = "[REDACTED]"
        else:
            sanitized[k] = v
    return sanitized


# ── Filesystem Hashing ───────────────────────────────────────────────

_SKIP_DIRS = {
    ".git",
    "__pycache__",
    "node_modules",
    ".tox",
    ".mypy_cache",
    "venv",
    ".venv",
}
_MAX_FILES = 5000
_MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB


def _hash_file(path: Path) -> str:
    """SHA-256 of a file's content."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while chunk := f.read(8192):
                h.update(chunk)
    except (OSError, PermissionError):
        return "ERROR"
    return h.hexdigest()


def _hash_workspace(root: str) -> tuple[str, Dict[str, str]]:
    """Hash all files in the workspace.

    Returns (aggregate_hash, {relative_path: file_hash}).
    """
    root_path = Path(root)
    manifest: Dict[str, str] = {}
    count = 0

    for path in sorted(root_path.rglob("*")):
        if count >= _MAX_FILES:
            break
        if any(skip in path.parts for skip in _SKIP_DIRS):
            continue
        if not path.is_file():
            continue
        if path.stat().st_size > _MAX_FILE_SIZE:
            continue

        rel = str(path.relative_to(root_path))
        manifest[rel] = _hash_file(path)
        count += 1

    # Aggregate hash of all file hashes
    combined = json.dumps(manifest, sort_keys=True)
    agg_hash = hashlib.sha256(combined.encode()).hexdigest()

    return agg_hash, manifest


# ── Package Introspection ─────────────────────────────────────────────


def _get_installed_packages() -> Dict[str, str]:
    """Get installed Python packages and versions."""
    try:
        from importlib.metadata import distributions

        return {
            d.metadata["Name"]: d.metadata["Version"]
            for d in distributions()
            if d.metadata["Name"]
        }
    except Exception:
        return {}


# ── Capture ───────────────────────────────────────────────────────────


def capture_environment(
    workspace: str = ".",
    seed: Optional[int] = None,
    include_packages: bool = True,
    include_env: bool = True,
    include_files: bool = True,
) -> EnvironmentSnapshot:
    """Capture a complete environment snapshot.

    Args:
        workspace: Root directory to hash.
        seed: Random seed to set and record. If None, current state is captured.
        include_packages: Whether to capture installed packages.
        include_env: Whether to capture environment variables.
        include_files: Whether to hash workspace files.

    Returns:
        An EnvironmentSnapshot with all captured data.
    """
    snap = EnvironmentSnapshot()

    # Platform
    snap.python_version = sys.version
    snap.platform_system = platform.system()
    snap.platform_machine = platform.machine()
    snap.platform_release = platform.release()
    try:
        snap.locale_setting = locale.getlocale()[0] or "unknown"
    except Exception:
        snap.locale_setting = "unknown"

    # Seeds
    if seed is not None:
        random.seed(seed)
        snap.random_seed = seed
    snap.pythonhashseed = os.environ.get("PYTHONHASHSEED", "random")

    # Timestamps
    snap.capture_time = time.time()
    snap.monotonic_time = time.monotonic()

    # Filesystem
    if include_files and os.path.isdir(workspace):
        snap.workspace_hash, snap.file_manifest = _hash_workspace(workspace)

    # Packages
    if include_packages:
        snap.installed_packages = _get_installed_packages()

    # Environment
    if include_env:
        snap.env_vars = _sanitize_env(dict(os.environ))

    return snap


# ── Comparison ────────────────────────────────────────────────────────


@dataclass
class DriftReport:
    """Report of differences between two environment snapshots."""

    is_identical: bool = True
    python_version_match: bool = True
    platform_match: bool = True
    seed_match: bool = True
    workspace_match: bool = True
    package_diffs: List[str] = field(default_factory=list)
    file_diffs: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def compare_environments(a: EnvironmentSnapshot, b: EnvironmentSnapshot) -> DriftReport:
    """Compare two environment snapshots and produce a drift report."""
    report = DriftReport()

    if a.python_version != b.python_version:
        report.python_version_match = False
        report.is_identical = False

    if (
        a.platform_system != b.platform_system
        or a.platform_machine != b.platform_machine
    ):
        report.platform_match = False
        report.is_identical = False

    if a.random_seed != b.random_seed:
        report.seed_match = False
        report.is_identical = False

    if a.workspace_hash != b.workspace_hash:
        report.workspace_match = False
        report.is_identical = False

        # Detail file diffs
        all_files = set(a.file_manifest.keys()) | set(b.file_manifest.keys())
        for f in sorted(all_files):
            ha = a.file_manifest.get(f, "<missing>")
            hb = b.file_manifest.get(f, "<missing>")
            if ha != hb:
                report.file_diffs.append(f"  {f}: {ha[:12]}... → {hb[:12]}...")

    # Package diffs
    all_pkgs = set(a.installed_packages.keys()) | set(b.installed_packages.keys())
    for pkg in sorted(all_pkgs):
        va = a.installed_packages.get(pkg, "<missing>")
        vb = b.installed_packages.get(pkg, "<missing>")
        if va != vb:
            report.package_diffs.append(f"  {pkg}: {va} → {vb}")
            report.is_identical = False

    return report
