from __future__ import annotations

from typing import Any, Dict, List, Optional


def _has_marker(workdir: Dict[str, Any], markers: set[str]) -> bool:
    ws = workdir.get("markers")
    if not isinstance(ws, list):
        return False
    return any(isinstance(m, str) and m in markers for m in ws)


def _preferred_workdir(workdirs: List[Dict[str, Any]]) -> Optional[str]:
    for w in workdirs:
        if w.get("rel") == "." and isinstance(w.get("id"), str):
            return w["id"]
    for w in workdirs:
        wid = w.get("id")
        if isinstance(wid, str) and wid:
            return wid
    return None


def infer_commands(
    project_profile: Dict[str, Any],
    workdirs: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Deterministically choose command templates and default workdir.

    This layer is intentionally rule-based so replay behavior stays stable.
    """
    has_python = bool(project_profile.get("has_python", False))
    has_node = bool(project_profile.get("has_node", False))
    has_go = bool(project_profile.get("has_go", False))
    has_rust = bool(project_profile.get("has_rust", False))
    has_make = bool(project_profile.get("has_make", False))

    preferred = _preferred_workdir(workdirs)

    test_templates: List[str] = []
    lint_templates: List[str] = []
    build_templates: List[str] = []

    if has_python:
        test_templates.extend(["python:pytest", "python:unittest"])
        lint_templates.append("python:ruff")

    if has_node:
        test_templates.append("node:test")
        lint_templates.append("node:lint")
        build_templates.append("tsc")

    if has_go:
        test_templates.append("go:test")

    if has_rust:
        test_templates.append("rust:test")

    if has_make:
        test_templates.append("make:test")

    # Workdir hints: if preferred workdir has clear markers, bias command order.
    marker_boost = {}
    for w in workdirs:
        if w.get("id") == preferred:
            marker_boost["python"] = _has_marker(
                w,
                {"pyproject.toml", "requirements.txt", "setup.py", "setup.cfg"},
            )
            marker_boost["node"] = _has_marker(
                w,
                {"package.json", "pnpm-lock.yaml", "yarn.lock", "package-lock.json"},
            )
            marker_boost["go"] = _has_marker(w, {"go.mod"})
            marker_boost["rust"] = _has_marker(w, {"Cargo.toml"})
            break

    # If the preferred workdir looks strongly typed, put matching templates first.
    if marker_boost.get("node") and "node:test" in test_templates:
        test_templates = ["node:test"] + [
            x for x in test_templates if x != "node:test"
        ]
    if marker_boost.get("python") and "python:pytest" in test_templates:
        test_templates = ["python:pytest"] + [
            x for x in test_templates if x != "python:pytest"
        ]

    return {
        "workdir_id": preferred,
        "test_templates": test_templates,
        "lint_templates": lint_templates,
        "build_templates": build_templates,
    }
