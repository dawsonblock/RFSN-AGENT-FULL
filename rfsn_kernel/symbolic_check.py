"""Backward-compatibility shim — renamed to ast_lint.py.

This module was renamed because 'symbolic check' implied symbolic
execution or constraint solving. The actual implementation is an
AST-based linter for unreachable code and infinite loops.
"""

# Re-export everything from the honestly-named module
from rfsn_kernel.ast_lint import (  # noqa: F401
    AstIssue,
    AstIssue as SymbolicIssue,  # backward compat
    check_file,
    check_directory,
)

