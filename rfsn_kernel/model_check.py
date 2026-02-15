"""Backward-compatibility shim — renamed to state_search.py.

This module was renamed because 'model checking' implied temporal
logic verification (CTL/LTL). The actual implementation is
BFS/DFS graph search with guard-clause callbacks.
"""

# Re-export everything from the honestly-named module
from rfsn_kernel.state_search import (  # noqa: F401
    StateNode,
    SearchResult,
    SearchResult as ModelCheckResult,  # backward compat
    explore_bfs,
    explore_dfs,
)
