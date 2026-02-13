"""Risk-gating for proposed patches.

Enforces SWE-bench-appropriate safety constraints: blocks degenerate patches
that skip tests, delete assertions, touch CI/deps configs, or are excessively
large.  When RFSN policy files are available, limits are loaded from them so
the gate stays consistent with the existing Tool Gateway and diff_guard policy.

The ``diff_analysis`` helper is intentionally a standalone pure function so
that services/tool_gateway can import it to share logic without duplicating
the diff-parsing code.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import yaml  # type: ignore[import-untyped]

from .contracts import RiskReport

# ---------------------------------------------------------------------------
# Defaults (overridden by policy files when available)
# ---------------------------------------------------------------------------
_DEFAULT_BANNED_PATTERNS: List[str] = [
    r"pytest\.skip",
    r"unittest\.skip",
    r"@skip",
    r"@pytest\.mark\.skip",
    r"xfail",
    r"remove\s+test",
    r"disable\s+test",
]

# Patterns that are only suspicious in test files, not in source code.
# Many legitimate source-code fixes use these standard library APIs.
_TEST_ONLY_BANNED_PATTERNS: List[str] = [
    r"__import__\s*\(",
    r"eval\s*\(",
    r"exec\s*\(",
    r"subprocess\.",
    r"os\.system\(",
]

_DEFAULT_BANNED_PATH_FRAGMENTS: List[str] = [
    ".github/workflows/",
    "ci/",
    ".circleci/",
    "Jenkinsfile",
    "requirements.txt",
    "pyproject.toml",
    "Dockerfile",
    "docker-compose",
    ".env",
]


# ---------------------------------------------------------------------------
# Diff analysis — pure function, shareable with tool_gateway
# ---------------------------------------------------------------------------
@dataclass
class DiffStats:
    """Metrics extracted from a unified diff."""

    size_bytes: int = 0
    files_touched: List[str] = field(default_factory=list)
    new_files: int = 0
    added_lines: int = 0
    deleted_lines: int = 0
    deleted_test_lines: int = 0
    plus_lines: List[str] = field(default_factory=list)
    # Added lines grouped by whether they belong to test files
    plus_lines_test: List[str] = field(default_factory=list)
    plus_lines_source: List[str] = field(default_factory=list)


def diff_analysis(unified_diff: str) -> DiffStats:
    """Parse a unified diff and return structural statistics.

    This is a pure function with no side-effects — safe to call from any
    context (bench gate, tool-gateway, tests).
    """
    stats = DiffStats()
    stats.size_bytes = len(unified_diff.encode("utf-8", errors="replace"))

    files_set: set[str] = set()
    current_file: str = ""
    for line in unified_diff.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                current_file = parts[2].replace("a/", "", 1)
                files_set.add(current_file)
        if line.startswith("+++ b/"):
            current_file = line[6:]
        if line.startswith("new file mode"):
            stats.new_files += 1
        if line.startswith("+") and not line.startswith("+++"):
            stats.added_lines += 1
            stats.plus_lines.append(line[1:])
            # Classify by file type for targeted pattern checks
            is_test_file = "test" in current_file.lower()
            if is_test_file:
                stats.plus_lines_test.append(line[1:])
            else:
                stats.plus_lines_source.append(line[1:])
        elif line.startswith("-") and not line.startswith("---"):
            stats.deleted_lines += 1
            if "test" in line.lower():
                stats.deleted_test_lines += 1

    stats.files_touched = sorted(files_set)
    return stats


# ---------------------------------------------------------------------------
# Policy loader
# ---------------------------------------------------------------------------

def load_policies(
    diff_guard_path: str = "/policies/diff_guard.yaml",
    allowlist_path: str = "/policies/tool_allowlist.yaml",
) -> Dict:
    """Load RFSN policy files if they exist; return a merged config dict."""
    cfg: Dict = {
        "max_patch_bytes": 250_000,
        "max_files_touched": 25,
        "max_new_files": 5,
        "max_added_lines": 0,       # 0 = no limit
        "max_deleted_lines": 0,     # 0 = no limit
        "blocked_dependency_files": [],
        "banned_patterns": list(_DEFAULT_BANNED_PATTERNS),
        "banned_path_fragments": list(_DEFAULT_BANNED_PATH_FRAGMENTS),
    }

    if os.path.isfile(diff_guard_path):
        try:
            with open(diff_guard_path, "r", encoding="utf-8") as f:
                dg = yaml.safe_load(f) or {}
            if dg.get("max_changed_files"):
                cfg["max_files_touched"] = int(dg["max_changed_files"])
            if dg.get("max_added_lines"):
                cfg["max_added_lines"] = int(dg["max_added_lines"])
            if dg.get("max_deleted_lines"):
                cfg["max_deleted_lines"] = int(dg["max_deleted_lines"])
            if dg.get("blocked_dependency_files"):
                cfg["blocked_dependency_files"] = list(
                    dg["blocked_dependency_files"]
                )
        except Exception:
            pass  # fall back to defaults

    if os.path.isfile(allowlist_path):
        try:
            with open(allowlist_path, "r", encoding="utf-8") as f:
                al = yaml.safe_load(f) or {}
            if al.get("max_patch_bytes"):
                cfg["max_patch_bytes"] = int(al["max_patch_bytes"])
        except Exception:
            pass

    return cfg


# Module-level config — loaded lazily, can be reloaded.
_POLICIES: Optional[Dict] = None


def _get_policies() -> Dict:
    global _POLICIES
    if _POLICIES is None:
        _POLICIES = load_policies()
    return _POLICIES


def reload_policies(
    diff_guard_path: str = "/policies/diff_guard.yaml",
    allowlist_path: str = "/policies/tool_allowlist.yaml",
) -> Dict:
    """Force-reload policy files.  Useful for long-running processes."""
    global _POLICIES
    _POLICIES = load_policies(diff_guard_path, allowlist_path)
    return _POLICIES


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def patch_risk_gate(
    unified_diff: str,
    max_bytes: Optional[int] = None,
    max_files: Optional[int] = None,
    max_new_files: Optional[int] = None,
) -> RiskReport:
    """Evaluate a unified diff for risk.  Returns ALLOW or REJECT."""
    pol = _get_policies()

    max_bytes = max_bytes if max_bytes is not None else pol["max_patch_bytes"]
    if max_files is None:
        max_files = int(pol["max_files_touched"])
    if max_new_files is None:
        max_new_files = int(pol["max_new_files"])
    max_added = pol.get("max_added_lines", 0)
    max_deleted = pol.get("max_deleted_lines", 0)

    reasons: List[str] = []

    # --- Structured diff analysis ---
    stats = diff_analysis(unified_diff)

    # --- size ---
    if stats.size_bytes > max_bytes:
        reasons.append(
            f"patch too large: {stats.size_bytes}"
            f" bytes > {max_bytes}"
        )

    # --- files ---
    if len(stats.files_touched) > max_files:
        reasons.append(
            f"too many files touched: "
            f"{len(stats.files_touched)} > {max_files}"
        )
    if stats.new_files > max_new_files:
        reasons.append(
            f"too many new files: "
            f"{stats.new_files} > {max_new_files}"
        )

    # --- blocked paths (from policy + defaults) ---
    blocked_deps = set(pol.get("blocked_dependency_files", []))
    for fpath in stats.files_touched:
        base = os.path.basename(fpath)
        if base in blocked_deps:
            reasons.append(f"touches blocked dependency file: {fpath}")
        banned_frags = pol.get(
            "banned_path_fragments",
            _DEFAULT_BANNED_PATH_FRAGMENTS,
        )
        for frag in banned_frags:
            if frag in fpath:
                reasons.append(
                    f"touches restricted path: "
                    f"{fpath} (matched {frag})"
                )

    # --- line-count limits ---
    if max_added and stats.added_lines > max_added:
        reasons.append(
            f"too many added lines: "
            f"{stats.added_lines} > {max_added}"
        )
    if max_deleted and stats.deleted_lines > max_deleted:
        reasons.append(
            f"too many deleted lines: "
            f"{stats.deleted_lines} > {max_deleted}"
        )

    # --- banned patterns in added code ---
    # Universal bans (test-skip, xfail, etc.) apply everywhere
    plus_blob = "\n".join(stats.plus_lines)
    for pat in pol.get("banned_patterns", _DEFAULT_BANNED_PATTERNS):
        if re.search(pat, plus_blob, flags=re.IGNORECASE):
            reasons.append(f"banned pattern in added code: {pat}")

    # Test-only bans (subprocess, eval, exec, etc.) only in test files
    if stats.plus_lines_test:
        test_blob = "\n".join(stats.plus_lines_test)
        for pat in _TEST_ONLY_BANNED_PATTERNS:
            if re.search(pat, test_blob, flags=re.IGNORECASE):
                reasons.append(f"banned pattern in test code: {pat}")

    # --- heuristic: large test deletion ---
    if stats.deleted_test_lines > 50:
        reasons.append(
            "large test deletions: "
            f"{stats.deleted_test_lines} lines"
        )

    if reasons:
        return RiskReport(decision="REJECT", reasons=reasons)
    return RiskReport(decision="ALLOW", reasons=[])
