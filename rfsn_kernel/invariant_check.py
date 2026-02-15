"""Backward-compatibility shim — renamed to guard_checks.py.

This module was renamed because 'invariant checking' implied formal
invariant verification. The actual implementation is runtime guard
clauses checking dictionary field values.
"""

# Re-export everything from the honestly-named module
from rfsn_kernel.guard_checks import (  # noqa: F401
    GuardViolation,
    GuardViolation as InvariantViolation,  # backward compat
    check_guards,
    check_guards as check_invariants,  # backward compat
    check_pre,
    check_post,
    register_guard,
    register_guard as register_invariant,  # backward compat
)

