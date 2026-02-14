"""Generate minimal counterexamples from model check violations.

Given a violation path from the model checker, produces a simplified
reproduction case that triggers the same violation with fewer steps.
"""

import copy
from typing import Any, Callable, Dict, List, Optional


def minimize_path(
    initial_state: Dict[str, Any],
    violation_path: List[str],
    transition: Callable[[Dict[str, Any], str], Dict[str, Any]],
    check_invariant: Callable[[Dict[str, Any]], Optional[str]],
) -> List[str]:
    """Try removing actions from the path while preserving the violation.

    Uses delta-debugging: try removing each action one at a time,
    keep the removal if the violation still triggers.

    Returns:
        A (possibly shorter) path that still triggers the violation.
    """
    if not violation_path:
        return []

    current_path = list(violation_path)

    # Try removing each action
    changed = True
    while changed:
        changed = False
        for i in range(len(current_path)):
            candidate = current_path[:i] + current_path[i + 1 :]
            if _triggers_violation(
                initial_state, candidate, transition, check_invariant
            ):
                current_path = candidate
                changed = True
                break

    return current_path


def _triggers_violation(
    initial_state: Dict[str, Any],
    path: List[str],
    transition: Callable[[Dict[str, Any], str], Dict[str, Any]],
    check_invariant: Callable[[Dict[str, Any]], Optional[str]],
) -> bool:
    """Replay a path and check if it triggers a violation."""
    state = copy.deepcopy(initial_state)
    for action in path:
        try:
            state = transition(state, action)
        except Exception:
            return True  # Exception counts as violation

        violation = check_invariant(state)
        if violation is not None:
            return True

    return False


def generate_counterexample(
    initial_state: Dict[str, Any],
    violation_path: List[str],
    transition: Callable[[Dict[str, Any], str], Dict[str, Any]],
    check_invariant: Callable[[Dict[str, Any]], Optional[str]],
) -> Dict[str, Any]:
    """Generate a counterexample report.

    Returns:
        dict with keys: original_length, minimized_length, minimized_path,
                        violation_message, final_state
    """
    minimized = minimize_path(
        initial_state,
        violation_path,
        transition,
        check_invariant,
    )

    # Replay minimized path to get final state + violation
    state = copy.deepcopy(initial_state)
    violation_msg = None
    for action in minimized:
        try:
            state = transition(state, action)
        except Exception as e:
            violation_msg = f"Exception: {e}"
            break
        msg = check_invariant(state)
        if msg is not None:
            violation_msg = msg
            break

    return {
        "original_length": len(violation_path),
        "minimized_length": len(minimized),
        "minimized_path": minimized,
        "violation_message": violation_msg,
        "final_state": state,
    }
