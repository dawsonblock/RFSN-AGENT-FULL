"""tests/test_tool_registry_consistency.py

Verifies that the canonical tool registry is consistent with:
- policies/tool_allowlist.yaml
- rfsn_kernel/validate.py
- rfsn_kernel/normalize.py
- rfsn_kernel/dispatcher.py

Acceptance:
* No tool can be in the YAML allowlist and also be disabled in the registry.
* No enabled registry tool is missing from the dispatcher's handler map.
* Disabled tools are rejected by validate().
* Unknown tools are rejected by validate().
* Registry params match what normalize preserves.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = str(Path(__file__).resolve().parent.parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import yaml

from rfsn_kernel.tool_registry import (
    CANONICAL_TOOLS,
    ENABLED_TOOL_NAMES,
    CANONICAL_TOOL_NAMES,
)
from rfsn_kernel.validate import validate, VALID_ACTIONS, _DISABLED_TOOLS
from rfsn_kernel.normalize import normalize, _ALLOWED_PARAMS
from rfsn_kernel.state import Proposal, SystemState
from rfsn_kernel.dispatcher import _HANDLERS, dispatch_tool, ExecutionContext


# ---------------------------------------------------------------------------
# Load YAML policy
# ---------------------------------------------------------------------------

def _load_yaml_allowlist() -> list:
    path = Path(ROOT) / "policies" / "tool_allowlist.yaml"
    if not path.exists():
        pytest.skip(f"tool_allowlist.yaml not found at {path}")
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return list(data.get("allowed_step_types", []))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRegistryCompleteness:
    def test_all_yaml_tools_exist_in_registry(self):
        """Every tool listed in tool_allowlist.yaml must be in CANONICAL_TOOLS."""
        yaml_tools = _load_yaml_allowlist()
        for name in yaml_tools:
            assert name in CANONICAL_TOOLS, (
                f"YAML allowlist tool {name!r} is missing from CANONICAL_TOOLS"
            )

    def test_no_yaml_tool_is_disabled(self):
        """YAML allowlist must not include disabled tools."""
        yaml_tools = _load_yaml_allowlist()
        for name in yaml_tools:
            spec = CANONICAL_TOOLS.get(name)
            if spec is not None:
                assert spec.enabled, (
                    f"YAML allowlist contains disabled tool {name!r}. "
                    "Remove it from allowed_step_types."
                )

    def test_enabled_tools_have_dispatcher_handler(self):
        """Every enabled tool must have an entry in dispatcher._HANDLERS."""
        for name in ENABLED_TOOL_NAMES:
            assert name in _HANDLERS, (
                f"Enabled tool {name!r} has no handler in dispatcher._HANDLERS"
            )

    def test_valid_actions_matches_canonical_names(self):
        """validate.VALID_ACTIONS must equal CANONICAL_TOOL_NAMES."""
        assert VALID_ACTIONS == set(CANONICAL_TOOL_NAMES)

    def test_normalize_params_match_registry(self):
        """_ALLOWED_PARAMS must be derived from registry allowed_params."""
        for name, spec in CANONICAL_TOOLS.items():
            assert name in _ALLOWED_PARAMS, (
                f"{name!r} is in CANONICAL_TOOLS but missing from _ALLOWED_PARAMS"
            )
            assert set(_ALLOWED_PARAMS[name]) == set(spec.allowed_params), (
                f"_ALLOWED_PARAMS[{name!r}] does not match ToolSpec.allowed_params"
            )


class TestDisabledToolRejection:
    def _state(self):
        return SystemState()

    def _policy(self):
        return {"max_total_steps": 100}

    def test_trace_execution_disabled_by_validate(self):
        """trace_execution must be rejected with TOOL_DISABLED."""
        p = Proposal(
            action="trace_execution",
            params={},
            context_hash="c",
            planner_hash="p",
        )
        result = validate(p, self._state(), self._policy())
        assert not result.ok
        assert any(e.get("code") == "TOOL_DISABLED" for e in result.errors)

    def test_apply_semantic_patch_disabled_by_validate(self):
        """apply_semantic_patch must be rejected with TOOL_DISABLED."""
        p = Proposal(
            action="apply_semantic_patch",
            params={"path": "f.py", "search": "x", "replace": "y"},
            context_hash="c",
            planner_hash="p",
        )
        result = validate(p, self._state(), self._policy())
        assert not result.ok
        assert any(e.get("code") == "TOOL_DISABLED" for e in result.errors)

    def test_unknown_tool_rejected(self):
        """Unknown tools must be rejected with UNKNOWN_ACTION."""
        p = Proposal(
            action="totally_unknown_tool",
            params={},
            context_hash="c",
            planner_hash="p",
        )
        result = validate(p, self._state(), self._policy())
        assert not result.ok
        assert any(e.get("code") == "UNKNOWN_ACTION" for e in result.errors)

    def test_disabled_tool_rejected_by_dispatcher(self, tmp_path):
        """Dispatcher must reject disabled tools even if called directly."""
        ctx = ExecutionContext(workspace_root=str(tmp_path))
        result = dispatch_tool("trace_execution", {}, ctx)
        assert not result.success
        assert "TOOL_DISABLED" in (result.error or "")

    def test_unknown_tool_rejected_by_dispatcher(self, tmp_path):
        """Dispatcher must reject unknown tools."""
        ctx = ExecutionContext(workspace_root=str(tmp_path))
        result = dispatch_tool("nonexistent_tool_xyz", {}, ctx)
        assert not result.success
        assert "UNKNOWN_TOOL" in (result.error or "")


class TestEnabledToolAcceptance:
    def _state(self):
        return SystemState()

    def _policy(self):
        return {"max_total_steps": 100}

    def test_read_file_accepted_by_validate(self):
        p = Proposal(
            action="read_file",
            params={"path": "src/main.py"},
            context_hash="c",
            planner_hash="p",
        )
        result = validate(p, self._state(), self._policy())
        assert result.ok, result.errors

    def test_repo_search_accepted_by_validate(self):
        p = Proposal(
            action="repo_search",
            params={"pattern": "def foo"},
            context_hash="c",
            planner_hash="p",
        )
        result = validate(p, self._state(), self._policy())
        assert result.ok, result.errors

    def test_read_file_dispatches(self, tmp_path):
        (tmp_path / "hello.py").write_text("print('hi')")
        ctx = ExecutionContext(workspace_root=str(tmp_path))
        result = dispatch_tool("read_file", {"path": "hello.py"}, ctx)
        assert result.success
        assert "print" in (result.output or "")


class TestNormalizeStripsUnknownParams:
    def test_unknown_params_stripped_for_read_file(self):
        raw = {"type": "read_file", "path": "f.py", "evil": "bad"}
        p = normalize(raw, "intent", "ctx", "b")
        assert "evil" not in p.params
        assert "path" in p.params

    def test_known_params_preserved_for_apply_patch(self):
        raw = {"type": "apply_patch", "patch": "--- a\n+++ b\n", "timeout_s": 30}
        p = normalize(raw, "intent", "ctx", "b")
        assert "patch" in p.params
        assert "timeout_s" in p.params
