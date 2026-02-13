"""Tests for rfsn_swebench.locator — repo tree, file localization, context."""

from __future__ import annotations

import os
import tempfile

from rfsn_swebench.locator import (
    build_repo_tree,
    locate_files,
    read_file_context,
)


# ---------------------------------------------------------------------------
# build_repo_tree
# ---------------------------------------------------------------------------


def test_build_repo_tree_basic():
    with tempfile.TemporaryDirectory() as tmpdir:
        os.makedirs(os.path.join(tmpdir, "src"))
        with open(os.path.join(tmpdir, "src", "main.py"), "w") as f:
            f.write("print('hello')\n")
        with open(os.path.join(tmpdir, "README.md"), "w") as f:
            f.write("# Readme\n")

        tree = build_repo_tree(tmpdir, max_files=50)
        assert "src/main.py" in tree
        assert "README.md" in tree


def test_build_repo_tree_skips_hidden():
    with tempfile.TemporaryDirectory() as tmpdir:
        os.makedirs(os.path.join(tmpdir, ".git", "objects"), exist_ok=True)
        with open(os.path.join(tmpdir, ".git", "config"), "w") as f:
            f.write("[core]\n")
        with open(os.path.join(tmpdir, "app.py"), "w") as f:
            f.write("x = 1\n")

        tree = build_repo_tree(tmpdir)
        assert "app.py" in tree
        assert ".git" not in tree


def test_build_repo_tree_respects_max_files():
    with tempfile.TemporaryDirectory() as tmpdir:
        for i in range(20):
            with open(os.path.join(tmpdir, f"f{i}.py"), "w") as f:
                f.write(f"x = {i}\n")

        tree = build_repo_tree(tmpdir, max_files=5)
        lines = [l for l in tree.splitlines() if l.strip()]
        assert len(lines) <= 5


# ---------------------------------------------------------------------------
# locate_files
# ---------------------------------------------------------------------------


def test_locate_files_json_array():
    resp = '["src/foo.py", "src/bar.py"]'
    assert locate_files(resp) == ["src/foo.py", "src/bar.py"]


def test_locate_files_json_in_code_block():
    resp = '```json\n["a/b.py", "c/d.py"]\n```'
    assert locate_files(resp) == ["a/b.py", "c/d.py"]


def test_locate_files_json_object_with_files_key():
    resp = '{"files": ["x.py", "y.py"]}'
    assert locate_files(resp) == ["x.py", "y.py"]


def test_locate_files_markdown_list():
    resp = "- src/module.py\n- tests/test_module.py\n"
    result = locate_files(resp)
    assert "src/module.py" in result


def test_locate_files_numbered_list():
    resp = "1. `src/core.py`\n2. `lib/utils.py`\n"
    result = locate_files(resp)
    assert "src/core.py" in result
    assert "lib/utils.py" in result


def test_locate_files_deduplicates():
    resp = '["a.py", "b.py", "a.py"]'
    assert locate_files(resp) == ["a.py", "b.py"]


def test_locate_files_empty():
    assert locate_files("No files found") == []


# ---------------------------------------------------------------------------
# read_file_context
# ---------------------------------------------------------------------------


def test_read_file_context_basic():
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "main.py"), "w") as f:
            f.write("def hello():\n    return 'world'\n")

        ctx = read_file_context(tmpdir, ["main.py"])
        assert "## File: main.py" in ctx
        assert "def hello():" in ctx
        assert "return 'world'" in ctx


def test_read_file_context_missing_file():
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx = read_file_context(tmpdir, ["nonexistent.py"])
        assert "(file not found)" in ctx


def test_read_file_context_respects_max_total():
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "big.py"), "w") as f:
            f.write("x = 1\n" * 5000)

        ctx = read_file_context(tmpdir, ["big.py"], max_total_chars=100)
        # Content should be truncated
        assert len(ctx) < 5000


def test_read_file_context_line_numbers():
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "numbered.py"), "w") as f:
            f.write("line_one\nline_two\nline_three\n")

        ctx = read_file_context(tmpdir, ["numbered.py"])
        assert "   1 | line_one" in ctx
        assert "   2 | line_two" in ctx
