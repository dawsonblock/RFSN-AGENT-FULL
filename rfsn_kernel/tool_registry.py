"""Canonical tool registry — single source of truth for tool names and params.

Current consumers
-----------------
* ``rfsn_kernel/validate.py`` — derives ``VALID_ACTIONS`` and the unsafe-tool
  gate from this registry.
* ``rfsn_kernel/normalize.py`` — derives ``_ALLOWED_PARAMS`` from
  ``ToolSpec.allowed_params`` so the two contracts stay in sync.
* Other services may mirror or consume this registry indirectly, but the
  current tree should not be read as guaranteeing runtime enforcement in
  ``services/tool_gateway/app.py`` or ``services/executor/app.py``.

Design notes
------------
* ``ToolSpec.enabled`` is the master on/off switch.  Disabled tools **must**
  fail closed with a clear error at every layer; they must never silently no-op.
* ``ToolSpec.safe`` marks tools whose implementation is verified as
  non-shell, non-dynamic.  The kernel adds extra gating for unsafe ones.
* ``ToolSpec.allowed_params`` is the authoritative list of parameters that
  may survive the normalize → validate pipeline.  Any extra key the planner
  emits will be silently stripped by ``normalize()``.
* ``ToolSpec.requires_patch_gate`` — both ``apply_patch`` and
  ``apply_semantic_patch`` must pass through the patch risk gate.
* ``ToolSpec.requires_sandbox`` — tools that execute code must run inside a
  sandbox or explicit dev-mode; the executor enforces this.
* ``ToolSpec.description`` is human-readable only — not enforced by policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Optional


@dataclass(frozen=True)
class ToolSpec:
    """Static descriptor for a single canonical tool."""

    name: str
    description: str
    # True if the implementation is verified safe (no shell, no dynamic exec).
    safe: bool = True
    # False = tool is disabled; validation must reject it.
    enabled: bool = True
    # Qualitative risk level: "low" | "medium" | "high"
    risk_level: str = "low"
    # Whether the tool may be used in the warm (cached) executor path.
    allowed_in_warm_path: bool = True
    # Whether the tool may be used in the cold (fresh) executor path.
    allowed_in_cold_path: bool = True
    # True = executor must use sandbox (Docker or explicit dev mode).
    requires_sandbox: bool = False
    # True = tool must pass through the patch risk gate before execution.
    requires_patch_gate: bool = False
    # Optional hard limit on total input bytes for this tool.
    max_input_size: Optional[int] = None
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
        enabled=True,
        risk_level="low",
        allowed_params=frozenset({"pattern", "timeout_s", "max_results", "path_glob"}),
    ),
    "repo_read_range": ToolSpec(
        name="repo_read_range",
        description="Read a line range from a repo file.",
        safe=True,
        enabled=True,
        risk_level="low",
        allowed_params=frozenset({"path", "line_start", "line_end", "timeout_s"}),
    ),
    "read_file": ToolSpec(
        name="read_file",
        description="Read an entire file.",
        safe=True,
        enabled=True,
        risk_level="low",
        allowed_params=frozenset({"path", "timeout_s"}),
    ),
    "list_files": ToolSpec(
        name="list_files",
        description="List files under a directory.",
        safe=True,
        enabled=True,
        risk_level="low",
        allowed_params=frozenset({"path", "glob", "max_results"}),
    ),
    "detect_project": ToolSpec(
        name="detect_project",
        description="Detect project type and structure.",
        safe=True,
        enabled=True,
        risk_level="low",
        allowed_params=frozenset({"path", "timeout_s"}),
    ),
    "detect_workdirs": ToolSpec(
        name="detect_workdirs",
        description="Detect working directories in a monorepo.",
        safe=True,
        enabled=True,
        risk_level="low",
        allowed_params=frozenset({"max_depth", "timeout_s"}),
    ),
    # ── Mutation tools ──────────────────────────────────────────────────────
    "apply_patch": ToolSpec(
        name="apply_patch",
        description="Apply a unified diff patch to the repo.",
        safe=True,
        enabled=True,
        risk_level="high",
        requires_patch_gate=True,
        allowed_params=frozenset({"patch", "timeout_s"}),
    ),
    "apply_semantic_patch": ToolSpec(
        name="apply_semantic_patch",
        description=(
            "Apply an exact SEARCH/REPLACE semantic patch. "
            "Disabled until Phase 3 validation is complete. "
            "Enable only after all tests/test_semantic_patch_safety.py pass."
        ),
        safe=True,
        # DISABLED: enable only after Phase 3 is verified.
        enabled=False,
        risk_level="high",
        requires_patch_gate=True,
        allowed_params=frozenset({"path", "search", "replace", "timeout_s"}),
    ),
    # ── Execution tools ─────────────────────────────────────────────────────
    "run_tests": ToolSpec(
        name="run_tests",
        description="Run tests via an allowlisted template.",
        safe=True,
        enabled=True,
        risk_level="medium",
        requires_sandbox=True,
        allowed_params=frozenset({
            "template_id", "template_params", "workdir_id", "timeout_s",
        }),
    ),
    "run_cmd_template": ToolSpec(
        name="run_cmd_template",
        description="Run an allowlisted command template (non-shell).",
        safe=True,
        enabled=True,
        risk_level="medium",
        requires_sandbox=True,
        allowed_params=frozenset({"template", "workdir_id", "timeout_s", "env"}),
    ),
    "format_fix": ToolSpec(
        name="format_fix",
        description="Auto-format files using an allowlisted formatter.",
        safe=True,
        enabled=True,
        risk_level="low",
        allowed_params=frozenset({"template", "workdir_id", "timeout_s"}),
    ),
    "ensure_deps": ToolSpec(
        name="ensure_deps",
        description="Ensure project dependencies are installed.",
        safe=True,
        enabled=True,
        risk_level="medium",
        allowed_params=frozenset({"manifest", "workdir_id", "timeout_s"}),
    ),
    # ── Introspection / analysis tools ─────────────────────────────────────
    "generate_repo_map": ToolSpec(
        name="generate_repo_map",
        description="Generate a structural map of the repository.",
        safe=True,
        enabled=True,
        risk_level="low",
        allowed_params=frozenset({"path", "max_depth", "include_globs"}),
    ),
    "trace_execution": ToolSpec(
        name="trace_execution",
        description=(
            "DISABLED — unsafe until rewritten. "
            "Previous implementation used os.system() with agent-controlled "
            "command strings.  Do not enable until a fully audited, "
            "structured (non-shell) implementation is provided."
        ),
        # Marked unsafe: implementation used shell execution.
        safe=False,
        # Disabled: must fail closed at every layer.
        enabled=False,
        risk_level="high",
        requires_sandbox=True,
        allowed_params=frozenset({"path", "entry_point", "args", "timeout_s"}),
    ),
}

# Convenience set of all canonical tool names (used by validate.py).
CANONICAL_TOOL_NAMES: FrozenSet[str] = frozenset(CANONICAL_TOOLS.keys())

# Enabled tool names only (used by gateway/executor routing validation).
ENABLED_TOOL_NAMES: FrozenSet[str] = frozenset(
    name for name, spec in CANONICAL_TOOLS.items() if spec.enabled
)

# Tools that require the patch risk gate.
PATCH_GATE_TOOLS: FrozenSet[str] = frozenset(
    name for name, spec in CANONICAL_TOOLS.items() if spec.requires_patch_gate
)

# Tools that require a sandbox.
SANDBOX_TOOLS: FrozenSet[str] = frozenset(
    name for name, spec in CANONICAL_TOOLS.items() if spec.requires_sandbox
)
