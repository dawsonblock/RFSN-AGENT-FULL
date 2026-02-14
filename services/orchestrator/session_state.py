"""Global session state management."""

from typing import Dict, Any, Optional

# Global in-memory state for active runs
_RUN_CONTEXT: Dict[str, Dict[str, Any]] = {}


def get_run_context(run_id: str) -> Optional[Dict[str, Any]]:
    return _RUN_CONTEXT.get(run_id)


def ensure_run_context(
    run_id: str, default_cmd_plan: Dict[str, Any] = None
) -> Dict[str, Any]:
    ctx = _RUN_CONTEXT.get(run_id)
    if isinstance(ctx, dict):
        return ctx

    # Initialize new context
    from rfsn_kernel.sim_cache import (
        SimCache,
    )  # Lazy import to avoid cycle if kernel imports this

    ctx = {
        "cmd_plan": default_cmd_plan or {},
        "baseline_test_template": "",
        "sim_cache": SimCache(),
        "repair": {
            "phase": "SEARCH",
            "attempt": 0,
            "max_attempts": 3,
            "last_status": 1,
        },
    }
    _RUN_CONTEXT[run_id] = ctx
    return ctx


def clear_run_context(run_id: str):
    if run_id in _RUN_CONTEXT:
        del _RUN_CONTEXT[run_id]
