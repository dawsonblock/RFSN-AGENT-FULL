"""Deterministic repo + failure fingerprinting.

Derives a context key for the Learner from repo
structure (language, framework, test runner) and
failure class (ImportError, AssertionError, etc.).

Also provides structured failure parsing for the
failure signature index.
"""
from __future__ import annotations

import hashlib
import os
import re
from typing import Dict, Optional


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
    "requests": ["requests"],
    "matplotlib": ["matplotlib"],
    "scikit-learn": ["scikit-learn", "sklearn"],
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
        "package.json",
        "Cargo.toml",
        "go.mod",
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
    counts: Dict[str, int] = {}
    for root, _, files in os.walk(repo_path):
        # skip hidden dirs
        if "/." in root:
            continue
        for f in files:
            if f.endswith(".py"):
                counts["py"] = counts.get("py", 0) + 1
            elif f.endswith(".js"):
                counts["js"] = counts.get("js", 0) + 1
            elif f.endswith(".ts"):
                counts["ts"] = counts.get("ts", 0) + 1
            elif f.endswith(".rs"):
                counts["rs"] = counts.get("rs", 0) + 1
            elif f.endswith(".go"):
                counts["go"] = counts.get("go", 0) + 1
            elif f.endswith(".java"):
                counts["java"] = counts.get("java", 0) + 1
            elif f.endswith(".rb"):
                counts["rb"] = counts.get("rb", 0) + 1
        # Don't walk entire tree for speed.
        if sum(counts.values()) > 50:
            break
    if not counts:
        return "unknown"
    return max(counts, key=counts.get)  # type: ignore[arg-type]


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


# ── Structured failure parsing ───────────────

def parse_failure_signature(
    fail_text: str,
) -> dict:
    """Parse a failure log into structured fields.

    Returns:
        {
            "failure_class": "ImportError",
            "failure_module": "django.db",
            "failure_test": "test_models::test_save",
            "failure_message": "No module named ...",
            "signature_hash": "abc123...",
            "test_counts": {
                "passed": 12, "failed": 3,
                "error": 1, "total": 16,
            },
        }
    """
    if not fail_text:
        return {
            "failure_class": "none",
            "failure_module": "",
            "failure_test": "",
            "failure_message": "",
            "signature_hash": "0" * 16,
            "test_counts": None,
        }

    fc = failure_class(fail_text)

    # Extract module from ImportError/ModuleNotFoundError
    failure_module = ""
    m_mod = re.search(
        r"No module named ['\"]([^'\"]+)['\"]",
        fail_text,
    )
    if m_mod:
        failure_module = m_mod.group(1)
    elif fc in ("AttributeError",):
        m_attr = re.search(
            r"module ['\"]([^'\"]+)['\"]"
            r" has no attribute",
            fail_text,
        )
        if m_attr:
            failure_module = m_attr.group(1)

    # Extract failing test name.
    failure_test = ""
    # pytest format: FAILED tests/test_x.py::TestFoo::test_bar
    m_test = re.search(
        r"FAILED\s+([^\s]+::[^\s]+)",
        fail_text,
    )
    if m_test:
        failure_test = m_test.group(1)
    else:
        # Shorter: test_name FAILED or ERROR
        m_test2 = re.search(
            r"(test_[A-Za-z0-9_]+)\s+(?:FAILED|ERROR)",
            fail_text,
        )
        if m_test2:
            failure_test = m_test2.group(1)

    # Extract first meaningful error message line.
    failure_message = ""
    m_msg = re.search(
        r"(?:Error|Exception):\s*(.+?)(?:\n|$)",
        fail_text,
    )
    if m_msg:
        failure_message = m_msg.group(1).strip()[:200]

    # Parse test counts from pytest output.
    test_counts = _parse_test_counts(fail_text)

    # Deterministic signature hash.
    sig_blob = f"{fc}|{failure_module}|{failure_test}"
    sig_hash = hashlib.sha256(
        sig_blob.encode("utf-8"),
    ).hexdigest()[:16]

    return {
        "failure_class": fc,
        "failure_module": failure_module,
        "failure_test": failure_test,
        "failure_message": failure_message,
        "signature_hash": sig_hash,
        "test_counts": test_counts,
    }


