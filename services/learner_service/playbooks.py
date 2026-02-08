"""Deterministic failure-router playbook catalog.

Each playbook is a named sequence of step-type phases
that guide the LLM through a structured debugging flow.
These replace the abstract S1–S5 strategy names and
become the concrete arms for Thompson sampling.

Playbooks are keyed by a short ID that appears in
the learner's strategy_stats and outcome_map tables.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class PlaybookStep:
    """One phase in a playbook."""

    label: str           # human-readable name
    step_type: str       # kernel step type
    guidance: str        # instruction for the LLM
    max_calls: int = 1   # how many calls allowed


@dataclass(frozen=True)
class Playbook:
    """A named debugging playbook."""

    playbook_id: str
    name: str
    description: str
    phases: tuple[PlaybookStep, ...]
    # Failure classes this playbook is designed for.
    # Empty means "generic / any failure".
    target_failures: tuple[str, ...] = ()

    @property
    def prompt_addendum(self) -> str:
        """Build a compact LLM-facing addendum."""
        lines = [
            f"Playbook: {self.name}",
            f"  {self.description}",
            "",
            "Follow these phases IN ORDER:",
        ]
        for i, ph in enumerate(self.phases, 1):
            calls = (
                f" (up to {ph.max_calls}x)"
                if ph.max_calls > 1 else ""
            )
            lines.append(
                f"  {i}. [{ph.step_type}]{calls}"
                f" — {ph.guidance}"
            )
        lines.append("")
        lines.append(
            "Do NOT skip phases."
            "  Move to the next phase only"
            " when the current one is satisfied."
            "  Keep diffs minimal."
        )
        return "\n".join(lines)


# ─────────────────────────────────────────────
#  Playbook catalog
# ─────────────────────────────────────────────

PB_IMPORT_FIX = Playbook(
    playbook_id="PB_import_fix",
    name="Import / Dependency Fix",
    description=(
        "For ImportError / ModuleNotFoundError."
        " Fix missing or broken imports/deps"
        " before touching application code."
    ),
    target_failures=(
        "ImportError", "ModuleNotFoundError",
    ),
    phases=(
        PlaybookStep(
            label="locate_import",
            step_type="repo_search",
            guidance=(
                "Search for the missing module or"
                " import statement in the codebase."
            ),
            max_calls=2,
        ),
        PlaybookStep(
            label="read_context",
            step_type="repo_read_range",
            guidance=(
                "Read the file(s) containing the"
                " broken import to understand"
                " the dependency."
            ),
            max_calls=3,
        ),
        PlaybookStep(
            label="fix_deps",
            step_type="ensure_deps",
            guidance=(
                "If the module is a missing"
                " external package, install it."
                " Skip if it's an internal import."
            ),
            max_calls=1,
        ),
        PlaybookStep(
            label="verify_import",
            step_type="run_tests",
            guidance=(
                "Run targeted tests to verify the"
                " import error is resolved."
            ),
            max_calls=1,
        ),
        PlaybookStep(
            label="patch_if_needed",
            step_type="apply_patch",
            guidance=(
                "If the import path is wrong"
                " (not a missing package),"
                " patch the import statement."
                " Minimal diff only."
            ),
            max_calls=1,
        ),
        PlaybookStep(
            label="final_test",
            step_type="run_tests",
            guidance=(
                "Run the full test suite to"
                " confirm no regressions."
            ),
            max_calls=1,
        ),
    ),
)

PB_ASSERTION_FIX = Playbook(
    playbook_id="PB_assertion_fix",
    name="Assertion / Test-Failure Fix",
    description=(
        "For AssertionError and test failures."
        " Locate the failing test, trace to"
        " the implementation, patch the impl."
    ),
    target_failures=(
        "AssertionError", "AssertionError",
    ),
    phases=(
        PlaybookStep(
            label="find_failing_test",
            step_type="repo_search",
            guidance=(
                "Search for the failing test"
                " function or class name from"
                " the error output."
            ),
            max_calls=2,
        ),
        PlaybookStep(
            label="read_test",
            step_type="repo_read_range",
            guidance=(
                "Read the failing test to"
                " understand what it expects."
            ),
            max_calls=2,
        ),
        PlaybookStep(
            label="find_impl",
            step_type="repo_search",
            guidance=(
                "Search for the symbol/function"
                " under test in the source code."
            ),
            max_calls=2,
        ),
        PlaybookStep(
            label="read_impl",
            step_type="repo_read_range",
            guidance=(
                "Read the implementation to"
                " understand the bug."
            ),
            max_calls=3,
        ),
        PlaybookStep(
            label="patch_impl",
            step_type="apply_patch",
            guidance=(
                "Apply a minimal patch to the"
                " implementation (NOT the test)."
                " Keep diff as small as possible."
            ),
            max_calls=2,
        ),
        PlaybookStep(
            label="targeted_test",
            step_type="run_tests",
            guidance=(
                "Run the failing test(s) with"
                " pytest_targeted to verify fix."
            ),
            max_calls=1,
        ),
        PlaybookStep(
            label="suite_test",
            step_type="run_tests",
            guidance=(
                "Run full suite to confirm"
                " no regressions."
            ),
            max_calls=1,
        ),
    ),
)

PB_SYNTAX_FIX = Playbook(
    playbook_id="PB_syntax_fix",
    name="Syntax Error Fix",
    description=(
        "For SyntaxError. Locate the exact file"
        " and line, read a window around it,"
        " and apply a surgical fix."
    ),
    target_failures=("SyntaxError",),
    phases=(
        PlaybookStep(
            label="locate_file",
            step_type="repo_search",
            guidance=(
                "Search for the file mentioned"
                " in the syntax error traceback."
            ),
            max_calls=1,
        ),
        PlaybookStep(
            label="read_window",
            step_type="repo_read_range",
            guidance=(
                "Read a ±30-line window around"
                " the error line to understand"
                " the syntax issue."
            ),
            max_calls=2,
        ),
        PlaybookStep(
            label="patch_syntax",
            step_type="apply_patch",
            guidance=(
                "Fix the syntax error with a"
                " minimal patch. Do NOT refactor."
            ),
            max_calls=1,
        ),
        PlaybookStep(
            label="verify",
            step_type="run_tests",
            guidance=(
                "Run targeted tests then full"
                " suite to confirm fix."
            ),
            max_calls=2,
        ),
    ),
)

PB_TRACEBACK_FIX = Playbook(
    playbook_id="PB_traceback_fix",
    name="Traceback-Driven Fix",
    description=(
        "For TypeError, ValueError, KeyError,"
        " IndexError, AttributeError."
        " Use the error traceback to locate"
        " the exact fault, read context,"
        " and patch."
    ),
    target_failures=(
        "TypeError", "ValueError",
        "KeyError", "IndexError",
        "AttributeError",
    ),
    phases=(
        PlaybookStep(
            label="search_error_site",
            step_type="repo_search",
            guidance=(
                "Search for the function or"
                " symbol mentioned in the"
                " traceback."
            ),
            max_calls=2,
        ),
        PlaybookStep(
            label="read_error_site",
            step_type="repo_read_range",
            guidance=(
                "Read the code at the error site"
                " to understand the root cause."
            ),
            max_calls=3,
        ),
        PlaybookStep(
            label="patch_fix",
            step_type="apply_patch",
            guidance=(
                "Apply a targeted fix at the"
                " error site. Minimal diff."
                " No refactoring."
            ),
            max_calls=2,
        ),
        PlaybookStep(
            label="targeted_test",
            step_type="run_tests",
            guidance=(
                "Run targeted tests to verify"
                " the fix."
            ),
            max_calls=1,
        ),
        PlaybookStep(
            label="suite_test",
            step_type="run_tests",
            guidance=(
                "Run full suite to confirm"
                " no regressions."
            ),
            max_calls=1,
        ),
    ),
)

PB_GENERIC_FIX = Playbook(
    playbook_id="PB_generic_fix",
    name="Generic Search-Read-Patch",
    description=(
        "General-purpose playbook for unknown"
        " or unclassified errors."
        " Search → read → patch → test."
    ),
    target_failures=(),  # fallback for any
    phases=(
        PlaybookStep(
            label="search",
            step_type="repo_search",
            guidance=(
                "Search for code relevant to"
                " the task or error."
            ),
            max_calls=3,
        ),
        PlaybookStep(
            label="read",
            step_type="repo_read_range",
            guidance=(
                "Read relevant source files"
                " to understand the codebase."
            ),
            max_calls=4,
        ),
        PlaybookStep(
            label="patch",
            step_type="apply_patch",
            guidance=(
                "Apply a minimal patch."
                " No refactoring."
                " Keep diff small."
            ),
            max_calls=2,
        ),
        PlaybookStep(
            label="targeted_test",
            step_type="run_tests",
            guidance=(
                "Run pytest_targeted first."
            ),
            max_calls=1,
        ),
        PlaybookStep(
            label="suite_test",
            step_type="run_tests",
            guidance=(
                "Run full suite to confirm"
                " no regressions."
            ),
            max_calls=1,
        ),
    ),
)


# ─────────────────────────────────────────────
#  Registry
# ─────────────────────────────────────────────

# Ordered list — this is the set of bandit arms.
PLAYBOOKS: list[Playbook] = [
    PB_IMPORT_FIX,
    PB_ASSERTION_FIX,
    PB_SYNTAX_FIX,
    PB_TRACEBACK_FIX,
    PB_GENERIC_FIX,
]

PLAYBOOK_IDS: list[str] = [
    pb.playbook_id for pb in PLAYBOOKS
]

PLAYBOOK_MAP: dict[str, Playbook] = {
    pb.playbook_id: pb for pb in PLAYBOOKS
}

# Build failure-class → playbook prior mapping.
# When the bandit has zero data, pick the playbook
# designed for that failure class.
FAILURE_PLAYBOOK_PRIORS: dict[str, str] = {}
for _pb in PLAYBOOKS:
    for _fc in _pb.target_failures:
        if _fc not in FAILURE_PLAYBOOK_PRIORS:
            FAILURE_PLAYBOOK_PRIORS[_fc] = (
                _pb.playbook_id
            )


def get_playbook(playbook_id: str) -> Optional[Playbook]:
    """Look up a playbook by ID."""
    return PLAYBOOK_MAP.get(playbook_id)


def best_playbook_for_failure(
    failure_class: str,
) -> str:
    """Return the best prior playbook ID for a
    failure class, or the generic fallback."""
    return FAILURE_PLAYBOOK_PRIORS.get(
        failure_class, PB_GENERIC_FIX.playbook_id,
    )
