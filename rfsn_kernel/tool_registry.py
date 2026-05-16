"""Canonical tool registry — single source of truth for tool names and params.

Current consumers
-----------------
* ``rfsn_kernel/validate.py`` — derives ``VALID_ACTIONS`` and the unsafe-tool
  gate from this registry.
* ``rfsn_kernel/normalize.py`` — derives ``_ALLOWED_PARAMS`` from
  ``ToolSpec.allowed_params`` so the two contracts stay in sync.

Pending wiring
--------------
* ``services/tool_gateway/app.py`` — still reads ``allowed_step_types`` from
  ``policies/tool_allowlist.yaml``.  That YAML must be kept manually in sync
  with ``CANONICAL_TOOLS`` until a follow-on PR wires the gateway directly.

Design notes
------------
* ``ToolSpec.safe`` marks tools whose implementation is verified as
  non-shell, non-dynamic; the kernel may add extra gating for unsafe ones.
* ``ToolSpec.allowed_params`` is the authoritative list of parameters that
  may survive the normalize → validate pipeline.  Any extra key the planner
  emits will be silently stripped by ``normalize()``.
* ``ToolSpec.description`` is human-readable only — not enforced by policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet


@dataclass(frozen=True)
class ToolSpec:
    """Static descriptor for a single canonical tool."""

    name: str
    description: str
    # True if the implementation is verified safe (no shell, no dynamic exec).
    safe: bool = True
    # Parameter names allowed through the validate/normalize layer.
    allowed_params: FrozenSet[str] = field(default_factory=frozenset)


# ---------------------------------------------------------------------------
# Canonical registry
# Every tool that any layer of the system may see MUST appear here.
# ---------------------------------------------------------------------------

CANONICAL_TOOLS: Dict[str, ToolSpec] = {
    # ── Read-only repo tools ────────────────────────────────────────────────
    "repo_search": ToolSpec(
        name="repo_search",
        description="Search the repository for a pattern.",
        safe=True,
        allowed_params=frozenset({"pattern", "timeout_s", "max_results", "path_glob"}),
    ),
    "repo_read_range": ToolSpec(
        name="repo_read_range",
        description="Read a line range from a repo file.",
        safe=True,
        allowed_params=frozenset({"path", "line_start", "line_end", "timeout_s"}),
    ),
    "read_file": ToolSpec(
        name="read_file",
        description="Read an entire file.",
        safe=True,
        allowed_params=frozenset({"path", "timeout_s"}),
    ),
    "list_files": ToolSpec(
        name="list_files",
        description="List files under a directory.",
        safe=True,
        allowed_params=frozenset({"path", "glob", "max_results"}),
    ),
    "detect_project": ToolSpec(
        name="detect_project",
        description="Detect project type and structure.",
        safe=True,
        allowed_params=frozenset({"path", "timeout_s"}),
    ),
    "detect_workdirs": ToolSpec(
        name="detect_workdirs",
        description="Detect working directories in a monorepo.",
        safe=True,
        allowed_params=frozenset({"max_depth", "timeout_s"}),
    ),
    # ── Mutation tools ──────────────────────────────────────────────────────
    "apply_patch": ToolSpec(
        name="apply_patch",
        description="Apply a unified diff patch to the repo.",
        safe=True,
        allowed_params=frozenset({"patch", "timeout_s"}),
    ),
    "apply_semantic_patch": ToolSpec(
        name="apply_semantic_patch",
        description=(
            "Apply a SEARCH/REPLACE semantic patch block. "
            "Must produce an actual diff; reports failure if no replacement occurs."
        ),
        # Implementation requires validation that a real replacement happened.
        safe=True,
        allowed_params=frozenset({"path", "search", "replace", "timeout_s"}),
    ),
    # ── Execution tools ─────────────────────────────────────────────────────
    "run_tests": ToolSpec(
        name="run_tests",
        description="Run tests via an allowlisted template.",
        safe=True,
        allowed_params=frozenset({
            "template_id", "template_params", "workdir_id", "timeout_s",
        }),
    ),
    "run_cmd_template": ToolSpec(
        name="run_cmd_template",
        description="Run an allowlisted command template (non-shell).",
        safe=True,
        allowed_params=frozenset({"template", "workdir_id", "timeout_s", "env"}),
    ),
    "format_fix": ToolSpec(
        name="format_fix",
        description="Auto-format files using an allowlisted formatter.",
        safe=True,
        allowed_params=frozenset({"template", "workdir_id", "timeout_s"}),
    ),
    "ensure_deps": ToolSpec(
        name="ensure_deps",
        description="Ensure project dependencies are installed.",
        safe=True,
        allowed_params=frozenset({"manifest", "workdir_id", "timeout_s"}),
    ),
    # ── Introspection / analysis tools ─────────────────────────────────────
    "generate_repo_map": ToolSpec(
        name="generate_repo_map",
        description="Generate a structural map of the repository.",
        safe=True,
        allowed_params=frozenset({"path", "max_depth", "include_globs"}),
    ),
    "trace_execution": ToolSpec(
        name="trace_execution",
        description=(
            "Trace execution of a code path. "
            "NOTE: implementation must not use shell execution or dynamic eval."
        ),
        # Marked unsafe until the implementation is audited and confirmed
        # to use only structured, non-shell, allowlisted execution.
        safe=False,
        allowed_params=frozenset({"path", "entry_point", "args", "timeout_s"}),
    ),
}

# Convenience set of all canonical tool names (used by validate.py).
CANONICAL_TOOL_NAMES: FrozenSet[str] = frozenset(CANONICAL_TOOLS.keys())
