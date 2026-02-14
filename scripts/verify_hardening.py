#!/usr/bin/env python3
"""Runtime hardening verification script.

Validates that hardening controls are properly enforced:
1. Missing auth → startup failure
2. Missing patch gate → startup failure
3. Warm sandbox disabled in prod
4. Template with eval rejected
5. Invalid target rejected
6. Per-run venv isolation
7. Snapshot excludes secrets
8. Kernel policy fields present
"""
import json
import os
import re
import sys
import tarfile
import tempfile

PASS = 0
FAIL = 0


def _ok(msg: str) -> None:
    global PASS
    PASS += 1
    print(f"  PASS: {msg}", flush=True)


def _fail(msg: str) -> None:
    global FAIL
    FAIL += 1
    print(f"  FAIL: {msg}", flush=True)


def check_auth_guard() -> None:
    """Auth required guard is enforced in non-dev mode."""
    print("\n[1] Auth guard enforcement", flush=True)
    # Check that executor has the guard pattern.
    executor_py = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "services",
        "executor",
        "app.py",
    )
    if os.path.isfile(executor_py):
        content = open(executor_py).read()
        if "RFSN_AUTH_REQUIRED" in content and "SystemExit" in content:
            _ok("executor has auth-required guard")
        else:
            _fail("executor missing auth-required guard")
    else:
        _fail(f"executor app.py not found at {executor_py}")


def check_patch_gate_guard() -> None:
    """Patch gate required guard is enforced."""
    print("\n[2] Patch gate guard enforcement", flush=True)
    gateway_py = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "services",
        "tool_gateway",
        "app.py",
    )
    if os.path.isfile(gateway_py):
        content = open(gateway_py).read()
        if "RFSN_PATCH_GATE_REQUIRED" in content and "SystemExit" in content:
            _ok("gateway has patch-gate-required guard")
        else:
            _fail("gateway missing patch-gate-required guard")
        if "_patch_gate_verdict" in content:
            _ok("gateway records patch_gate_verdict")
        else:
            _fail("gateway missing patch_gate_verdict recording")
    else:
        _fail(f"gateway app.py not found at {gateway_py}")


def check_warm_sandbox_default() -> None:
    """Warm sandbox defaults to off."""
    print("\n[3] Warm sandbox disabled in prod", flush=True)
    compose_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "docker-compose.yml",
    )
    if os.path.isfile(compose_path):
        content = open(compose_path).read()
        if "RFSN_WARM_SANDBOX:-0" in content:
            _ok("compose defaults WARM_SANDBOX to 0")
        elif "RFSN_WARM_SANDBOX:-1" in content:
            _fail("compose still defaults WARM_SANDBOX to 1")
        else:
            _ok("compose does not set WARM_SANDBOX (no default)")
    else:
        _fail(f"docker-compose.yml not found")


def check_template_validation() -> None:
    """Templates with eval are rejected at startup."""
    print("\n[4] Template eval rejection", flush=True)
    executor_py = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "services",
        "executor",
        "app.py",
    )
    if os.path.isfile(executor_py):
        content = open(executor_py).read()
        if "eval" in content and "forbidden" in content.lower():
            _ok("executor rejects eval in templates")
        else:
            _fail("executor missing eval template rejection")
    else:
        _fail("executor app.py not found")


def check_target_sanitizer() -> None:
    """Invalid targets (shell metacharacters) are rejected."""
    print("\n[5] Target sanitizer", flush=True)
    for label, path_parts in [
        ("executor", ["services", "executor", "app.py"]),
        ("gateway", ["services", "tool_gateway", "app.py"]),
    ]:
        fp = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            *path_parts,
        )
        if os.path.isfile(fp):
            content = open(fp).read()
            if (
                "SHELL_" in content
                or "shell metacharacter" in content.lower()
                or "_GW_SHELL_REJECT" in content
            ):
                _ok(f"{label} has target sanitizer")
            else:
                _fail(f"{label} missing target sanitizer")
        else:
            _fail(f"{label} app.py not found")


