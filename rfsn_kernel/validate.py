"""Proposal validation — hard bounds + action envelope.

Validates that a normalized proposal meets all
constraints before it reaches simulation or execution.
This is the FIRST gate — schema, bounds, content bans.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Set, Tuple

from rfsn_kernel.state import Proposal, SystemState
from rfsn_kernel.tool_registry import CANONICAL_TOOL_NAMES, CANONICAL_TOOLS


@dataclass
class ValidationResult:
    ok: bool
    errors: List[Dict[str, Any]] = field(default_factory=list)


# Single source of truth: derived from the canonical tool registry.
# Do NOT add tool names here directly — edit tool_registry.py instead.
VALID_ACTIONS: Set[str] = set(CANONICAL_TOOL_NAMES)

# Tools flagged safe=False in the registry are blocked unless the policy
# explicitly enables them via the "allow_unsafe_tools" list.
_UNSAFE_TOOLS: Set[str] = {
    name for name, spec in CANONICAL_TOOLS.items() if not spec.safe
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

_DEP_MANIFESTS = {
    "pyproject.toml",
    "poetry.lock",
    "requirements.txt",
    "requirements-dev.txt",
    "requirements.in",
    "constraints.txt",
    "setup.py",
    "setup.cfg",
    "pipfile",
    "pipfile.lock",
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "go.mod",
    "go.sum",
    "cargo.toml",
    "cargo.lock",
}


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


def _extract_patch_touched_paths(patch_text: str) -> Set[str]:
    touched: Set[str] = set()
    for raw in (patch_text or "").splitlines():
        line = raw.strip()
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                for part in (parts[2], parts[3]):
                    p = part
                    if p.startswith("a/") or p.startswith("b/"):
                        p = p[2:]
                    p = p.lstrip("/")
                    if p and p != "/dev/null":
                        touched.add(p)
            continue
        if line.startswith("+++ ") or line.startswith("--- "):
            parts = line.split(maxsplit=1)
            if len(parts) != 2:
                continue
            p = parts[1].strip()
            if p in {"a/dev/null", "b/dev/null", "/dev/null"}:
                continue
            if p.startswith("a/") or p.startswith("b/"):
                p = p[2:]
            p = p.lstrip("/")
            if p:
                touched.add(p)
    return touched


def _diff_line_stats(patch_text: str) -> Tuple[int, int, int]:
    added = 0
    deleted = 0
    total = 0
    for line in (patch_text or "").splitlines():
        if line.startswith(
            ("diff --git ", "+++ ", "--- ", "@@"),
        ):
            continue
        if line.startswith("+"):
            added += 1
            total += 1
        elif line.startswith("-"):
            deleted += 1
            total += 1
    return added, deleted, total


def _is_test_path(path: str) -> bool:
    p = (path or "").replace("\\", "/").lstrip("/")
    return (
        p.startswith("tests/")
        or "/tests/" in f"/{p}"
        or p.startswith("test/")
        or p.endswith("_test.py")
        or p.endswith("test.py")
    )


def _is_ci_path(path: str) -> bool:
    p = (path or "").replace("\\", "/").lstrip("/")
    return (
        p.startswith(".github/workflows/")
        or p.startswith("ci/")
        or p.startswith("scripts/")
    )


def _is_dep_manifest(path: str) -> bool:
    p = (path or "").replace("\\", "/").lstrip("/")
    base = p.split("/")[-1].lower()
    return base in _DEP_MANIFESTS


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

    # 1b. Unsafe tools are blocked unless explicitly allowed by policy.
    if proposal.action in _UNSAFE_TOOLS:
        allowed_unsafe_set = set(str(t) for t in (policy.get("allow_unsafe_tools") or []))
        if proposal.action not in allowed_unsafe_set:
            errors.append({
                "code": "UNSAFE_TOOL_BLOCKED",
                "msg": (
                    f"Tool '{proposal.action}' is not safe and is not listed"
                    " in policy allow_unsafe_tools"
                ),
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
            touched = _extract_patch_touched_paths(patch)
            max_patch_files = int(
                policy.get("max_patch_files", 0)
                or 0
            )
            max_patch_total_lines = int(
                policy.get("max_patch_total_lines", 0)
                or 0
            )
            max_added_lines = int(
                policy.get("max_added_lines", 0)
                or 0
            )
            max_deleted_lines = int(
                policy.get("max_deleted_lines", 0)
                or 0
            )

            if (
                max_patch_files > 0
                and len(touched) > max_patch_files
            ):
                errors.append({
                    "code": "PATCH_TOO_MANY_FILES",
                    "msg": (
                        f"Patch touches {len(touched)} files"
                        f" > {max_patch_files}"
                    ),
                })

            added, deleted, total = _diff_line_stats(patch)
            if (
                max_added_lines > 0
                and added > max_added_lines
            ):
                errors.append({
                    "code": "PATCH_TOO_MANY_ADDED",
                    "msg": (
                        f"Patch adds {added} lines"
                        f" > {max_added_lines}"
                    ),
                })
            if (
                max_deleted_lines > 0
                and deleted > max_deleted_lines
            ):
                errors.append({
                    "code": "PATCH_TOO_MANY_DELETED",
                    "msg": (
                        f"Patch deletes {deleted} lines"
                        f" > {max_deleted_lines}"
                    ),
                })
            if (
                max_patch_total_lines > 0
                and total > max_patch_total_lines
            ):
                errors.append({
                    "code": "PATCH_TOO_LARGE",
                    "msg": (
                        f"Patch changes {total} lines"
                        f" > {max_patch_total_lines}"
                    ),
                })

            if bool(policy.get("forbid_test_edits", False)):
                if any(_is_test_path(p) for p in touched):
                    errors.append({
                        "code": "FORBID_TEST_EDITS",
                        "msg": (
                            "Patch touches test paths while"
                            " forbid_test_edits=true"
                        ),
                    })
            if bool(policy.get("forbid_ci_edits", False)):
                if any(_is_ci_path(p) for p in touched):
                    errors.append({
                        "code": "FORBID_CI_EDITS",
                        "msg": (
                            "Patch touches CI paths while"
                            " forbid_ci_edits=true"
                        ),
                    })
            if bool(policy.get("forbid_dep_manifest_edits", False)):
                if any(_is_dep_manifest(p) for p in touched):
                    errors.append({
                        "code": "FORBID_DEP_MANIFEST_EDITS",
                        "msg": (
                            "Patch touches dependency manifests while"
                            " forbid_dep_manifest_edits=true"
                        ),
                    })

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
        allowed = policy.get("allowed_test_templates")
        if isinstance(allowed, list) and allowed:
            allowed_set = set(str(x) for x in allowed)
            if not tmpl or tmpl not in allowed_set:
                errors.append({
                    "code": "UNKNOWN_TEST_TEMPLATE",
                    "msg": (
                        "template_id must be in allowed_test_templates;"
                        f" got {tmpl!r}"
                    ),
                })
        elif not tmpl:
            errors.append({
                "code": "MISSING_TEST_TEMPLATE",
                "msg": "run_tests requires template_id",
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
