"""Regression tests for Phase 1 fix: HardKernel must instantiate correctly.

Covers:
* HardKernel() instantiates with default config (no explicit policy).
* HardKernel(tier_policy_path=...) instantiates with a valid YAML file.
* Missing / None tier_policy_path falls back safely (no crash).
* Invalid-path tier_policy_path falls back safely (no crash).
* kernel_step attribute exists.
* _load_tier_policy attribute exists (method was previously missing).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = str(Path(__file__).resolve().parent.parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from rfsn_kernel.kernel import HardKernel  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ledger_path(tmp_path: Path) -> str:
    return str(tmp_path / "test_ledger.jsonl")


def _write_valid_policy(tmp_path: Path) -> str:
    policy = {
        "tiers": {
            0: {
                "name": "code-only",
                "allow": {"edit_tests": False, "edit_deps": False, "edit_ci": False},
                "budgets": {},
            },
        },
        "escalation_rules": {},
        "classifiers": {
            "tests_globs": ["**/tests/**"],
            "deps_globs": ["**/requirements.txt"],
            "ci_globs": ["**/.github/workflows/**"],
        },
    }
    path = str(tmp_path / "test_tier_policy.yaml")
    with open(path, "w") as f:
        yaml.dump(policy, f)
    return path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHardKernelBoot:
    def test_default_instantiation(self, tmp_path):
        """HardKernel() must instantiate without arguments (except ledger path)."""
        # The default env-var path likely doesn't exist on the test runner
        # — the fallback must handle that gracefully.
        kernel = HardKernel(ledger_path=_make_ledger_path(tmp_path))
        assert kernel is not None

    def test_instantiation_with_valid_policy(self, tmp_path):
        """HardKernel instantiates when given a valid tier-policy YAML."""
        policy_path = _write_valid_policy(tmp_path)
        kernel = HardKernel(
            ledger_path=_make_ledger_path(tmp_path),
            tier_policy_path=policy_path,
        )
        assert kernel is not None
        # The policy should be loaded with at least tier 0.
        assert 0 in kernel.tier_policy.get("tiers", {})

    def test_instantiation_with_none_policy_path(self, tmp_path):
        """Passing tier_policy_path=None falls back to the safe default."""
        kernel = HardKernel(
            ledger_path=_make_ledger_path(tmp_path),
            tier_policy_path=None,
        )
        assert kernel is not None
        tiers = kernel.tier_policy.get("tiers", {})
        assert isinstance(tiers, dict)
        assert len(tiers) >= 1

    def test_instantiation_with_missing_policy_path(self, tmp_path):
        """Passing a non-existent path falls back to the safe default."""
        missing = str(tmp_path / "does_not_exist.yaml")
        kernel = HardKernel(
            ledger_path=_make_ledger_path(tmp_path),
            tier_policy_path=missing,
        )
        assert kernel is not None
        tiers = kernel.tier_policy.get("tiers", {})
        assert isinstance(tiers, dict)
        assert len(tiers) >= 1

    def test_has_kernel_step(self, tmp_path):
        """kernel_step attribute must exist on every HardKernel instance."""
        kernel = HardKernel(
            ledger_path=_make_ledger_path(tmp_path),
            tier_policy_path=None,
        )
        assert hasattr(kernel, "kernel_step"), "kernel_step missing from HardKernel"
        assert callable(kernel.kernel_step)

    def test_has_load_tier_policy(self, tmp_path):
        """_load_tier_policy must exist (it was previously missing, causing AttributeError)."""
        kernel = HardKernel(
            ledger_path=_make_ledger_path(tmp_path),
            tier_policy_path=None,
        )
        assert hasattr(
            kernel, "_load_tier_policy"
        ), "_load_tier_policy missing from HardKernel"
        assert callable(kernel._load_tier_policy)

    def test_only_one_kernel_step_method(self, tmp_path):
        """There must be exactly one kernel_step method — no duplicates.

        Also instantiates the kernel to confirm the duplicate doesn't survive
        at runtime (a duplicate would silently shadow the real implementation).
        """
        import inspect
        kernel = HardKernel(
            ledger_path=_make_ledger_path(tmp_path),
            tier_policy_path=None,
        )
        members = inspect.getmembers(HardKernel, predicate=inspect.isfunction)
        ks_members = [m for m in members if m[0] == "kernel_step"]
        assert len(ks_members) == 1, (
            f"Expected exactly 1 kernel_step, found {len(ks_members)}"
        )
        # Confirm the method on the instance is the real one (has a docstring).
        assert kernel.kernel_step.__doc__ is not None

    def test_load_tier_policy_returns_dict(self, tmp_path):
        """_load_tier_policy must return a dict in all code paths."""
        kernel = HardKernel(
            ledger_path=_make_ledger_path(tmp_path),
            tier_policy_path=None,
        )
        # None path
        result = kernel._load_tier_policy(None)
        assert isinstance(result, dict)

        # Missing path
        result = kernel._load_tier_policy("/nonexistent/path.yaml")
        assert isinstance(result, dict)

        # Valid path
        policy_path = _write_valid_policy(tmp_path)
        result = kernel._load_tier_policy(policy_path)
        assert isinstance(result, dict)
        assert "tiers" in result

    def test_malformed_yaml_falls_back_to_default(self, tmp_path):
        """A YAML file with non-dict top-level falls back to the default policy."""
        bad_yaml = str(tmp_path / "bad.yaml")
        with open(bad_yaml, "w") as f:
            f.write("- this is a list not a dict\n")
        kernel = HardKernel(
            ledger_path=_make_ledger_path(tmp_path),
            tier_policy_path=bad_yaml,
        )
        assert kernel is not None
        tiers = kernel.tier_policy.get("tiers", {})
        assert isinstance(tiers, dict)
        assert len(tiers) >= 1


# ---------------------------------------------------------------------------
# Normalize / registry param-preservation tests
# Verify that the params added/reconciled in tool_registry.py are not
# silently stripped by normalize() after the registry wiring.
# ---------------------------------------------------------------------------

class TestNormalizeRegistryParamPreservation:
    """Params that exist in ToolSpec.allowed_params must survive normalize()."""

    def _norm(self, step: dict) -> dict:
        from rfsn_kernel.normalize import normalize
        return normalize(step, "test", "ctx", "b1").params

    def test_repo_read_range_timeout_s_preserved(self):
        params = self._norm(
            {"type": "repo_read_range", "path": "src/a.py",
             "line_start": 1, "line_end": 10, "timeout_s": 30}
        )
        assert params.get("timeout_s") == 30

    def test_read_file_timeout_s_preserved(self):
        params = self._norm({"type": "read_file", "path": "foo.py", "timeout_s": 15})
        assert params.get("timeout_s") == 15

    def test_detect_project_path_and_timeout_preserved(self):
        params = self._norm({"type": "detect_project", "path": ".", "timeout_s": 5})
        assert params.get("path") == "."
        assert params.get("timeout_s") == 5

    def test_detect_workdirs_timeout_s_preserved(self):
        params = self._norm({"type": "detect_workdirs", "max_depth": 3, "timeout_s": 10})
        assert params.get("timeout_s") == 10

    def test_run_tests_template_params_preserved(self):
        params = self._norm(
            {"type": "run_tests", "template_id": "pytest",
             "template_params": {"k": "v"}, "workdir_id": "workdir_1"}
        )
        assert params.get("template_params") == {"k": "v"}
        assert params.get("workdir_id") == "workdir_1"

    def test_ensure_deps_manifest_preserved(self):
        params = self._norm(
            {"type": "ensure_deps", "manifest": "requirements.txt",
             "workdir_id": "workdir_0", "timeout_s": 60}
        )
        assert params.get("manifest") == "requirements.txt"
        assert params.get("workdir_id") == "workdir_0"

    def test_new_tools_params_preserved(self):
        """New tools introduced in the registry must not have params stripped."""
        # apply_semantic_patch
        params = self._norm(
            {"type": "apply_semantic_patch",
             "path": "foo.py", "search": "old", "replace": "new"}
        )
        assert params.get("path") == "foo.py"
        assert params.get("search") == "old"
        assert params.get("replace") == "new"

        # generate_repo_map
        params = self._norm({"type": "generate_repo_map", "path": ".", "max_depth": 2})
        assert params.get("path") == "."
        assert params.get("max_depth") == 2

        # list_files
        params = self._norm({"type": "list_files", "path": "src", "glob": "*.py"})
        assert params.get("path") == "src"
        assert params.get("glob") == "*.py"

    def test_unknown_extra_params_still_stripped(self):
        """Unregistered params must still be stripped even for known actions."""
        params = self._norm(
            {"type": "read_file", "path": "foo.py",
             "evil_extra": "bad", "another_extra": 99}
        )
        assert "evil_extra" not in params
        assert "another_extra" not in params
        assert params.get("path") == "foo.py"

