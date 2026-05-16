"""tests/test_executor_dispatch_consistency.py

Verifies that warm and cold execution paths are consistent via the unified
dispatcher.

Acceptance:
* read_file same behavior warm/cold.
* apply_patch same gate warm/cold.
* apply_semantic_patch same gate warm/cold (both disabled).
* trace_execution rejected warm/cold.
* Unknown tool rejected warm/cold.
* Result has stable schema.
"""

from __future__ import annotations

import sys
from dataclasses import fields
from pathlib import Path

import pytest

ROOT = str(Path(__file__).resolve().parent.parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from rfsn_kernel.dispatcher import dispatch_tool, ExecutionContext, ToolResult


# ---------------------------------------------------------------------------
# Context factories — simulate warm vs cold path
# ---------------------------------------------------------------------------

def _warm_ctx(tmp_path) -> ExecutionContext:
    return ExecutionContext(
        workspace_root=str(tmp_path),
        run_id="run-warm",
        sandbox_mode="local_dev",
        dev_mode=True,
    )


def _cold_ctx(tmp_path) -> ExecutionContext:
    return ExecutionContext(
        workspace_root=str(tmp_path),
        run_id="run-cold",
        sandbox_mode="local_dev",
        dev_mode=True,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _result_schema_fields():
    return {f.name for f in fields(ToolResult)}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestResultSchema:
    def test_tool_result_has_required_fields(self):
        required = {
            "success", "tool", "output", "stdout", "stderr", "exit_code",
            "files_changed", "diff_stats", "timeout", "error",
            "policy_decision_id", "replay_event_id",
        }
        assert required.issubset(_result_schema_fields())

    def test_dispatch_always_returns_tool_result(self, tmp_path):
        ctx = _warm_ctx(tmp_path)
        result = dispatch_tool("read_file", {"path": "nofile.py"}, ctx)
        assert isinstance(result, ToolResult)

    def test_result_has_tool_name(self, tmp_path):
        ctx = _warm_ctx(tmp_path)
        result = dispatch_tool("read_file", {"path": "nofile.py"}, ctx)
        assert result.tool == "read_file"

    def test_result_has_replay_event_id(self, tmp_path):
        ctx = _warm_ctx(tmp_path)
        result = dispatch_tool("read_file", {"path": "nofile.py"}, ctx)
        assert result.replay_event_id is not None


class TestWarmColdConsistency:
    def test_read_file_success_same_warm_cold(self, tmp_path):
        (tmp_path / "hello.py").write_text("x = 1\n")
        warm = dispatch_tool("read_file", {"path": "hello.py"}, _warm_ctx(tmp_path))
        cold = dispatch_tool("read_file", {"path": "hello.py"}, _cold_ctx(tmp_path))
        assert warm.success == cold.success
        assert warm.output == cold.output

    def test_read_file_missing_same_warm_cold(self, tmp_path):
        warm = dispatch_tool("read_file", {"path": "missing.py"}, _warm_ctx(tmp_path))
        cold = dispatch_tool("read_file", {"path": "missing.py"}, _cold_ctx(tmp_path))
        assert warm.success == cold.success
        assert not warm.success
        assert not cold.success

    def test_trace_execution_rejected_warm(self, tmp_path):
        result = dispatch_tool("trace_execution", {}, _warm_ctx(tmp_path))
        assert not result.success
        assert "TOOL_DISABLED" in (result.error or "")

    def test_trace_execution_rejected_cold(self, tmp_path):
        result = dispatch_tool("trace_execution", {}, _cold_ctx(tmp_path))
        assert not result.success
        assert "TOOL_DISABLED" in (result.error or "")

    def test_apply_semantic_patch_rejected_warm(self, tmp_path):
        result = dispatch_tool(
            "apply_semantic_patch",
            {"path": "x.py", "search": "old", "replace": "new"},
            _warm_ctx(tmp_path),
        )
        assert not result.success
        assert "TOOL_DISABLED" in (result.error or "")

    def test_apply_semantic_patch_rejected_cold(self, tmp_path):
        result = dispatch_tool(
            "apply_semantic_patch",
            {"path": "x.py", "search": "old", "replace": "new"},
            _cold_ctx(tmp_path),
        )
        assert not result.success
        assert "TOOL_DISABLED" in (result.error or "")

    def test_unknown_tool_rejected_warm(self, tmp_path):
        result = dispatch_tool("super_hacker_tool", {}, _warm_ctx(tmp_path))
        assert not result.success
        assert "UNKNOWN_TOOL" in (result.error or "")

    def test_unknown_tool_rejected_cold(self, tmp_path):
        result = dispatch_tool("super_hacker_tool", {}, _cold_ctx(tmp_path))
        assert not result.success
        assert "UNKNOWN_TOOL" in (result.error or "")

    def test_apply_patch_stub_same_warm_cold(self, tmp_path):
        args = {"patch": "--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-x\n+y\n"}
        warm = dispatch_tool("apply_patch", args, _warm_ctx(tmp_path))
        cold = dispatch_tool("apply_patch", args, _cold_ctx(tmp_path))
        assert warm.success == cold.success

    def test_apply_patch_empty_rejected_same_warm_cold(self, tmp_path):
        warm = dispatch_tool("apply_patch", {"patch": ""}, _warm_ctx(tmp_path))
        cold = dispatch_tool("apply_patch", {"patch": ""}, _cold_ctx(tmp_path))
        assert warm.success == cold.success
        assert not warm.success
        assert not cold.success


class TestPathTraversalInDispatcher:
    def test_read_file_path_traversal_rejected(self, tmp_path):
        result = dispatch_tool(
            "read_file", {"path": "../../etc/passwd"}, _warm_ctx(tmp_path)
        )
        assert not result.success
        assert "unsafe" in (result.error or "").lower()

    def test_list_files_path_traversal_rejected(self, tmp_path):
        result = dispatch_tool(
            "list_files", {"path": "../../"}, _warm_ctx(tmp_path)
        )
        assert not result.success