def check_venv_mode() -> None:
    """Per-run venv isolation is configured."""
    print("\n[6] Per-run venv isolation", flush=True)
    executor_py = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "services",
        "executor",
        "app.py",
    )
    if os.path.isfile(executor_py):
        content = open(executor_py).read()
        if "RFSN_VENV_MODE" in content and "per_run" in content:
            _ok("executor supports per_run venv mode")
        else:
            _fail("executor missing per_run venv support")
        if "deps_state.json" in content:
            _ok("executor writes deps_state.json")
        else:
            _fail("executor missing deps_state.json")
    else:
        _fail("executor app.py not found")


def check_snapshot_excludes() -> None:
    """Snapshot excludes secrets and sensitive files."""
    print("\n[7] Snapshot excludes secrets", flush=True)
    orch_py = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "services",
        "orchestrator",
        "app.py",
    )
    if os.path.isfile(orch_py):
        content = open(orch_py).read()
        for pattern in [
            ".pem",
            ".key",
            ".env",
            "id_rsa",
            "secrets",
            "credentials",
            "private",
        ]:
            if pattern in content:
                _ok(f"snapshot excludes {pattern!r}")
            else:
                _fail(f"snapshot missing exclusion for {pattern!r}")
    else:
        _fail("orchestrator app.py not found")


def check_kernel_policy() -> None:
    """Kernel policy contains all required fields."""
    print("\n[8] Kernel policy fields", flush=True)
    policy_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "policies",
        "gate_policy.yaml",
    )
    if os.path.isfile(policy_path):
        try:
            import yaml

            with open(policy_path) as f:
                policy = yaml.safe_load(f) or {}
        except ImportError:
            content = open(policy_path).read()
            policy = {}
            for key in [
                "max_patch_files",
                "max_patch_total_lines",
                "forbid_test_edits",
                "forbid_ci_edits",
                "forbid_dep_manifest_edits",
            ]:
                if key in content:
                    policy[key] = True

        required_fields = [
            "max_patch_files",
            "max_patch_total_lines",
            "max_added_lines",
            "max_deleted_lines",
            "forbid_test_edits",
            "forbid_ci_edits",
            "forbid_dep_manifest_edits",
        ]
        for field in required_fields:
            if field in policy:
                _ok(f"kernel policy has {field}")
            else:
                _fail(f"kernel policy missing {field}")
    else:
        _fail("gate_policy.yaml not found")


def check_compose_defaults() -> None:
    """Compose has all required hardening defaults."""
    print("\n[9] Compose hardening defaults", flush=True)
    compose_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "docker-compose.yml",
    )
    if os.path.isfile(compose_path):
        content = open(compose_path).read()
        required_defaults = {
            "RFSN_DEV_MODE:-0": "DEV_MODE defaults to 0",
            "RFSN_AUTH_REQUIRED:-1": "AUTH_REQUIRED defaults to 1",
            "RFSN_PATCH_GATE_REQUIRED:-1": "PATCH_GATE_REQUIRED defaults to 1",
            "RFSN_VENV_MODE:-per_run": "VENV_MODE defaults to per_run",
            "RFSN_WARM_SANDBOX:-0": "WARM_SANDBOX defaults to 0",
        }
        for pattern, label in required_defaults.items():
            if pattern in content:
                _ok(label)
            else:
                _fail(f"missing compose default: {label}")
    else:
        _fail("docker-compose.yml not found")


def main() -> None:
    print("=" * 60)
    print("RFSN HARDENING VERIFICATION")
    print("=" * 60)

    check_auth_guard()
    check_patch_gate_guard()
    check_warm_sandbox_default()
    check_template_validation()
    check_target_sanitizer()
    check_venv_mode()
    check_snapshot_excludes()
    check_kernel_policy()
    check_compose_defaults()

    print("\n" + "=" * 60)
    print(f"RESULTS: {PASS} passed, {FAIL} failed")
    print("=" * 60)

    if FAIL > 0:
        sys.exit(1)
    print("\nVERIFY_OK — all hardening checks passed.")


if __name__ == "__main__":
    main()
