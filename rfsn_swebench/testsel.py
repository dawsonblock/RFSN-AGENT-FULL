"""Incremental test-selection strategy."""
from __future__ import annotations

from .contracts import TaskHints


def choose_quick_tests(hints: TaskHints, default_quick_cmd: str) -> str:
    """Return a test command narrowed to known-failing tests when available.

    If the default command already contains ``-k`` (a filter expression
    built by the task converter), return it as-is — the failing tests are
    already baked in and appending them again as positional args would
    break pytest.

    When the default command does NOT already contain ``-k``, each test
    node is validated against a strict regex and appended as a positional
    argument.
    """
    import re as _re
    import shlex as _shlex

    # If the converter already embedded a -k filter, don't double-append.
    if " -k " in default_quick_cmd:
        return default_quick_cmd

    # Only allow safe pytest node IDs: alphanumeric, _, ., /, :, -, [, ]
    _SAFE_TEST_NODE = _re.compile(r"^[A-Za-z0-9_./:@\[\]-]{1,512}$")
    if hints.failing_tests:
        safe = []
        for t in hints.failing_tests:
            if _SAFE_TEST_NODE.match(t):
                safe.append(_shlex.quote(t))
            # silently skip invalid test names
        if safe:
            return f"{default_quick_cmd} {' '.join(safe)}"
    return default_quick_cmd
