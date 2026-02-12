"""Tests for the RFSN phase state machine."""
import os
import sys

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__),
        "..", "services", "orchestrator",
    ),
)
from phase_tracker import (  # noqa: E402
    PhaseTracker,
    RfsnPhase,
)


def test_initial_phase_is_idle():
    pt = PhaseTracker()
    assert pt.phase == RfsnPhase.IDLE


def test_idle_to_search():
    pt = PhaseTracker()
    ok, err = pt.check_transition("repo_search")
    assert ok, err
    pt.advance("repo_search")
    assert pt.phase == RfsnPhase.SEARCHING


def test_idle_to_deps():
    pt = PhaseTracker()
    ok, _ = pt.check_transition("ensure_deps")
    assert ok
    pt.advance("ensure_deps")
    assert pt.phase == RfsnPhase.DEPS


def test_search_to_read():
    pt = PhaseTracker()
    pt.advance("repo_search")
    ok, _ = pt.check_transition("repo_read_range")
    assert ok
    pt.advance("repo_read_range")
    assert pt.phase == RfsnPhase.READING


def test_search_to_patch():
    pt = PhaseTracker()
    pt.advance("repo_search")
    ok, _ = pt.check_transition("apply_patch")
    assert ok


def test_read_to_patch():
    pt = PhaseTracker()
    pt.advance("repo_search")
    pt.advance("repo_read_range")
    ok, _ = pt.check_transition("apply_patch")
    assert ok


def test_patch_to_test():
    pt = PhaseTracker()
    pt.advance("repo_search")
    pt.advance("apply_patch")
    ok, _ = pt.check_transition("run_tests")
    assert ok
    pt.advance("run_tests")
    assert pt.phase == RfsnPhase.TESTING


def test_patch_to_search_blocked():
    """After patching, must test first."""
    pt = PhaseTracker()
    pt.advance("repo_search")
    pt.advance("apply_patch")
    ok, err = pt.check_transition("repo_search")
    assert not ok
    assert "PATCHING" in err


def test_test_to_search_allowed():
    """After testing, can search again."""
    pt = PhaseTracker()
    pt.advance("repo_search")
    pt.advance("apply_patch")
    pt.advance("run_tests")
    ok, _ = pt.check_transition("repo_search")
    assert ok


def test_test_to_patch_allowed():
    """After testing, can try another patch."""
    pt = PhaseTracker()
    pt.advance("repo_search")
    pt.advance("apply_patch")
    pt.advance("run_tests")
    ok, _ = pt.check_transition("apply_patch")
    assert ok


def test_multiple_patches_allowed():
    """Multiple consecutive patches are valid
    (multi-file fix)."""
    pt = PhaseTracker()
    pt.advance("repo_search")
    pt.advance("apply_patch")
    ok, _ = pt.check_transition("apply_patch")
    assert ok
    pt.advance("apply_patch")
    assert pt.phase == RfsnPhase.PATCHING


def test_idle_to_test_blocked():
    """Cannot test without searching/patching."""
    pt = PhaseTracker()
    ok, err = pt.check_transition("run_tests")
    assert not ok
    assert "IDLE" in err


def test_deps_to_search():
    pt = PhaseTracker()
    pt.advance("ensure_deps")
    ok, _ = pt.check_transition("repo_search")
    assert ok


def test_history_tracking():
    pt = PhaseTracker()
    pt.advance("repo_search")
    pt.advance("apply_patch")
    pt.advance("run_tests")
    assert pt.history == [
        RfsnPhase.IDLE,
        RfsnPhase.SEARCHING,
        RfsnPhase.PATCHING,
        RfsnPhase.TESTING,
    ]


def test_reset():
    pt = PhaseTracker()
    pt.advance("repo_search")
    pt.advance("apply_patch")
    pt.reset()
    assert pt.phase == RfsnPhase.IDLE
    assert pt.history == [RfsnPhase.IDLE]


def test_mark_done():
    pt = PhaseTracker()
    pt.advance("repo_search")
    pt.advance("apply_patch")
    pt.advance("run_tests")
    pt.mark_done()
    assert pt.phase == RfsnPhase.DONE
    # DONE is terminal
    ok, err = pt.check_transition("repo_search")
    assert not ok


def test_mark_failed():
    pt = PhaseTracker()
    pt.mark_failed()
    assert pt.phase == RfsnPhase.FAILED
    ok, _ = pt.check_transition("apply_patch")
    assert not ok


def test_unknown_step_type():
    pt = PhaseTracker()
    ok, err = pt.check_transition("unknown_type")
    assert not ok
    assert "Unknown step type" in err
