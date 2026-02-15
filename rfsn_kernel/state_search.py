"""BFS/DFS state-space exploration for kernel decision trees.

Given a set of possible actions at each step and a transition function,
explores the reachable state space to detect unreachable states,
deadlocks, and guard-clause violations.

This is a parameterized graph search utility — not formal model checking
(no temporal logic, CTL/LTL, or symbolic state representation).
"""

from collections import deque
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


class StateNode:
    """A node in the exploration graph."""

    __slots__ = ("state_id", "data", "parent_id", "action")

    def __init__(
        self,
        state_id: str,
        data: Dict[str, Any],
        parent_id: Optional[str] = None,
        action: Optional[str] = None,
    ):
        self.state_id = state_id
        self.data = data
        self.parent_id = parent_id
        self.action = action


class SearchResult:
    """Result of a state-space search run."""

    def __init__(self):
        self.visited: int = 0
        self.deadlocks: List[str] = []
        self.violations: List[Dict[str, Any]] = []
        self.max_depth: int = 0
        self.path_to_violation: List[str] = []

    def to_dict(self) -> Dict[str, Any]:
        return {
            "visited": self.visited,
            "deadlocks": self.deadlocks,
            "violations": self.violations,
            "max_depth": self.max_depth,
            "ok": len(self.violations) == 0 and len(self.deadlocks) == 0,
        }


# Backward compatibility alias
ModelCheckResult = SearchResult


def _state_fingerprint(state: Dict[str, Any]) -> str:
    """Create a hashable fingerprint of a state for cycle detection."""
    import json

    return json.dumps(state, sort_keys=True, separators=(",", ":"))


def explore_bfs(
    initial_state: Dict[str, Any],
    get_actions: Callable[[Dict[str, Any]], List[str]],
    transition: Callable[[Dict[str, Any], str], Dict[str, Any]],
    check_invariant: Callable[[Dict[str, Any]], Optional[str]] = lambda s: None,
    max_states: int = 10000,
    max_depth: int = 50,
) -> SearchResult:
    """BFS exploration of the state space.

    Args:
        initial_state: Starting state.
        get_actions: Given a state, returns list of possible actions.
        transition: Given a state and action, returns the next state.
        check_invariant: Given a state, returns None if OK or error message.
        max_states: Budget for total states to explore.
        max_depth: Maximum depth to explore.

    Returns:
        SearchResult with findings.
    """
    result = SearchResult()
    visited: Set[str] = set()
    queue: deque = deque()

    fp0 = _state_fingerprint(initial_state)
    visited.add(fp0)
    queue.append((initial_state, 0, []))

    while queue and result.visited < max_states:
        state, depth, path = queue.popleft()
        result.visited += 1
        result.max_depth = max(result.max_depth, depth)

        # Check invariant
        violation = check_invariant(state)
        if violation is not None:
            result.violations.append(
                {
                    "depth": depth,
                    "message": violation,
                    "path": path,
                }
            )
            continue  # Don't expand violating states

        if depth >= max_depth:
            continue

        actions = get_actions(state)
        if not actions:
            result.deadlocks.append(f"depth={depth}, path={path}")
            continue

        for action in actions:
            try:
                next_state = transition(state, action)
            except Exception as e:
                result.violations.append(
                    {
                        "depth": depth + 1,
                        "message": f"Transition error on {action}: {e}",
                        "path": path + [action],
                    }
                )
                continue

            fp = _state_fingerprint(next_state)
            if fp not in visited:
                visited.add(fp)
                queue.append((next_state, depth + 1, path + [action]))

    return result


def explore_dfs(
    initial_state: Dict[str, Any],
    get_actions: Callable[[Dict[str, Any]], List[str]],
    transition: Callable[[Dict[str, Any], str], Dict[str, Any]],
    check_invariant: Callable[[Dict[str, Any]], Optional[str]] = lambda s: None,
    max_states: int = 10000,
    max_depth: int = 50,
) -> SearchResult:
    """DFS exploration of the state space.

    Same interface as explore_bfs but uses depth-first traversal
    for faster detection of deep violations.
    """
    result = SearchResult()
    visited: Set[str] = set()
    stack = [(initial_state, 0, [])]

    fp0 = _state_fingerprint(initial_state)
    visited.add(fp0)

    while stack and result.visited < max_states:
        state, depth, path = stack.pop()
        result.visited += 1
        result.max_depth = max(result.max_depth, depth)

        violation = check_invariant(state)
        if violation is not None:
            result.violations.append(
                {
                    "depth": depth,
                    "message": violation,
                    "path": path,
                }
            )
            continue

        if depth >= max_depth:
            continue

        actions = get_actions(state)
        if not actions:
            result.deadlocks.append(f"depth={depth}, path={path}")
            continue

        for action in reversed(actions):
            try:
                next_state = transition(state, action)
            except Exception as e:
                result.violations.append(
                    {
                        "depth": depth + 1,
                        "message": f"Transition error on {action}: {e}",
                        "path": path + [action],
                    }
                )
                continue

            fp = _state_fingerprint(next_state)
            if fp not in visited:
                visited.add(fp)
                stack.append((next_state, depth + 1, path + [action]))

    return result
