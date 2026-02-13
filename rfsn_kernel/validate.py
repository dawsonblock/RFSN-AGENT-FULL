"""Proposal validation — hard bounds + action envelope.

Validates that a normalized proposal meets all
constraints before it reaches simulation or execution.
This is the FIRST gate — schema, bounds, content bans.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List

from rfsn_kernel.state import Proposal, SystemState


@dataclass
class ValidationResult:
    ok: bool
    errors: List[Dict[str, Any]] = field(default_factory=list)


# Known action types.
VALID_ACTIONS = {
    "repo_search", "repo_read_range",
    "read_file", "detect_project",
    "detect_workdirs", "apply_patch",
    "run_tests", "run_cmd_template",
    "format_fix", "ensure_deps",
}

# Banned patterns in patch content (plus-lines only).
_BANNED_PATCH_PATTERNS = [
    r"pytest\.skip", r"unittest\.skip",
    r"@skip", r"@pytest\.mark\.skip",
    r"xfail", r"remove\s+test", r"disable\s+test",
    r"__import__\s*\(", r"eval\s*\(",
    r"exec\s*\(", r"subprocess\.",
    r"os\.system\(",
]


def _blocked_read_path(
    path: str,
    policy: Dict[str, Any],
) -> bool:
    norm = (path or "").replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    norm = norm.lstrip("/")
    prefixes = policy.get("blocked_read_prefixes", []) or []
    suffixes = policy.get("blocked_read_suffixes", []) or []
    for pref in prefixes:
        p = str(pref or "").replace("\\", "/")
        while p.startswith("./"):
            p = p[2:]
        p = p.lstrip("/")
        if p and norm.startswith(p):
            return True
    for suff in suffixes:
        s = str(suff or "")
        if s and norm.endswith(s):
            return True
    return False


def validate(
    proposal: Proposal,
    state: SystemState,
    policy: Dict[str, Any] | None = None,
) -> ValidationResult:
    """Validate a proposal against hard bounds.

    Returns ValidationResult with ok=False if any
    constraint is violated.
    """
    policy = policy or {}
    errors: List[Dict[str, Any]] = []

    # 1. Action type must be known.
    if proposal.action not in VALID_ACTIONS:
        errors.append({
            "code": "UNKNOWN_ACTION",
            "msg": f"Unknown action: {proposal.action}",
        })
        return ValidationResult(ok=False, errors=errors)

    # 2. Safety level lockout.
    if state.safety_level >= 2:
        errors.append({
            "code": "SAFETY_LOCKED",
            "msg": "System is in safety lockdown — no execution allowed",
        })
        return ValidationResult(ok=False, errors=errors)

    # 3. Step budget enforcement.
    max_steps = int(policy.get("max_total_steps", 100))
    if state.step_count >= max_steps:
        errors.append({
            "code": "STEP_BUDGET_EXHAUSTED",
            "msg": f"Total steps {state.step_count} >= {max_steps}",
        })
        return ValidationResult(ok=False, errors=errors)

    # 4. Per-action-type validation.
    if proposal.action == "repo_search":
        pattern = proposal.params.get("pattern", "")
        if len(pattern) > 500:
            errors.append({
                "code": "PATTERN_TOO_LONG",
                "msg": "Search pattern > 500 chars",
            })

    elif proposal.action == "repo_read_range":
        path = proposal.params.get("path", "")
        if not path:
            errors.append({
                "code": "MISSING_PATH",
                "msg": "repo_read_range requires path",
            })
        elif ".." in path or path.startswith("/"):
            errors.append({
                "code": "PATH_TRAVERSAL",
                "msg": f"Unsafe path: {path}",
            })
        elif _blocked_read_path(path, policy):
            errors.append({
                "code": "PATH_BLOCKED_BY_POLICY",
                "msg": f"Path blocked by read policy: {path}",
            })
        ls = int(proposal.params.get("line_start", 1))
        le = int(proposal.params.get("line_end", ls))
        max_lpr = int(policy.get("max_lines_per_read", 300))
        if le - ls + 1 > max_lpr:
            errors.append({
                "code": "READ_RANGE_TOO_LARGE",
                "msg": f"{le - ls + 1} lines > {max_lpr}",
            })

    elif proposal.action == "read_file":
        path = proposal.params.get("path", "")
        if not path:
            errors.append({
                "code": "MISSING_PATH",
                "msg": "read_file requires path",
            })
        elif ".." in path or path.startswith("/"):
            errors.append({
                "code": "PATH_TRAVERSAL",
                "msg": f"Unsafe path: {path}",
            })
        elif _blocked_read_path(path, policy):
            errors.append({
                "code": "PATH_BLOCKED_BY_POLICY",
                "msg": f"Path blocked by read policy: {path}",
            })

    elif proposal.action == "detect_workdirs":
        depth = int(proposal.params.get("max_depth", 4))
        if depth < 1 or depth > 8:
            errors.append({
                "code": "INVALID_DEPTH",
                "msg": f"max_depth out of bounds: {depth}",
            })

    elif proposal.action == "apply_patch":
        patch = proposal.params.get("patch", "") or ""
        if not patch.strip():
            errors.append({
                "code": "EMPTY_PATCH",
                "msg": "apply_patch requires non-empty patch",
            })
        else:
            # Content bans.
            plus_lines = [
                line[1:] for line in patch.splitlines()
                if line.startswith("+") and not line.startswith("+++")
            ]
            plus_blob = "\n".join(plus_lines)
            for pat in _BANNED_PATCH_PATTERNS:
                if re.search(pat, plus_blob, re.IGNORECASE):
                    errors.append({
                        "code": "PATCH_CONTENT_BANNED",
                        "msg": f"Banned pattern: {pat}",
                    })

    elif proposal.action == "run_tests":
        tmpl = proposal.params.get("template_id")
        if tmpl and tmpl not in (
            "pytest_targeted", "pytest_suite",
            "ruff_check", "mypy_check",
        ):
            errors.append({
                "code": "UNKNOWN_TEST_TEMPLATE",
                "msg": f"Unknown template: {tmpl}",
            })

    elif proposal.action in {
        "run_cmd_template",
        "format_fix",
    }:
        tmpl = str(
            proposal.params.get("template", "")
            or "",
        ).strip()
        if not tmpl:
            errors.append({
                "code": "MISSING_TEMPLATE",
                "msg": f"{proposal.action} requires template",
            })
        allowed = policy.get("allowed_command_templates")
        if isinstance(allowed, list) and allowed:
            if tmpl not in set(str(x) for x in allowed):
                errors.append({
                    "code": "UNKNOWN_COMMAND_TEMPLATE",
                    "msg": f"Unknown command template: {tmpl}",
                })
        wid = str(
            proposal.params.get("workdir_id", "")
            or "",
        )
        if wid and not re.fullmatch(r"workdir_\d+", wid):
            errors.append({
                "code": "INVALID_WORKDIR_ID",
                "msg": f"Invalid workdir_id: {wid}",
            })
        if proposal.action == "format_fix" and tmpl:
            if not tmpl.endswith("_fix"):
                errors.append({
                    "code": "FORMAT_FIX_TEMPLATE_REQUIRED",
                    "msg": "format_fix requires a *_fix template",
                })

    return ValidationResult(ok=len(errors) == 0, errors=errors)
