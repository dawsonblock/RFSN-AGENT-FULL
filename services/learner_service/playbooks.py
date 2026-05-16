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

    label: str  # human-readable name
    step_type: str  # kernel step type
    guidance: str  # instruction for the LLM
    max_calls: int = 1  # how many calls allowed


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
            calls = f" (up to {ph.max_calls}x)" if ph.max_calls > 1 else ""
            lines.append(f"  {i}. [{ph.step_type}]{calls}" f" — {ph.guidance}")
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
        "ImportError",
        "ModuleNotFoundError",
    ),
    phases=(
        PlaybookStep(
            label="locate_import",
            step_type="repo_search",
            guidance=(
                "Search for the missing module or" " import statement in the codebase."
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
            guidance=("Run targeted tests to verify the" " import error is resolved."),
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
            guidance=("Run the full test suite to" " confirm no regressions."),
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
        "AssertionError",
        "AssertionError",
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
            guidance=("Read the failing test to" " understand what it expects."),
            max_calls=2,
        ),
        PlaybookStep(
            label="find_impl",
            step_type="repo_search",
            guidance=(
                "Search for the symbol/function" " under test in the source code."
            ),
            max_calls=2,
        ),
        PlaybookStep(
            label="read_impl",
            step_type="repo_read_range",
            guidance=("Read the implementation to" " understand the bug."),
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
            guidance=("Run the failing test(s) with" " pytest_targeted to verify fix."),
            max_calls=1,
        ),
        PlaybookStep(
            label="suite_test",
            step_type="run_tests",
            guidance=("Run full suite to confirm" " no regressions."),
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
                "Search for the file mentioned" " in the syntax error traceback."
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
            guidance=("Fix the syntax error with a" " minimal patch. Do NOT refactor."),
            max_calls=1,
        ),
        PlaybookStep(
            label="verify",
            step_type="run_tests",
            guidance=("Run targeted tests then full" " suite to confirm fix."),
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
        "TypeError",
        "ValueError",
        "KeyError",
        "IndexError",
        "AttributeError",
    ),
    phases=(
        PlaybookStep(
            label="search_error_site",
            step_type="repo_search",
            guidance=(
                "Search for the function or" " symbol mentioned in the" " traceback."
            ),
            max_calls=2,
        ),
        PlaybookStep(
            label="read_error_site",
            step_type="repo_read_range",
            guidance=(
                "Read the code at the error site" " to understand the root cause."
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
            guidance=("Run targeted tests to verify" " the fix."),
            max_calls=1,
        ),
        PlaybookStep(
            label="suite_test",
            step_type="run_tests",
            guidance=("Run full suite to confirm" " no regressions."),
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
            guidance=("Search for code relevant to" " the task or error."),
            max_calls=3,
        ),
        PlaybookStep(
            label="read",
            step_type="repo_read_range",
            guidance=("Read relevant source files" " to understand the codebase."),
            max_calls=4,
        ),
        PlaybookStep(
            label="patch",
            step_type="apply_patch",
            guidance=("Apply a minimal patch." " No refactoring." " Keep diff small."),
            max_calls=2,
        ),
        PlaybookStep(
            label="targeted_test",
            step_type="run_tests",
            guidance=("Run pytest_targeted first."),
            max_calls=1,
        ),
        PlaybookStep(
            label="suite_test",
            step_type="run_tests",
            guidance=("Run full suite to confirm" " no regressions."),
            max_calls=1,
        ),
    ),
)


# ─────────────────────────────────────────────
#  Registry & RAG
# ─────────────────────────────────────────────


class PlaybookRegistry:
    """Manages static and dynamic playbooks with RAG retrieval.

    Acts as the 'Active Experience Replay' store.
    """

    def __init__(self) -> None:
        self._playbooks: dict[str, Playbook] = {}
        self._failure_map: dict[str, str] = {}

        # Load static defaults
        self.register(PB_IMPORT_FIX)
        self.register(PB_ASSERTION_FIX)
        self.register(PB_SYNTAX_FIX)
        self.register(PB_TRACEBACK_FIX)
        self.register(PB_GENERIC_FIX)

    def register(self, playbook: Playbook) -> None:
        """Register a playbook."""
        self._playbooks[playbook.playbook_id] = playbook
        for fc in playbook.target_failures:
            # First-come-first-serve for failure mapping, or overwrite?
            # Let's preserve first generic match, but specialized ones might override.
            # For now, simple registration.
            if fc not in self._failure_map:
                self._failure_map[fc] = playbook.playbook_id

    def get(self, playbook_id: str) -> Optional[Playbook]:
        return self._playbooks.get(playbook_id)

    def best_for_failure(self, failure_class: str) -> str:
        return self._failure_map.get(failure_class, PB_GENERIC_FIX.playbook_id)

    def retrieve_relevant(self, query: str, limit: int = 3) -> list[Playbook]:
        """Retrieve playbooks relevant to a query (RAG).

        Uses keyword overlap + prioritization of failure class matches.
        """
        query_words = set(query.lower().split())
        scored = []

        for pb in self._playbooks.values():
            # Index text: name + description + phases
            content_parts = [pb.name, pb.description]
            for ph in pb.phases:
                content_parts.append(ph.label)
                content_parts.append(ph.guidance)

            full_text = " ".join(content_parts).lower()
            pb_words = set(full_text.split())

            if not pb_words:
                continue

            overlap = len(query_words & pb_words)
            score = overlap / len(query_words) if query_words else 0.0

            # Boost matches on failure classes (strong signal)
            for fc in pb.target_failures:
                if fc.lower() in query_words:
                    score += 0.5

            if score > 0.1:  # Filter low relevance
                scored.append((score, pb))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [pb for _, pb in scored[:limit]]

    def load_from_directory(self, directory: str) -> int:
        """Load dynamic playbooks from a directory of JSON/YAML files."""
        import os
        import json

        count = 0
        if not os.path.exists(directory):
            return 0

        for filename in os.listdir(directory):
            if filename.endswith(".json"):
                path = os.path.join(directory, filename)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        self._load_from_dict(data)
                        count += 1
                except Exception as e:
                    print(f"WARN: Failed to load playbook {filename}: {e}")
        return count

    def _load_from_dict(self, data: dict) -> None:
        """Parse dictionary into Playbook and register."""
        try:
            phases = []
            for p in data.get("phases", []):
                phases.append(
                    PlaybookStep(
                        label=p["label"],
                        step_type=p["step_type"],
                        guidance=p["guidance"],
                        max_calls=p.get("max_calls", 1),
                    )
                )

            pb = Playbook(
                playbook_id=data["playbook_id"],
                name=data["name"],
                description=data["description"],
                phases=tuple(phases),
                target_failures=tuple(data.get("target_failures", [])),
            )
            self.register(pb)
        except KeyError as e:
            print(f"WARN: Invalid playbook data: missing {e}")


# Global instance
REGISTRY = PlaybookRegistry()


def get_playbook(playbook_id: str) -> Optional[Playbook]:
    """Look up a playbook by ID."""
    return REGISTRY.get(playbook_id)


def best_playbook_for_failure(failure_class: str) -> str:
    """Return the best prior playbook ID for a failure class."""
    return REGISTRY.best_for_failure(failure_class)


def retrieve_playbooks(query: str, limit: int = 3) -> list[Playbook]:
    """RAG interface: Retrieve playbooks relevant to a query."""
    return REGISTRY.retrieve_relevant(query, limit)


# Convenience export: flat list of all registered playbooks.
PLAYBOOKS: list[Playbook] = list(REGISTRY._playbooks.values())

# Additional convenience exports expected by tests.
PLAYBOOK_IDS: list[str] = [pb.playbook_id for pb in PLAYBOOKS]
PLAYBOOK_MAP: dict[str, "Playbook"] = {pb.playbook_id: pb for pb in PLAYBOOKS}
FAILURE_PLAYBOOK_PRIORS: dict[str, str] = {
    "import_error_missing_module": "PB_import_fix",
    "assertion_failure": "PB_assertion_fix",
    "syntax_error": "PB_syntax_fix",
    "traceback": "PB_traceback_fix",
}
