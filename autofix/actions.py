"""Map classified failures to corrective actions.

Each strategy produces a concrete action dict that `apply.py` can execute.
"""

import os
from typing import Any, Dict, Optional


def _action_install_dependency(failure: Dict[str, Any]) -> Dict[str, Any]:
    """Generate an action to install a missing dependency."""
    msg = failure.get("message", "")
    # Try to extract module name from "No module named 'foo'"
    module = ""
    if "No module named" in msg:
        parts = msg.split("'")
        if len(parts) >= 2:
            module = parts[1].split(".")[0]

    return {
        "type": "install_dependency",
        "module": module,
        "command": f"pip install {module}" if module else "",
        "failure": failure,
    }


def _action_rollback_patch(failure: Dict[str, Any]) -> Dict[str, Any]:
    """Generate an action to rollback the last applied patch."""
    return {
        "type": "rollback_patch",
        "file": failure.get("file", ""),
        "reason": failure.get("message", ""),
        "failure": failure,
    }


def _action_restart_service(failure: Dict[str, Any]) -> Dict[str, Any]:
    """Generate an action to restart a crashed service."""
    return {
        "type": "restart_service",
        "service": _guess_service(failure),
        "reason": failure.get("message", ""),
        "failure": failure,
    }


def _action_increase_timeout(failure: Dict[str, Any]) -> Dict[str, Any]:
    """Generate an action to increase the timeout."""
    return {
        "type": "increase_timeout",
        "current_timeout": int(os.getenv("RFSN_EXEC_TIMEOUT", "300")),
        "new_timeout": int(os.getenv("RFSN_EXEC_TIMEOUT", "300")) * 2,
        "failure": failure,
    }


def _action_reduce_workload(failure: Dict[str, Any]) -> Dict[str, Any]:
    """Generate an action to reduce memory-intensive workloads."""
    return {
        "type": "reduce_workload",
        "suggestion": "Reduce batch size or parallel workers",
        "failure": failure,
    }


def _action_re_patch(failure: Dict[str, Any]) -> Dict[str, Any]:
    """Generate an action to re-attempt patching with feedback."""
    return {
        "type": "re_patch",
        "file": failure.get("file", ""),
        "test_name": failure.get("message", "").split(":")[0],
        "failure": failure,
    }


def _action_log(failure: Dict[str, Any]) -> Dict[str, Any]:
    """Generate a no-op action that just logs."""
    return {
        "type": "log_and_continue",
        "message": failure.get("message", ""),
        "failure": failure,
    }


def _action_check_fs(failure: Dict[str, Any]) -> Dict[str, Any]:
    """Generate an action to check filesystem state."""
    return {
        "type": "check_filesystem",
        "reason": failure.get("message", ""),
        "failure": failure,
    }


def _guess_service(failure: Dict[str, Any]) -> str:
    """Try to guess which service produced the failure."""
    source = failure.get("source", "")
    for svc in ("orchestrator", "executor", "learner", "replay_verifier"):
        if svc in source:
            return svc
    return "unknown"


_STRATEGY_MAP = {
    "install_dependency": _action_install_dependency,
    "rollback_patch": _action_rollback_patch,
    "restart_service": _action_restart_service,
    "increase_timeout": _action_increase_timeout,
    "reduce_workload": _action_reduce_workload,
    "re_patch": _action_re_patch,
    "log_and_continue": _action_log,
    "check_filesystem": _action_check_fs,
}


def plan_action(classified_failure: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a classified failure into a concrete action."""
    strategy = classified_failure.get("strategy", "log_and_continue")
    handler = _STRATEGY_MAP.get(strategy, _action_log)
    return handler(classified_failure)


def plan_actions(classified_failures: list) -> list:
    """Convert a batch of classified failures into actions."""
    return [plan_action(f) for f in classified_failures]
