from __future__ import annotations

import re
from typing import Any, Dict, List


def extract_failure_kinds(executor_out: Dict[str, Any]) -> List[str]:
    kinds: List[str] = []
    status = int(executor_out.get("status", 1) or 1)
    logs = str(
        executor_out.get("logs")
        or executor_out.get("payload")
        or "",
    )
    low = logs.lower()

    if status == 0:
        return kinds

    # tests
    if (
        "failed" in low
        and (
            "pytest" in low
            or "unittest" in low
            or "tests" in low
        )
    ):
        kinds.append("tests_failed")
    if re.search(r"\bassertionerror\b", low):
        kinds.append("tests_failed")
    if "collected 0 items" in low and "error" in low:
        kinds.append("tests_failed")

    # deps / import
    if (
        re.search(r"\bmodulenotfounderror\b", low)
        or "no module named" in low
    ):
        kinds.append("import_error_missing_module")
    if (
        "pip" in low
        and (
            "could not find a version" in low
            or "resolutionimpossible" in low
        )
    ):
        kinds.append("deps_install_failed")
    if (
        "error: subprocess-exited-with-error" in low
        and "pip" in low
    ):
        kinds.append("deps_install_failed")
    if (
        "missing dependency" in low
        or (
            "not found: " in low
            and (
                "gcc" in low
                or "cmake" in low
            )
        )
    ):
        kinds.append("build_system_missing_dependency")

    # CI
    if (
        ".github/workflows" in low
        or ("workflow" in low and "yaml" in low)
    ):
        kinds.append("ci_config_invalid")
    if "ci" in low and ("failed" in low or "error" in low):
        kinds.append("ci_failed")
    if (
        "runner" in low
        and (
            "ubuntu" in low
            or "windows" in low
            or "macos" in low
        )
    ):
        kinds.append("ci_env_mismatch")

    # dedupe while preserving order
    seen = set()
    out: List[str] = []
    for k in kinds:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out
