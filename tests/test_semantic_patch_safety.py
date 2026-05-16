"""tests/test_semantic_patch_safety.py

Tests for apply_semantic_patch safety guarantees.

Acceptance:
* Valid search/replace changes file.
* Empty search fails with PatchConflictError.
* Missing search text fails with PatchConflictError.
* Identical replacement fails with NoOpPatchError.
* Missing file fails with FileNotFoundError.
* Path traversal fails with PatchPathError.
* Attempt to patch tests fails with PatchGateError.
* Attempt to patch dependency manifests fails with PatchGateError.
* Attempt to patch CI config fails with PatchGateError.
* No-op never reports success.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = str(Path(__file__).resolve().parent.parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from rfsn_swebench.patcher import (
    apply_semantic_patch_to_content,
    apply_semantic_patch_to_file,
    PatchConflictError,
    NoOpPatchError,
    PatchPathError,
    PatchGateError,
    PatchResult,
)


# ---------------------------------------------------------------------------
# Content-level tests (no I/O)
# ---------------------------------------------------------------------------

class TestApplySemanticPatchToContent:
    def test_valid_replacement_changes_content(self):
        content = "def foo():\n    return 1\n"
        result = apply_semantic_patch_to_content(
            content, "return 1", "return 42"
        )
        assert "return 42" in result
        assert "return 1" not in result

    def test_empty_search_raises(self):
        with pytest.raises(PatchConflictError, match="empty"):
            apply_semantic_patch_to_content("anything", "", "replace")

    def test_missing_search_raises(self):
        with pytest.raises(PatchConflictError, match="not found"):
            apply_semantic_patch_to_content("def foo(): pass", "def bar()", "def baz()")

    def test_noop_raises(self):
        """search == replace must raise NoOpPatchError."""
        with pytest.raises(NoOpPatchError):
            apply_semantic_patch_to_content("def foo(): pass", "foo", "foo")

    def test_ambiguous_raises(self):
        """Multiple occurrences of search must raise PatchConflictError."""
        content = "foo\nfoo\n"
        with pytest.raises(PatchConflictError, match="2 times"):
            apply_semantic_patch_to_content(content, "foo", "bar")

    def test_result_is_string(self):
        result = apply_semantic_patch_to_content("hello world", "world", "there")
        assert isinstance(result, str)
        assert result == "hello there"


# ---------------------------------------------------------------------------
# File-level tests (with I/O)
# ---------------------------------------------------------------------------

class TestApplySemanticPatchToFile:
    def test_valid_patch_changes_file(self, tmp_path):
        f = tmp_path / "utils.py"
        f.write_text("def add(a, b):\n    return a - b\n")
        result = apply_semantic_patch_to_file(
            str(tmp_path), "utils.py", "return a - b", "return a + b",
            skip_patch_gate=True,
        )
        assert isinstance(result, PatchResult)
        assert result.files_changed == ["utils.py"]
        assert "return a + b" in f.read_text()
        assert result.before_hash != result.after_hash

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            apply_semantic_patch_to_file(
                str(tmp_path), "nonexistent.py", "x", "y",
                skip_patch_gate=True,
            )

    def test_path_traversal_raises(self, tmp_path):
        with pytest.raises(PatchPathError):
            apply_semantic_patch_to_file(
                str(tmp_path), "../../etc/passwd", "root", "evil",
                skip_patch_gate=True,
            )

    def test_absolute_path_raises(self, tmp_path):
        with pytest.raises(PatchPathError):
            apply_semantic_patch_to_file(
                str(tmp_path), "/etc/passwd", "root", "evil",
                skip_patch_gate=True,
            )

    def test_noop_raises(self, tmp_path):
        f = tmp_path / "x.py"
        f.write_text("result = 1\n")
        with pytest.raises(NoOpPatchError):
            apply_semantic_patch_to_file(
                str(tmp_path), "x.py", "result = 1", "result = 1",
                skip_patch_gate=True,
            )

    def test_patch_returns_hash_pair(self, tmp_path):
        f = tmp_path / "code.py"
        f.write_text("x = 1\n")
        result = apply_semantic_patch_to_file(
            str(tmp_path), "code.py", "x = 1", "x = 2",
            skip_patch_gate=True,
        )
        assert len(result.before_hash) == 64  # sha256 hex
        assert len(result.after_hash) == 64
        assert result.before_hash != result.after_hash

    def test_diff_preview_non_empty(self, tmp_path):
        f = tmp_path / "code.py"
        f.write_text("val = old\n")
        result = apply_semantic_patch_to_file(
            str(tmp_path), "code.py", "val = old", "val = new",
            skip_patch_gate=True,
        )
        assert result.diff_preview  # must be non-empty


# ---------------------------------------------------------------------------
# Patch gate tests
# ---------------------------------------------------------------------------

class TestPatchGate:
    def test_test_file_rejected(self, tmp_path):
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        f = tests_dir / "test_foo.py"
        f.write_text("def test_add(): assert 1 == 1\n")
        with pytest.raises(PatchGateError, match="test file"):
            apply_semantic_patch_to_file(
                str(tmp_path), "tests/test_foo.py", "assert 1 == 1", "assert 1 == 2",
            )

    def test_dep_manifest_rejected(self, tmp_path):
        f = tmp_path / "requirements.txt"
        f.write_text("requests==2.28.0\n")
        with pytest.raises(PatchGateError, match="dependency manifest"):
            apply_semantic_patch_to_file(
                str(tmp_path), "requirements.txt", "requests==2.28.0", "requests==99.0",
            )

    def test_ci_config_rejected(self, tmp_path):
        ci_dir = tmp_path / ".github" / "workflows"
        ci_dir.mkdir(parents=True)
        f = ci_dir / "ci.yml"
        f.write_text("on: push\n")
        with pytest.raises(PatchGateError, match="CI config"):
            apply_semantic_patch_to_file(
                str(tmp_path), ".github/workflows/ci.yml", "on: push", "on: pull_request",
            )

    def test_pyproject_toml_rejected(self, tmp_path):
        f = tmp_path / "pyproject.toml"
        f.write_text("[tool.poetry]\nname = 'myapp'\n")
        with pytest.raises(PatchGateError, match="dependency manifest"):
            apply_semantic_patch_to_file(
                str(tmp_path), "pyproject.toml", "name = 'myapp'", "name = 'evil'",
            )

    def test_normal_file_accepted_through_gate(self, tmp_path):
        f = tmp_path / "utils.py"
        f.write_text("def greet(): return 'hello'\n")
        # Should NOT raise PatchGateError
        result = apply_semantic_patch_to_file(
            str(tmp_path), "utils.py", "return 'hello'", "return 'hi'",
        )
        assert result.files_changed == ["utils.py"]


# ---------------------------------------------------------------------------
# No-op safety
# ---------------------------------------------------------------------------

class TestNoOpSafety:
    def test_noop_never_reports_success_content(self):
        """apply_semantic_patch_to_content must raise NoOpPatchError on no-op."""
        with pytest.raises(NoOpPatchError):
            apply_semantic_patch_to_content("x = 1", "x = 1", "x = 1")

    def test_noop_never_reports_success_file(self, tmp_path):
        """apply_semantic_patch_to_file must raise NoOpPatchError on no-op."""
        f = tmp_path / "x.py"
        f.write_text("x = 1\n")
        with pytest.raises(NoOpPatchError):
            apply_semantic_patch_to_file(
                str(tmp_path), "x.py", "x = 1", "x = 1",
                skip_patch_gate=True,
            )
