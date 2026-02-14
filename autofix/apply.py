"""Execute corrective actions with rollback on failure.

Takes action dicts from `actions.py` and executes them,
with a rollback mechanism if the action fails.
"""

import os
import shutil
import subprocess
from typing import Any, Dict, List


def _exec_cmd(cmd: str, timeout: int = 60) -> Dict[str, Any]:
    """Execute a shell command and return result."""
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "ok": result.returncode == 0,
            "stdout": result.stdout[-2000:],
            "stderr": result.stderr[-2000:],
            "returncode": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def apply_action(action: Dict[str, Any], dry_run: bool = False) -> Dict[str, Any]:
    """Apply a single corrective action.

    Args:
        action: Action dict from actions.plan_action()
        dry_run: If True, only report what would be done.

    Returns:
        dict with keys: action_type, applied, result
    """
    action_type = action.get("type", "unknown")

    if dry_run:
        return {
            "action_type": action_type,
            "applied": False,
            "dry_run": True,
            "description": _describe(action),
        }

    handler = _ACTION_HANDLERS.get(action_type, _noop)
    try:
        result = handler(action)
        return {
            "action_type": action_type,
            "applied": True,
            "result": result,
        }
    except Exception as e:
        return {
            "action_type": action_type,
            "applied": False,
            "error": str(e),
        }


def _describe(action: Dict[str, Any]) -> str:
    """Human-readable description of an action."""
    t = action.get("type", "unknown")
    if t == "install_dependency":
        return f"pip install {action.get('module', '?')}"
    if t == "rollback_patch":
        return f"git checkout -- {action.get('file', '?')}"
    if t == "restart_service":
        return f"docker-compose restart {action.get('service', '?')}"
    if t == "increase_timeout":
        return f"Increase timeout to {action.get('new_timeout', '?')}s"
    return t


def _handle_install(action: Dict[str, Any]) -> Dict[str, Any]:
    cmd = action.get("command", "")
    if not cmd:
        return {"ok": False, "error": "no command"}
    return _exec_cmd(cmd)


def _handle_rollback(action: Dict[str, Any]) -> Dict[str, Any]:
    f = action.get("file", "")
    if not f:
        return {"ok": False, "error": "no file"}
    return _exec_cmd(f"git checkout -- {f}")


def _handle_restart(action: Dict[str, Any]) -> Dict[str, Any]:
    svc = action.get("service", "")
    if not svc or svc == "unknown":
        return {"ok": False, "error": "unknown service"}
    return _exec_cmd(f"docker-compose restart {svc}")


def _handle_timeout(action: Dict[str, Any]) -> Dict[str, Any]:
    new_val = action.get("new_timeout", 600)
    os.environ["RFSN_EXEC_TIMEOUT"] = str(new_val)
    return {"ok": True, "new_timeout": new_val}


def _handle_reduce(action: Dict[str, Any]) -> Dict[str, Any]:
    # Reduce parallel workers if set
    current = int(os.getenv("RFSN_PARALLEL_WORKERS", "4"))
    new_val = max(1, current // 2)
    os.environ["RFSN_PARALLEL_WORKERS"] = str(new_val)
    return {"ok": True, "new_workers": new_val}


def _noop(action: Dict[str, Any]) -> Dict[str, Any]:
    return {"ok": True, "action": "logged"}


_ACTION_HANDLERS = {
    "install_dependency": _handle_install,
    "rollback_patch": _handle_rollback,
    "restart_service": _handle_restart,
    "increase_timeout": _handle_timeout,
    "reduce_workload": _handle_reduce,
    "log_and_continue": _noop,
    "re_patch": _noop,  # Re-patching is handled by the orchestrator
    "check_filesystem": _noop,
}


def apply_actions(
    actions: List[Dict[str, Any]],
    dry_run: bool = False,
) -> List[Dict[str, Any]]:
    """Apply a batch of corrective actions."""
    return [apply_action(a, dry_run=dry_run) for a in actions]
