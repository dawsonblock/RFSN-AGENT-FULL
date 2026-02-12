from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class TierDecision:
    tier: int
    reason: str


def _glob_any(path: str, globs: List[str]) -> bool:
    for g in globs:
        if fnmatch.fnmatch(path, g):
            return True
        if g.startswith("**/"):
            leaf = g[3:]
            if fnmatch.fnmatch(path, leaf):
                return True
            if ("/" + path).endswith("/" + leaf):
                return True
    return False


def classify_path(path: str, classifiers: Dict[str, List[str]]) -> str:
    p = (path or "").strip()
    if not p:
        return "code"
    if _glob_any(p, classifiers.get("tests_globs", [])):
        return "tests"
    if _glob_any(p, classifiers.get("deps_globs", [])):
        return "deps"
    if _glob_any(p, classifiers.get("ci_globs", [])):
        return "ci"
    return "code"


def _paths_from_patch(patch: str) -> List[str]:
    paths: List[str] = []
    if not patch:
        return paths
    for ln in patch.splitlines():
        if ln.startswith("+++ b/"):
            paths.append(ln[6:].strip())
        elif ln.startswith("--- a/"):
            paths.append(ln[6:].strip())
    if paths:
        return paths
    # Fallback for unusual diff headers.
    rx = re.compile(r"^diff --git a/(.+?) b/(.+)$")
    for ln in patch.splitlines():
        m = rx.match(ln)
        if m:
            paths.extend([m.group(1).strip(), m.group(2).strip()])
    return paths


def step_touches(step: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    p = step.get("path")
    if isinstance(p, str):
        out.append(p)

    ps = step.get("paths")
    if isinstance(ps, list):
        out.extend(x for x in ps if isinstance(x, str))

    fs = step.get("files")
    if isinstance(fs, list):
        for f in fs:
            if isinstance(f, dict) and isinstance(f.get("path"), str):
                out.append(f["path"])
            elif isinstance(f, str):
                out.append(f)

    patch = step.get("patch")
    if isinstance(patch, str):
        out.extend(_paths_from_patch(patch))

    # preserve order, dedupe
    seen = set()
    deduped: List[str] = []
    for x in out:
        if x not in seen:
            seen.add(x)
            deduped.append(x)
    return deduped


def tier_allows_step(
    step: Dict[str, Any],
    tier_cfg: Dict[str, Any],
    classifiers: Dict[str, List[str]],
) -> Tuple[bool, Optional[str]]:
    allow = tier_cfg.get("allow", {})
    touched = step_touches(step)
    for path in touched:
        kind = classify_path(path, classifiers)
        if kind == "tests" and not allow.get("edit_tests", False):
            return False, f"tier forbids tests edits: {path}"
        if kind == "deps" and not allow.get("edit_deps", False):
            return False, f"tier forbids deps edits: {path}"
        if kind == "ci" and not allow.get("edit_ci", False):
            return False, f"tier forbids CI edits: {path}"
    return True, None


def pick_next_tier(
    current: int,
    failure_kinds: List[str],
    policy: Dict[str, Any],
) -> TierDecision:
    rules = policy.get("escalation_rules", {})

    kinds = set(failure_kinds or [])

    def has(kind: str) -> bool:
        return kind in kinds

    if current < 1:
        for r in rules.get("to_tier_1", {}).get("requires_any", []):
            if has(r.get("failure_kind", "")):
                return TierDecision(
                    1, f"escalate->1 due to {r['failure_kind']}",
                )
    if current < 2:
        for r in rules.get("to_tier_2", {}).get("requires_any", []):
            if has(r.get("failure_kind", "")):
                return TierDecision(
                    2, f"escalate->2 due to {r['failure_kind']}",
                )
    if current < 3:
        for r in rules.get("to_tier_3", {}).get("requires_any", []):
            if has(r.get("failure_kind", "")):
                return TierDecision(
                    3, f"escalate->3 due to {r['failure_kind']}",
                )
    return TierDecision(current, "no escalation")
