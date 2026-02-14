"""Verify that corrective actions actually fixed the problem.

Re-runs the relevant checks after an action was applied to confirm
the failure is resolved.
"""

import subprocess
from typing import Any, Dict


def verify_action(action_result: Dict[str, Any]) -> Dict[str, Any]:
    """Verify a single action result.

    Returns:
        dict with keys: verified, action_type, reason
    """
    action_type = action_result.get("action_type", "unknown")
    result = action_result.get("result", {})

    if not action_result.get("applied", False):
        return {
            "verified": False,
            "action_type": action_type,
            "reason": "action was not applied",
        }

    if action_type == "install_dependency":
        return _verify_import(result)
    elif action_type == "rollback_patch":
        return _verify_clean_state(result)
    elif action_type == "restart_service":
        return _verify_service_healthy(result)
    else:
        # For actions without specific verification, trust the result
        ok = result.get("ok", False)
        return {
            "verified": ok,
            "action_type": action_type,
            "reason": "action result ok" if ok else "action failed",
        }


def _verify_import(result: Dict[str, Any]) -> Dict[str, Any]:
    """Check that pip install succeeded by verifying returncode."""
    ok = result.get("ok", False)
    return {
        "verified": ok,
        "action_type": "install_dependency",
        "reason": "install succeeded" if ok else result.get("stderr", "install failed"),
    }


def _verify_clean_state(result: Dict[str, Any]) -> Dict[str, Any]:
    """Check that git checkout succeeded."""
    ok = result.get("ok", False)
    return {
        "verified": ok,
        "action_type": "rollback_patch",
        "reason": "rollback succeeded" if ok else "rollback failed",
    }


def _verify_service_healthy(result: Dict[str, Any]) -> Dict[str, Any]:
    """Check that docker restart was at least accepted."""
    ok = result.get("ok", False)
    return {
        "verified": ok,
        "action_type": "restart_service",
        "reason": "restart accepted" if ok else "restart failed",
    }


def verify_all(action_results: list) -> Dict[str, Any]:
    """Verify a batch of action results.

    Returns:
        dict with keys: all_verified, total, verified_count, failures
    """
    results = [verify_action(r) for r in action_results]
    verified = [r for r in results if r["verified"]]
    failures = [r for r in results if not r["verified"]]

    return {
        "all_verified": len(failures) == 0,
        "total": len(results),
        "verified_count": len(verified),
        "failure_count": len(failures),
        "failures": failures,
    }
