"""Deterministic repo + failure fingerprinting.

Derives a context key for the Learner from repo
structure (language, framework, test runner) and
failure class (ImportError, AssertionError, etc.).
"""
from __future__ import annotations

import os
import re
from typing import Dict


FRAMEWORK_HINTS: Dict[str, list[str]] = {
    "fastapi": ["fastapi"],
    "django": ["django"],
    "flask": ["flask"],
    "numpy": ["numpy"],
    "pandas": ["pandas"],
    "pytest": ["pytest"],
    "sympy": ["sympy"],
    "scipy": ["scipy"],
    "torch": ["torch", "pytorch"],
    "tensorflow": ["tensorflow"],
}


def _safe_read(
    path: str, limit: int = 200_000,
) -> str:
    try:
        with open(path, "rb") as f:
            return f.read(limit).decode(
                "utf-8", errors="ignore",
            )
    except Exception:
        return ""


def detect_framework(repo_path: str) -> str:
    """Scan manifests for framework keywords."""
    candidates: list[str] = []
    for name in (
        "requirements.txt",
        "pyproject.toml",
        "Pipfile",
        "setup.cfg",
        "setup.py",
    ):
        p = os.path.join(repo_path, name)
        txt = _safe_read(p).lower()
        if txt:
            candidates.append(txt)

    blob = "\n".join(candidates)
    for fw, keys in FRAMEWORK_HINTS.items():
        for k in keys:
            if k in blob:
                return fw
    return "unknown"


def detect_tests(repo_path: str) -> str:
    """Detect test runner from config files."""
    if os.path.exists(
        os.path.join(repo_path, "pytest.ini"),
    ):
        return "pytest"
    if os.path.exists(
        os.path.join(repo_path, "tox.ini"),
    ):
        return "pytest"
    if os.path.exists(
        os.path.join(repo_path, "setup.cfg"),
    ):
        cfg = _safe_read(
            os.path.join(repo_path, "setup.cfg"),
        )
        if "pytest" in cfg.lower():
            return "pytest"
        if "nose" in cfg.lower():
            return "nose"
    if os.path.exists(
        os.path.join(repo_path, "nose.cfg"),
    ):
        return "nose"
    return "pytest"


def detect_lang(repo_path: str) -> str:
    """Detect primary language (simple scan)."""
    for root, _, files in os.walk(repo_path):
        # skip hidden dirs
        if "/." in root:
            continue
        for f in files:
            if f.endswith(".py"):
                return "py"
            if f.endswith(".js"):
                return "js"
            if f.endswith(".ts"):
                return "ts"
            if f.endswith(".rs"):
                return "rs"
            if f.endswith(".go"):
                return "go"
    return "unknown"


def failure_class(fail_text: str) -> str:
    """Extract error class from failure text."""
    if not fail_text:
        return "none"
    m = re.search(
        r"([A-Za-z]+(?:Error|Exception))",
        fail_text,
    )
    if m:
        return m.group(1)
    return "unknown"


def build_context(
    repo_path: str,
    fail_text: str,
) -> dict:
    """Build deterministic context dict."""
    return {
        "lang": detect_lang(repo_path),
        "framework": detect_framework(
            repo_path,
        ),
        "tests": detect_tests(repo_path),
        "failure": failure_class(fail_text),
    }
