"""Unified tool dispatcher — single schema for warm and cold executor.

Both the warm (cached sandbox) and cold (fresh sandbox) execution paths
**must** produce results shaped as ``ToolResult``.

Current integration status
--------------------------
This module provides:

* ``ToolResult`` — the stable result schema that all executor responses
  must eventually conform to.
* ``ExecutionContext`` — per-dispatch metadata.
* ``dispatch_tool`` — a kernel-layer dispatcher used by tests and the
  kernel bridge layer for read-only and stub tools.

The full service execution path (Docker sandbox, warm/cold paths) still
flows through ``services/orchestrator/kernel_bridge.py →
executor_client.run_step()``.  Wiring the service path through
``dispatch_tool`` is planned for a future phase once sandbox integration
is stable.  Until then, ``dispatch_tool`` serves as:

1. The canonical gate for disabled-tool rejection (defence-in-depth).
2. The test harness for registry/dispatcher consistency tests.
3. The target integration point for future warm/cold unification.

Do not bypass ``dispatch_tool`` for disabled tools — even in service code,
the registry's ``enabled`` flag must be checked before executing.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from rfsn_kernel.tool_registry import CANONICAL_TOOLS


# ---------------------------------------------------------------------------
# Result schema
# ---------------------------------------------------------------------------

@dataclass
class ToolResult:
    """Stable result schema for every dispatched tool call."""

    success: bool
    tool: str
    output: Optional[str] = None
    stdout: Optional[str] = None
    stderr: Optional[str] = None
    exit_code: Optional[int] = None
    files_changed: List[str] = field(default_factory=list)
    diff_stats: Dict[str, Any] = field(default_factory=dict)
    timeout: bool = False
    error: Optional[str] = None
    policy_decision_id: Optional[str] = None
    replay_event_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Execution context
# ---------------------------------------------------------------------------

@dataclass
class ExecutionContext:
    """Carries per-dispatch metadata."""

    workspace_root: str
    run_id: str = ""
    iter_count: int = 0
    sandbox_mode: str = "local_dev"
    # dev_mode=True means the caller has accepted unsafe-for-untrusted-code
    # responsibility.  Never set True without explicit configuration.
    dev_mode: bool = False


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def dispatch_tool(
    tool_name: str,
    args: Dict[str, Any],
    context: ExecutionContext,
    *,
    policy_decision_id: Optional[str] = None,
) -> ToolResult:
    """Dispatch *tool_name* with *args* inside *context*.

    Parameters
    ----------
    tool_name:
        Must match an entry in ``CANONICAL_TOOLS``.
    args:
        Tool-specific arguments (already normalized and validated by the
        kernel pipeline before reaching this point).
    context:
        Execution context including workspace root and run metadata.
    policy_decision_id:
        Opaque identifier from the policy layer for audit purposes.

    Returns
    -------
    ``ToolResult`` — always returned; never raises for normal tool errors.
    Raises on programming errors (wrong call-site usage), not tool failures.
    """
    replay_event_id = str(uuid.uuid4())[:8]

    # Defence-in-depth: verify the tool exists and is enabled.
    spec = CANONICAL_TOOLS.get(tool_name)
    if spec is None:
        return ToolResult(
            success=False,
            tool=tool_name,
            error=f"UNKNOWN_TOOL: {tool_name!r} is not in the canonical registry",
            policy_decision_id=policy_decision_id,
            replay_event_id=replay_event_id,
        )
    if not spec.enabled:
        return ToolResult(
            success=False,
            tool=tool_name,
            error=(
                f"TOOL_DISABLED: {tool_name!r} is disabled in the canonical "
                "registry and cannot be executed."
            ),
            policy_decision_id=policy_decision_id,
            replay_event_id=replay_event_id,
        )

    # Route to the appropriate handler.
    handler = _HANDLERS.get(tool_name)
    if handler is None:
        return ToolResult(
            success=False,
            tool=tool_name,
            error=(
                f"NO_HANDLER: {tool_name!r} is enabled in the registry but has "
                "no executor handler.  This is a programming error."
            ),
            policy_decision_id=policy_decision_id,
            replay_event_id=replay_event_id,
        )

    try:
        result = handler(args, context)
    except Exception as exc:  # noqa: BLE001
        result = ToolResult(
            success=False,
            tool=tool_name,
            error=f"HANDLER_EXCEPTION: {exc}",
        )

    result.tool = tool_name
    result.policy_decision_id = policy_decision_id
    result.replay_event_id = replay_event_id
    return result


# ---------------------------------------------------------------------------
# Handlers
# (Minimal stubs for testing.  Real implementations are in the executor
#  service.  These stubs allow the kernel layer to be tested independently.)
# ---------------------------------------------------------------------------

def _handle_read_file(args: Dict[str, Any], ctx: ExecutionContext) -> ToolResult:
    import os

    path = args.get("path", "")
    if not path:
        return ToolResult(success=False, tool="read_file", error="missing path")
    if os.path.isabs(path) or ".." in path.split("/"):
        return ToolResult(success=False, tool="read_file", error="unsafe path")
    full = os.path.join(ctx.workspace_root, path)
    if not os.path.isfile(full):
        return ToolResult(
            success=False, tool="read_file", error=f"file not found: {path}"
        )
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read(65536)
        return ToolResult(success=True, tool="read_file", output=content)
    except OSError as exc:
        return ToolResult(success=False, tool="read_file", error=str(exc))


def _handle_list_files(args: Dict[str, Any], ctx: ExecutionContext) -> ToolResult:
    import fnmatch
    import os

    path = args.get("path", ".") or "."
    if os.path.isabs(path) or ".." in path.split("/"):
        return ToolResult(success=False, tool="list_files", error="unsafe path")
    glob = args.get("glob", "*") or "*"
    max_results = int(args.get("max_results") or 200)
    full = os.path.join(ctx.workspace_root, path)
    if not os.path.isdir(full):
        return ToolResult(
            success=False, tool="list_files", error=f"directory not found: {path}"
        )
    results = []
    for root, _dirs, files in os.walk(full):
        for name in files:
            if fnmatch.fnmatch(name, glob):
                rel = os.path.relpath(os.path.join(root, name), ctx.workspace_root)
                results.append(rel)
                if len(results) >= max_results:
                    break
        if len(results) >= max_results:
            break
    return ToolResult(
        success=True, tool="list_files", output="\n".join(results)
    )


def _handle_apply_patch(args: Dict[str, Any], ctx: ExecutionContext) -> ToolResult:
    """Apply a unified diff patch to the workspace.

    In local/dev mode this calls the real patcher.  The patch must be a
    standard unified diff (``--- a/... +++ b/...``).
    """
    patch = args.get("patch", "")
    if not patch or not patch.strip():
        return ToolResult(
            success=False, tool="apply_patch", error="empty patch rejected"
        )
    if not ctx.workspace_root or not ctx.dev_mode:
        # Stub path: gate check passed, but no real file system write without
        # an explicit workspace root and dev_mode confirmation.
        return ToolResult(
            success=True,
            tool="apply_patch",
            output="[dispatcher stub] patch gate check passed",
        )
    # Real path: apply the unified diff to the workspace.
    try:
        from rfsn_swebench.patcher import apply_unified_diff
        apply_unified_diff(patch, ctx.workspace_root, strict=False)
        return ToolResult(
            success=True,
            tool="apply_patch",
            output="patch applied",
            files_changed=_extract_patched_files(patch),
        )
    except Exception as exc:
        return ToolResult(success=False, tool="apply_patch", error=str(exc))


def _extract_patched_files(patch: str) -> List[str]:
    """Parse ``+++ b/<path>`` lines from a unified diff."""
    files = []
    for line in patch.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            if path.startswith("b/"):
                path = path[2:]
            if path and path != "/dev/null":
                files.append(path)
    return files


def _handle_run_tests(args: Dict[str, Any], ctx: ExecutionContext) -> ToolResult:
    """Run the test suite for the workspace.

    In dev_mode with a valid workspace_root, this calls pytest directly.
    Otherwise falls back to a stub.
    """
    if not ctx.workspace_root or not ctx.dev_mode:
        return ToolResult(
            success=True,
            tool="run_tests",
            output="[dispatcher stub] read-only tool acknowledged",
        )
    import subprocess as _sp

    # Support both canonical template_params form and legacy test_path form.
    template_params = args.get("template_params") or {}
    if isinstance(template_params, str):
        import json
        try:
            template_params = json.loads(template_params)
        except Exception:
            template_params = {}
    test_path = (
        template_params.get("path")
        or args.get("test_path")
        or args.get("path")
        or "tests/"
    )
    timeout_s = int(args.get("timeout_s") or 120)
    try:
        result = _sp.run(
            ["python", "-m", "pytest", str(test_path), "-q", "--tb=short"],
            cwd=ctx.workspace_root,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        success = result.returncode == 0
        return ToolResult(
            success=success,
            tool="run_tests",
            exit_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            output=result.stdout + result.stderr,
        )
    except Exception as exc:
        return ToolResult(success=False, tool="run_tests", error=str(exc))


def _handle_noop_read(args: Dict[str, Any], ctx: ExecutionContext) -> ToolResult:
    """Generic stub for read-only tools that have no local implementation."""
    return ToolResult(
        success=True,
        tool="",
        output="[dispatcher stub] read-only tool acknowledged",
    )


def _handle_disabled(args: Dict[str, Any], ctx: ExecutionContext) -> ToolResult:
    """Should never be reached; defence-in-depth guard."""
    return ToolResult(
        success=False,
        tool="",
        error="TOOL_DISABLED: reached disabled handler — this is a programming error",
    )


# Map of tool_name → handler function.
# Every ENABLED tool in the canonical registry must appear here.
_HANDLERS = {
    "read_file": _handle_read_file,
    "list_files": _handle_list_files,
    "repo_search": _handle_noop_read,
    "repo_read_range": _handle_noop_read,
    "detect_project": _handle_noop_read,
    "detect_workdirs": _handle_noop_read,
    "generate_repo_map": _handle_noop_read,
    "apply_patch": _handle_apply_patch,
    "run_tests": _handle_run_tests,
    "run_cmd_template": _handle_noop_read,
    "format_fix": _handle_noop_read,
    "ensure_deps": _handle_noop_read,
    # Disabled tools — not listed here; the dispatcher checks spec.enabled first.
}
