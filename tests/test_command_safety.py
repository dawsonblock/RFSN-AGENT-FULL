"""tests/test_command_safety.py

Tests for the command execution safety policy.

Acceptance:
* pytest_file works with safe relative path.
* pytest_file rejects path traversal.
* Raw rm -rf / rejected.
* bash -c rejected.
* curl rejected.
* Semicolon injection rejected.
* Pipe injection rejected.
* Redirect injection rejected.
* Disabled trace_execution rejected.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = str(Path(__file__).resolve().parent.parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from autofix.apply import (
    ALLOWED_COMMAND_TEMPLATES,
    run_template,
    _has_metachar,
)
from rfsn_kernel.tool_registry import CANONICAL_TOOLS
from rfsn_kernel.validate import validate, _DISABLED_TOOLS
from rfsn_kernel.state import Proposal, SystemState


# ---------------------------------------------------------------------------
# ALLOWED_COMMAND_TEMPLATES shape
# ---------------------------------------------------------------------------

class TestAllowedTemplates:
    def test_templates_are_lists(self):
        for name, cmd in ALLOWED_COMMAND_TEMPLATES.items():
            assert isinstance(cmd, list), f"Template {name!r} is not a list"
            assert len(cmd) >= 1

    def test_no_shell_string_in_templates(self):
        """No template value should be a plain string (would need shell=True)."""
        for name, cmd in ALLOWED_COMMAND_TEMPLATES.items():
            assert not isinstance(cmd, str), (
                f"Template {name!r} is a string — use a list instead"
            )

    def test_pytest_template_present(self):
        assert "pytest" in ALLOWED_COMMAND_TEMPLATES
        assert "pytest_file" in ALLOWED_COMMAND_TEMPLATES

    def test_ruff_templates_present(self):
        assert "ruff_check" in ALLOWED_COMMAND_TEMPLATES
        assert "ruff_format" in ALLOWED_COMMAND_TEMPLATES


# ---------------------------------------------------------------------------
# Metacharacter detection
# ---------------------------------------------------------------------------

class TestMetacharDetection:
    def test_semicolon_detected(self):
        assert _has_metachar("foo; rm -rf /")

    def test_pipe_detected(self):
        assert _has_metachar("foo | cat /etc/passwd")

    def test_ampersand_detected(self):
        assert _has_metachar("foo & bar")

    def test_redirect_out_detected(self):
        assert _has_metachar("foo > /tmp/out")

    def test_redirect_in_detected(self):
        assert _has_metachar("foo < /dev/null")

    def test_dollar_paren_detected(self):
        assert _has_metachar("$(whoami)")

    def test_backtick_detected(self):
        assert _has_metachar("`id`")

    def test_newline_detected(self):
        assert _has_metachar("foo\nbar")

    def test_safe_path_not_detected(self):
        assert not _has_metachar("src/main.py")
        assert not _has_metachar("tests/test_foo.py")


# ---------------------------------------------------------------------------
# run_template safety
# ---------------------------------------------------------------------------

class TestRunTemplate:
    def test_unknown_template_rejected(self, tmp_path):
        result = run_template("rm_rf_root", path="/", workdir=str(tmp_path))
        assert not result["ok"]
        assert "Unknown template" in result.get("error", "")

    def test_bash_c_not_in_templates(self):
        """bash -c must not be an allowlisted template."""
        for name, cmd in ALLOWED_COMMAND_TEMPLATES.items():
            assert "bash" not in cmd, f"Template {name!r} contains 'bash'"
            assert "sh" not in cmd[:1], f"Template {name!r} starts with 'sh'"

    def test_curl_not_in_templates(self):
        for name, cmd in ALLOWED_COMMAND_TEMPLATES.items():
            assert "curl" not in cmd, f"Template {name!r} contains 'curl'"
            assert "wget" not in cmd, f"Template {name!r} contains 'wget'"

    def test_path_traversal_rejected(self, tmp_path):
        result = run_template("pytest_file", path="../../etc/passwd", workdir=str(tmp_path))
        assert not result["ok"]
        assert "traversal" in result.get("error", "").lower()

    def test_absolute_path_rejected(self, tmp_path):
        result = run_template("pytest_file", path="/etc/passwd", workdir=str(tmp_path))
        assert not result["ok"]

    def test_semicolon_in_path_rejected(self, tmp_path):
        result = run_template("pytest_file", path="tests/foo.py; rm -rf /", workdir=str(tmp_path))
        assert not result["ok"]
        assert "metachar" in result.get("error", "").lower()

    def test_pipe_in_path_rejected(self, tmp_path):
        result = run_template("pytest_file", path="tests/foo.py | cat", workdir=str(tmp_path))
        assert not result["ok"]

    def test_redirect_in_path_rejected(self, tmp_path):
        result = run_template("pytest_file", path="tests/foo.py > /tmp/evil", workdir=str(tmp_path))
        assert not result["ok"]


# ---------------------------------------------------------------------------
# Disabled trace_execution rejected by validate
# ---------------------------------------------------------------------------

class TestTraceExecutionDisabled:
    def _state(self):
        return SystemState()

    def _policy(self):
        return {"max_total_steps": 100}

    def test_trace_execution_in_disabled_set(self):
        assert "trace_execution" in _DISABLED_TOOLS

    def test_trace_execution_not_enabled(self):
        spec = CANONICAL_TOOLS.get("trace_execution")
        assert spec is not None
        assert not spec.enabled

    def test_trace_execution_rejected_by_validate(self):
        p = Proposal(
            action="trace_execution",
            params={"path": "x.py"},
            context_hash="c",
            planner_hash="p",
        )
        result = validate(p, self._state(), self._policy())
        assert not result.ok
        codes = [e.get("code") for e in result.errors]
        assert "TOOL_DISABLED" in codes


# ---------------------------------------------------------------------------
# Ensure no shell=True in autofix.apply runtime paths
# ---------------------------------------------------------------------------

class TestNoShellTrueInApply:
    def test_apply_module_has_no_shell_true(self):
        """The autofix/apply.py source must not contain shell=True as a
        subprocess keyword argument after the repair.

        We use a simple heuristic: look for ``shell=True`` preceded by
        ``subprocess.run(`` or ``, shell=True`` in the same logical line.
        References in comments and docstrings are acceptable.
        """
        import re
        apply_path = Path(ROOT) / "autofix" / "apply.py"
        source = apply_path.read_text()
        # Match shell=True that appears as a subprocess keyword argument.
        # Pattern: shell=True that is NOT on a comment-only line and NOT
        # inside a string literal that starts the line (docstring line).
        bad_lines = []
        for i, line in enumerate(source.splitlines()):
            stripped = line.strip()
            # Skip obvious comment-only lines.
            if stripped.startswith("#"):
                continue
            # Check for shell=True as a keyword argument (preceded by comma
            # or opening paren, to distinguish from prose in strings).
            if re.search(r'[,(]\s*shell\s*=\s*True', line):
                bad_lines.append((i + 1, line.rstrip()))
        assert not bad_lines, (
            f"shell=True found as keyword argument in autofix/apply.py: "
            f"{bad_lines}"
        )
