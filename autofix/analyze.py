"""Classify failures into actionable categories.

Takes failure records from ingest and categorizes them
for automatic corrective action.
"""

from typing import Any, Dict, List


# Classification hierarchy
_CLASSIFICATIONS = {
    "import_error": {
        "severity": "high",
        "auto_fixable": True,
        "strategy": "install_dependency",
    },
    "syntax_error": {
        "severity": "high",
        "auto_fixable": False,
        "strategy": "rollback_patch",
    },
    "test_failure": {
        "severity": "medium",
        "auto_fixable": True,
        "strategy": "re_patch",
    },
    "timeout": {
        "severity": "medium",
        "auto_fixable": True,
        "strategy": "increase_timeout",
    },
    "oom": {
        "severity": "critical",
        "auto_fixable": True,
        "strategy": "reduce_workload",
    },
    "fatal": {
        "severity": "critical",
        "auto_fixable": False,
        "strategy": "restart_service",
    },
    "os_error": {
        "severity": "medium",
        "auto_fixable": False,
        "strategy": "check_filesystem",
    },
    "traceback": {
        "severity": "low",
        "auto_fixable": False,
        "strategy": "log_and_continue",
    },
    "log_error": {
        "severity": "low",
        "auto_fixable": False,
        "strategy": "log_and_continue",
    },
}

_DEFAULT_CLASSIFICATION = {
    "severity": "low",
    "auto_fixable": False,
    "strategy": "log_and_continue",
}


def classify(failure: Dict[str, Any]) -> Dict[str, Any]:
    """Classify a single failure record.

    Adds: severity, auto_fixable, strategy to the failure dict.
    """
    kind = failure.get("kind", "unknown")
    classification = _CLASSIFICATIONS.get(kind, _DEFAULT_CLASSIFICATION)

    return {
        **failure,
        "severity": classification["severity"],
        "auto_fixable": classification["auto_fixable"],
        "strategy": classification["strategy"],
    }


def classify_batch(failures: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Classify a list of failure records."""
    return [classify(f) for f in failures]


def prioritize(classified: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Sort classified failures by severity (critical > high > medium > low)."""
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    return sorted(classified, key=lambda f: order.get(f.get("severity", "low"), 4))


def summarize(classified: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Produce a summary of classified failures."""
    by_severity: Dict[str, int] = {}
    by_strategy: Dict[str, int] = {}
    auto_fixable_count = 0

    for f in classified:
        sev = f.get("severity", "unknown")
        by_severity[sev] = by_severity.get(sev, 0) + 1
        strat = f.get("strategy", "unknown")
        by_strategy[strat] = by_strategy.get(strat, 0) + 1
        if f.get("auto_fixable"):
            auto_fixable_count += 1

    return {
        "total": len(classified),
        "auto_fixable": auto_fixable_count,
        "by_severity": by_severity,
        "by_strategy": by_strategy,
    }
