"""Pre-flight boot verification.

Checks that all critical dependencies and configurations
are present before the agent starts processing tasks.
"""

import importlib
import os
import shutil
from typing import Any, Dict, List


class BootCheck:
    """A single boot verification check."""

    def __init__(self, name: str, ok: bool, message: str = ""):
        self.name = name
        self.ok = ok
        self.message = message

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "message": self.message}


def check_python_imports() -> BootCheck:
    """Verify critical Python modules are importable."""
    required = ["json", "hashlib", "subprocess", "os", "re"]
    missing = []
    for mod in required:
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        return BootCheck("python_imports", False, f"Missing: {missing}")
    return BootCheck("python_imports", True, f"All {len(required)} core modules OK")


def check_rfsn_modules() -> BootCheck:
    """Verify RFSN-specific modules are importable."""
    modules = [
        "rfsn_kernel.kernel",
        "rfsn_kernel.planner",
        "rfsn_kernel.state",
    ]
    missing = []
    for mod in modules:
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        return BootCheck("rfsn_modules", False, f"Missing: {missing}")
    return BootCheck("rfsn_modules", True, f"All {len(modules)} RFSN modules OK")


def check_policies() -> BootCheck:
    """Verify policy files exist."""
    policy_dir = os.getenv("RFSN_POLICY_DIR", "policies")
    if not os.path.isdir(policy_dir):
        return BootCheck("policies", False, f"Policy dir not found: {policy_dir}")
    yaml_files = [f for f in os.listdir(policy_dir) if f.endswith((".yaml", ".yml"))]
    if not yaml_files:
        return BootCheck("policies", False, "No policy YAML files found")
    return BootCheck("policies", True, f"{len(yaml_files)} policy files found")


def check_disk_space() -> BootCheck:
    """Verify sufficient disk space (>500MB free)."""
    try:
        usage = shutil.disk_usage("/")
        free_mb = usage.free / (1024 * 1024)
        if free_mb < 500:
            return BootCheck("disk_space", False, f"Only {free_mb:.0f}MB free")
        return BootCheck("disk_space", True, f"{free_mb:.0f}MB free")
    except Exception as e:
        return BootCheck("disk_space", False, str(e))


def check_docker() -> BootCheck:
    """Verify Docker is available (best-effort)."""
    import subprocess

    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=5,
        )
        if result.returncode == 0:
            return BootCheck("docker", True, "Docker daemon running")
        return BootCheck("docker", False, "Docker not responding")
    except FileNotFoundError:
        return BootCheck("docker", False, "Docker not installed")
    except Exception as e:
        return BootCheck("docker", False, str(e))


def check_env_vars() -> BootCheck:
    """Verify critical environment variables are set."""
    recommended = ["RFSN_AUTH_REQUIRED", "RFSN_PATCH_GATE_REQUIRED"]
    missing = [v for v in recommended if not os.getenv(v)]
    if missing:
        return BootCheck("env_vars", True, f"Unset (using defaults): {missing}")
    return BootCheck("env_vars", True, "All env vars configured")


def run_all() -> Dict[str, Any]:
    """Run all boot checks.

    Returns:
        dict with keys: ok, checks, failed_count
    """
    checks = [
        check_python_imports(),
        check_rfsn_modules(),
        check_policies(),
        check_disk_space(),
        check_docker(),
        check_env_vars(),
    ]

    failed = [c for c in checks if not c.ok]

    return {
        "ok": len(failed) == 0,
        "total": len(checks),
        "passed": len(checks) - len(failed),
        "failed": len(failed),
        "checks": [c.to_dict() for c in checks],
    }


if __name__ == "__main__":
    import json

    result = run_all()
    for c in result["checks"]:
        status = "✓" if c["ok"] else "✗"
        print(f"  {status} {c['name']}: {c['message']}")
    print(
        f"\n{'BOOT_OK' if result['ok'] else 'BOOT_FAIL'}: {result['passed']}/{result['total']} passed"
    )
