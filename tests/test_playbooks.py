"""Tests for the playbook catalog and failure router."""
import os
import sys

import pytest

# Wire up learner_service imports.
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__),
        "..", "services", "learner_service",
    ),
)

from playbooks import (  # noqa: E402  # type: ignore[import-not-found]
    PLAYBOOKS,
    PLAYBOOK_IDS,
    PLAYBOOK_MAP,
    FAILURE_PLAYBOOK_PRIORS,
    Playbook,
    PlaybookStep,
    PB_IMPORT_FIX,
    PB_ASSERTION_FIX,
    PB_SYNTAX_FIX,
    PB_TRACEBACK_FIX,
    PB_GENERIC_FIX,
    get_playbook,
    best_playbook_for_failure,
)


# ── Catalog integrity ────────────────────────


class TestPlaybookCatalog:
    """Verify the catalog is well-formed."""

    def test_five_playbooks_registered(self):
        assert len(PLAYBOOKS) == 5

    def test_ids_match_objects(self):
        for pb in PLAYBOOKS:
            assert pb.playbook_id in PLAYBOOK_MAP
            assert PLAYBOOK_MAP[pb.playbook_id] is pb

    def test_ids_list_matches_objects(self):
        assert PLAYBOOK_IDS == [
            pb.playbook_id for pb in PLAYBOOKS
        ]

    def test_all_ids_unique(self):
        assert len(set(PLAYBOOK_IDS)) == len(PLAYBOOK_IDS)

    def test_every_playbook_has_phases(self):
        for pb in PLAYBOOKS:
            assert len(pb.phases) > 0, (
                f"{pb.playbook_id} has no phases"
            )

    def test_every_phase_has_valid_step_type(self):
        valid_types = {
            "repo_search",
            "repo_read_range",
            "apply_patch",
            "ensure_deps",
            "run_tests",
        }
        for pb in PLAYBOOKS:
            for ph in pb.phases:
                assert ph.step_type in valid_types, (
                    f"{pb.playbook_id}.{ph.label}"
                    f" has invalid type:"
                    f" {ph.step_type}"
                )

    def test_every_playbook_ends_with_tests(self):
        """All playbooks should end by running tests."""
        for pb in PLAYBOOKS:
            last = pb.phases[-1]
            assert last.step_type == "run_tests", (
                f"{pb.playbook_id} does not end"
                f" with run_tests"
            )

    def test_playbooks_are_frozen(self):
        """Playbooks should be immutable."""
        with pytest.raises(
            AttributeError,
        ):
            PB_GENERIC_FIX.name = "hacked"  # type: ignore[misc]


# ── Failure routing priors ───────────────────


class TestFailureRouter:
    """Verify the failure-class → playbook mapping."""

    def test_import_error_routes_to_import_fix(self):
        assert (
            best_playbook_for_failure("ImportError")
            == "PB_import_fix"
        )

    def test_module_not_found_routes_to_import(self):
        assert (
            best_playbook_for_failure(
                "ModuleNotFoundError"
            )
            == "PB_import_fix"
        )

    def test_syntax_error_routes_to_syntax_fix(self):
        assert (
            best_playbook_for_failure("SyntaxError")
            == "PB_syntax_fix"
        )

    def test_type_error_routes_to_traceback(self):
        assert (
            best_playbook_for_failure("TypeError")
            == "PB_traceback_fix"
        )

    def test_value_error_routes_to_traceback(self):
        assert (
            best_playbook_for_failure("ValueError")
            == "PB_traceback_fix"
        )

    def test_key_error_routes_to_traceback(self):
        assert (
            best_playbook_for_failure("KeyError")
            == "PB_traceback_fix"
        )

    def test_index_error_routes_to_traceback(self):
        assert (
            best_playbook_for_failure("IndexError")
            == "PB_traceback_fix"
        )

    def test_attribute_error_routes_to_traceback(
        self,
    ):
        assert (
            best_playbook_for_failure(
                "AttributeError"
            )
            == "PB_traceback_fix"
        )

    def test_assertion_error_routes_to_assertion(
        self,
    ):
        assert (
            best_playbook_for_failure(
                "AssertionError"
            )
            == "PB_assertion_fix"
        )

    def test_unknown_error_falls_back_to_generic(
        self,
    ):
        assert (
            best_playbook_for_failure(
                "WeirdCustomError"
            )
            == "PB_generic_fix"
        )

    def test_empty_class_falls_back_to_generic(self):
        assert (
            best_playbook_for_failure("")
            == "PB_generic_fix"
        )

    def test_all_priors_map_to_valid_playbooks(self):
        for fc, pid in FAILURE_PLAYBOOK_PRIORS.items():
            assert pid in PLAYBOOK_MAP, (
                f"Prior for {fc} maps to"
                f" unknown playbook: {pid}"
            )


