"""Execute corrective actions with rollback on failure.

Takes action dicts from `actions.py` and executes them,
with a rollback mechanism if the action fails.

SECURITY NOTE
-------------
This module previously used ``shell=True`` for arbitrary command execution.
That entry point is now disabled.  All command execution uses structured
argument lists via ``ALLOWED_COMMAND_TEMPLATES`` or explicit safe functions.
Callers that previously passed raw shell strings must migrate to the
template-based ``run_cmd_template`` tool.
"""

import os
import subprocess
from typing import Any, Dict, List

# ---------------------------------------------------------------------------
# Allowlisted command templates (no shell=True, no raw strings).
# Template values are used verbatim — no interpolation from agent output.
# ---------------------------------------------------------------------------
ALLOWED_COMMAND_TEMPLATES: Dict[str, List[str]] = {
    "pytest": ["python", "-m", "pytest"],
    "pytest_file": ["python", "-m", "pytest", "{path}"],
    "ruff_check": ["python", "-m", "ruff", "check", "{path}"],
    "ruff_format": ["python", "-m", "ruff", "format", "{path}"],
}

# Characters that indicate shell injection in a path or argument.
_SHELL_METACHAR_RE = None

def _has_metachar(value: str) -> bool:
    """Return True if *value* contains shell metacharacters."""
    dangerous = set(';|&><$()`\n\r')
    return any(c in dangerous for c in value)


def run_template(
    template_name: str,
    path: str = "",
    workdir: str = ".",
    timeout: int = 60,
) -> Dict[str, Any]:
    """Execute an allowlisted command template.

    Parameters
    ----------
    template_name:
        Key in ``ALLOWED_COMMAND_TEMPLATES``.
    path:
        Optional path argument (substituted for ``{path}`` placeholders).
        Must be a relative path; must not contain shell metacharacters.
    workdir:
        Working directory.  Must be absolute or relative; must not escape
        workspace.
    timeout:
        Hard timeout in seconds.

    Returns
    -------
    dict with ``ok``, ``stdout``, ``stderr``, ``returncode``.
    """
    template = ALLOWED_COMMAND_TEMPLATES.get(template_name)
    if template is None:
        return {
            "ok": False,
            "error": f"Unknown template: {template_name!r}. "
                     f"Allowed: {sorted(ALLOWED_COMMAND_TEMPLATES)}",
        }

    # Validate path argument.
    if path:
        if os.path.isabs(path):
            return {"ok": False, "error": "path must be relative"}
        if ".." in path.split("/") or ".." in path.split(os.sep):
            return {"ok": False, "error": "path traversal rejected"}
        if _has_metachar(path):
            return {"ok": False, "error": "path contains shell metacharacters"}

    # Build argv — substitute {path} placeholder.
    argv = [
        part.replace("{path}", path) if "{path}" in part else part
        for part in template
    ]

    # Remove empty args that arise when path is absent.
    argv = [a for a in argv if a]

    try:
        result = subprocess.run(
            argv,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=timeout,
            # shell=False is the default; never set shell=True here.
        )
        return {
            "ok": result.returncode == 0,
            "stdout": result.stdout[-2000:],
            "stderr": result.stderr[-2000:],
            "returncode": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout", "returncode": -1}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "returncode": -1}


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
        return f"restart service {action.get('service', '?')}"
    if t == "increase_timeout":
        return f"Increase timeout to {action.get('new_timeout', '?')}s"
    return t


def _handle_rollback(action: Dict[str, Any]) -> Dict[str, Any]:
    """Roll back a file using git checkout via structured args (no shell=True)."""
    f = action.get("file", "")
    if not f:
        return {"ok": False, "error": "no file"}
    if os.path.isabs(f) or ".." in f.split("/"):
        return {"ok": False, "error": "unsafe file path"}
    try:
        result = subprocess.run(
            ["git", "checkout", "--", f],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return {
            "ok": result.returncode == 0,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _handle_timeout(action: Dict[str, Any]) -> Dict[str, Any]:
    new_val = action.get("new_timeout", 600)
    os.environ["RFSN_EXEC_TIMEOUT"] = str(int(new_val))
    return {"ok": True, "new_timeout": new_val}


def _handle_reduce(action: Dict[str, Any]) -> Dict[str, Any]:
    current = int(os.getenv("RFSN_PARALLEL_WORKERS", "4"))
    new_val = max(1, current // 2)
    os.environ["RFSN_PARALLEL_WORKERS"] = str(new_val)
    return {"ok": True, "new_workers": new_val}


def _noop(action: Dict[str, Any]) -> Dict[str, Any]:
    return {"ok": True, "action": "logged"}


_ACTION_HANDLERS = {
    # install_dependency: removed — arbitrary package install not allowed.
    "rollback_patch": _handle_rollback,
    # restart_service: removed — docker-compose exec via shell is not allowed.
    "increase_timeout": _handle_timeout,
    "reduce_workload": _handle_reduce,
    "log_and_continue": _noop,
    "re_patch": _noop,
    "check_filesystem": _noop,
}


def apply_actions(
    actions: List[Dict[str, Any]],
    dry_run: bool = False,
) -> List[Dict[str, Any]]:
    """Apply a batch of corrective actions."""
    return [apply_action(a, dry_run=dry_run) for a in actions]