def _parse_test_counts(
    text: str,
) -> Optional[dict]:
    """Parse pytest summary line for test counts.

    Matches patterns like:
      '5 passed, 2 failed, 1 error in 3.45s'
      '= 12 passed in 1.23s ='
    """
    if not text:
        return None

    counts: Dict[str, int] = {
        "passed": 0, "failed": 0,
        "error": 0, "total": 0,
    }

    # Look for pytest summary line
    m = re.search(
        r"=+\s*([\d\w\s,]+?)\s*(?:in\s+[\d.]+s)?\s*=+",
        text,
    )
    summary = m.group(1) if m else text[-500:]

    for key in ("passed", "failed", "error",
                "errors", "warnings"):
        m_count = re.search(
            rf"(\d+)\s+{key}",
            summary,
        )
        if m_count:
            val = int(m_count.group(1))
            if key in ("error", "errors"):
                counts["error"] += val
            elif key == "warnings":
                pass  # ignore warnings
            else:
                counts[key] = val

    counts["total"] = (
        counts["passed"]
        + counts["failed"]
        + counts["error"]
    )
    if counts["total"] == 0:
        return None
    return counts


def extract_test_nodes(fail_text: str) -> list:
    """Extract pytest node IDs from failure text.

    Looks for patterns like:
      FAILED tests/test_x.py::TestFoo::test_bar
      ERROR tests/test_y.py::test_baz

    Returns a deduplicated list of node IDs
    suitable for pytest_targeted --target.
    """
    if not fail_text:
        return []
    # Match FAILED/ERROR + node ID.
    nodes: list = []
    seen: set = set()
    for m in re.finditer(
        r"(?:FAILED|ERROR)\s+("
        r"[A-Za-z0-9_./-]+::[A-Za-z0-9_:]+)",
        fail_text,
    ):
        node = m.group(1)
        if node not in seen:
            seen.add(node)
            nodes.append(node)
    # Also match "test_foo FAILED" short form
    # (convert to just the test name).
    if not nodes:
        for m in re.finditer(
            r"(test_[A-Za-z0-9_]+)\s+"
            r"(?:FAILED|ERROR)",
            fail_text,
        ):
            node = m.group(1)
            if node not in seen:
                seen.add(node)
                nodes.append(node)
    return nodes[:20]  # cap at 20


def compute_dense_reward(
    prev_counts: Optional[dict],
    curr_counts: Optional[dict],
) -> float:
    """Compute dense reward from test count delta.

    Reward signal:
      +1.0  = all tests passing (full success)
      +0.5  = reduced failure count
      +0.2  = same failure count (no regression)
       0.0  = no test data
      -0.5  = increased failure count
      -1.0  = total regression (everything fails)
    """
    if not curr_counts:
        return 0.0

    total = curr_counts.get("total", 0)
    if total == 0:
        return 0.0

    curr_fail = (
        curr_counts.get("failed", 0)
        + curr_counts.get("error", 0)
    )

    # Full pass
    if curr_fail == 0:
        return 1.0

    if not prev_counts:
        # First measurement — baseline
        return -0.2 if curr_fail > 0 else 0.5

    prev_fail = (
        prev_counts.get("failed", 0)
        + prev_counts.get("error", 0)
    )

    if prev_fail == 0:
        # Was green, now failing → regression
        return -1.0

    delta = prev_fail - curr_fail  # positive = improvement
    if delta > 0:
        # Improvement — scale by fraction fixed
        return 0.2 + 0.6 * (delta / prev_fail)
    elif delta == 0:
        return 0.2  # no change
    else:
        # Regression
        return -0.5


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