# ── Playbook lookup ──────────────────────────


class TestPlaybookLookup:
    def test_get_playbook_by_id(self):
        pb = get_playbook("PB_import_fix")
        assert pb is PB_IMPORT_FIX

    def test_get_playbook_returns_none_for_unknown(
        self,
    ):
        assert get_playbook("PB_nonexistent") is None

    def test_all_ids_are_retrievable(self):
        for pid in PLAYBOOK_IDS:
            assert get_playbook(pid) is not None


# ── Prompt addendum generation ───────────────


class TestPlaybookPrompt:
    """Verify prompt_addendum output is well-formed."""

    def test_addendum_contains_playbook_name(self):
        text = PB_IMPORT_FIX.prompt_addendum
        assert "Import / Dependency Fix" in text

    def test_addendum_contains_phase_steps(self):
        text = PB_IMPORT_FIX.prompt_addendum
        assert "[repo_search]" in text
        assert "[repo_read_range]" in text
        assert "[ensure_deps]" in text
        assert "[run_tests]" in text

    def test_addendum_contains_phase_numbers(self):
        text = PB_ASSERTION_FIX.prompt_addendum
        assert "1." in text
        assert "2." in text
        # Should have at least 5 phases.
        assert "5." in text

    def test_addendum_contains_do_not_skip(self):
        for pb in PLAYBOOKS:
            text = pb.prompt_addendum
            assert "Do NOT skip phases" in text, (
                f"{pb.playbook_id} missing"
                " do-not-skip instruction"
            )

    def test_addendum_is_non_empty_for_all(self):
        for pb in PLAYBOOKS:
            assert len(pb.prompt_addendum) > 50, (
                f"{pb.playbook_id} has"
                " suspiciously short addendum"
            )

    def test_multi_call_phases_show_max(self):
        """Phases with max_calls > 1 should say so."""
        text = PB_IMPORT_FIX.prompt_addendum
        assert "(up to 2x)" in text


# ── Specific playbook structure ──────────────


class TestImportPlaybook:
    def test_starts_with_search(self):
        assert (
            PB_IMPORT_FIX.phases[0].step_type
            == "repo_search"
        )

    def test_has_ensure_deps_phase(self):
        dep_phases = [
            p for p in PB_IMPORT_FIX.phases
            if p.step_type == "ensure_deps"
        ]
        assert len(dep_phases) == 1

    def test_targets_import_errors(self):
        assert "ImportError" in (
            PB_IMPORT_FIX.target_failures
        )
        assert "ModuleNotFoundError" in (
            PB_IMPORT_FIX.target_failures
        )


class TestAssertionPlaybook:
    def test_search_then_read_then_search_impl(self):
        types = [
            p.step_type
            for p in PB_ASSERTION_FIX.phases
        ]
        # search test -> read test -> search impl
        # -> read impl -> patch -> test -> test
        assert types[:4] == [
            "repo_search",
            "repo_read_range",
            "repo_search",
            "repo_read_range",
        ]

    def test_patch_before_tests(self):
        types = [
            p.step_type
            for p in PB_ASSERTION_FIX.phases
        ]
        patch_idx = types.index("apply_patch")
        test_indices = [
            i for i, t in enumerate(types)
            if t == "run_tests"
        ]
        assert all(
            ti > patch_idx for ti in test_indices
        )


class TestSyntaxPlaybook:
    def test_minimal_phases(self):
        # Syntax fix should be short and surgical.
        assert len(PB_SYNTAX_FIX.phases) <= 5

    def test_targets_syntax_error(self):
        assert "SyntaxError" in (
            PB_SYNTAX_FIX.target_failures
        )


class TestTracebackPlaybook:
    def test_targets_multiple_error_types(self):
        assert len(
            PB_TRACEBACK_FIX.target_failures
        ) >= 4

    def test_no_ensure_deps_phase(self):
        """Traceback fix shouldn't install deps."""
        dep_phases = [
            p for p in PB_TRACEBACK_FIX.phases
            if p.step_type == "ensure_deps"
        ]
        assert len(dep_phases) == 0


class TestGenericPlaybook:
    def test_no_target_failures(self):
        """Generic is a fallback for any error."""
        assert PB_GENERIC_FIX.target_failures == ()

    def test_has_search_read_patch_test_flow(self):
        types = [
            p.step_type
            for p in PB_GENERIC_FIX.phases
        ]
        assert "repo_search" in types
        assert "repo_read_range" in types
        assert "apply_patch" in types
        assert "run_tests" in types
