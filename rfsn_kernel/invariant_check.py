"""Pre/post condition assertions on kernel state transitions.

Defines invariants that must hold before and after every kernel
decision step. Violations are logged and optionally fatal.
"""

import os
from typing import Any, Dict, List, Optional


class InvariantViolation(Exception):
    """Raised when a kernel invariant is violated."""

    def __init__(self, invariant_name: str, message: str, context: dict):
        self.invariant_name = invariant_name
        self.context = context
        super().__init__(f"INVARIANT_VIOLATION [{invariant_name}]: {message}")


def _check_action_in_allowlist(state: Dict[str, Any]) -> Optional[str]:
    """Every proposed action must be in the tool allowlist."""
    action = state.get("proposed_action", {})
    tool = action.get("tool")
    allowlist = state.get("tool_allowlist", [])
    if tool and allowlist and tool not in allowlist:
        return f"Tool '{tool}' not in allowlist"
    return None


def _check_iteration_bound(state: Dict[str, Any]) -> Optional[str]:
    """Iteration count must not exceed max_iterations."""
    current = state.get("iteration", 0)
    maximum = state.get("max_iterations", 30)
    if current > maximum:
        return f"Iteration {current} exceeds max {maximum}"
    return None


def _check_patch_not_empty(state: Dict[str, Any]) -> Optional[str]:
    """If a patch is committed, it must not be empty."""
    patch = state.get("committed_patch")
    if patch is not None and len(patch.strip()) == 0:
        return "Committed patch is empty"
    return None


def _check_outcome_valid(state: Dict[str, Any]) -> Optional[str]:
    """Outcome must be one of the allowed values."""
    outcome = state.get("outcome")
    valid = {"success", "fail", "error", "timeout", "skip", None}
    if outcome not in valid:
        return f"Invalid outcome: {outcome!r}"
    return None


def _check_cost_within_budget(state: Dict[str, Any]) -> Optional[str]:
    """Total cost must not exceed budget."""
    cost = state.get("total_cost", 0.0)
    budget = state.get("cost_budget", float("inf"))
    if cost > budget:
        return f"Cost {cost:.4f} exceeds budget {budget:.4f}"
    return None


# Registry of all invariant checks
_INVARIANTS = [
    ("action_in_allowlist", _check_action_in_allowlist),
    ("iteration_bound", _check_iteration_bound),
    ("patch_not_empty", _check_patch_not_empty),
    ("outcome_valid", _check_outcome_valid),
    ("cost_within_budget", _check_cost_within_budget),
]


def check_invariants(
    state: Dict[str, Any],
    strict: bool = False,
) -> List[Dict[str, Any]]:
    """Run all invariant checks against the given state.

    Args:
        state: Current kernel state dict.
        strict: If True, raise InvariantViolation on first failure.

    Returns:
        List of violation dicts with keys: name, message
    """
    violations = []
    for name, check_fn in _INVARIANTS:
        msg = check_fn(state)
        if msg is not None:
            violations.append({"name": name, "message": msg})
            if strict:
                raise InvariantViolation(name, msg, state)

    return violations


def check_pre(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Pre-step invariant check."""
    return check_invariants(state, strict=_is_strict())


def check_post(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Post-step invariant check."""
    return check_invariants(state, strict=_is_strict())


def _is_strict() -> bool:
    return os.getenv("RFSN_INVARIANT_STRICT", "0") == "1"


def register_invariant(name: str, check_fn):
    """Register a custom invariant check function.

    check_fn(state) should return None if OK, or an error message string.
    """
    _INVARIANTS.append((name, check_fn))
