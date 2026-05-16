"""Unified tool dispatcher — single path for warm and cold executor.

Both the warm (cached sandbox) and cold (fresh sandbox) execution paths
**must** call ``dispatch_tool`` after policy validation.  There must be no
divergent per-path tool behaviour.

Design
------
* ``dispatch_tool`` performs a final registry check before executing.
* Disabled tools fail closed here regardless of how they reached the dispatcher.
* The caller is responsible for kernel/gateway policy validation *before*
  calling this function.  The dispatcher adds a defence-in-depth check only.

``ToolResult``
--------------
Every tool response, whether from the warm or cold path, is wrapped in
``ToolResult``.  The schema is stable — consumers must not rely on fields
outside this dataclass.
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
    """Stub: real implementation is in the executor service (with patch gate)."""
    patch = args.get("patch", "")
    if not patch or not patch.strip():
        return ToolResult(
            success=False, tool="apply_patch", error="empty patch rejected"
        )
    return ToolResult(
        success=True,
        tool="apply_patch",
        output="[dispatcher stub] patch gate check passed",
    )


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
    "run_tests": _handle_noop_read,
    "run_cmd_template": _handle_noop_read,
    "format_fix": _handle_noop_read,
    "ensure_deps": _handle_noop_read,
    # Disabled tools — not listed here; the dispatcher checks spec.enabled first.
}
