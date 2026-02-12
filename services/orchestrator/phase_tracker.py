from __future__ import annotations

from typing import Dict, List, Set, Tuple


class RfsnPhase:
    IDLE = "IDLE"
    SEARCHING = "SEARCHING"
    READING = "READING"
    PATCHING = "PATCHING"
    TESTING = "TESTING"
    DEPS = "DEPS"
    DONE = "DONE"
    FAILED = "FAILED"


_STEP_TO_PHASE: Dict[str, str] = {
    "repo_search": RfsnPhase.SEARCHING,
    "repo_read_range": RfsnPhase.READING,
    "apply_patch": RfsnPhase.PATCHING,
    "run_tests": RfsnPhase.TESTING,
    "ensure_deps": RfsnPhase.DEPS,
}


_VALID_TRANSITIONS: Dict[str, Set[str]] = {
    RfsnPhase.IDLE: {
        RfsnPhase.SEARCHING,
        RfsnPhase.READING,
        RfsnPhase.PATCHING,
        RfsnPhase.DEPS,
    },
    RfsnPhase.SEARCHING: {
        RfsnPhase.SEARCHING,
        RfsnPhase.READING,
        RfsnPhase.PATCHING,
    },
    RfsnPhase.READING: {
        RfsnPhase.READING,
        RfsnPhase.SEARCHING,
        RfsnPhase.PATCHING,
    },
    RfsnPhase.PATCHING: {
        RfsnPhase.TESTING,
        RfsnPhase.PATCHING,
    },
    RfsnPhase.TESTING: {
        RfsnPhase.SEARCHING,
        RfsnPhase.READING,
        RfsnPhase.PATCHING,
        RfsnPhase.DONE,
    },
    RfsnPhase.DEPS: {
        RfsnPhase.SEARCHING,
        RfsnPhase.READING,
        RfsnPhase.PATCHING,
    },
    RfsnPhase.DONE: set(),
    RfsnPhase.FAILED: set(),
}


class PhaseTracker:
    """Tracks RFSN phase transitions per run."""

    def __init__(self) -> None:
        self._phase = RfsnPhase.IDLE
        self._history: List[str] = [RfsnPhase.IDLE]

    @property
    def phase(self) -> str:
        return self._phase

    @property
    def history(self) -> List[str]:
        return list(self._history)

    def check_transition(
        self, step_type: str,
    ) -> Tuple[bool, str]:
        target = _STEP_TO_PHASE.get(step_type)
        if target is None:
            return False, (
                f"Unknown step type: {step_type}"
            )

        allowed = _VALID_TRANSITIONS.get(
            self._phase, set(),
        )
        if target not in allowed:
            return False, (
                f"Invalid phase transition:"
                f" {self._phase} → {target}"
                f" (step_type={step_type})."
                f" Allowed targets:"
                f" {sorted(allowed)}"
            )
        return True, ""

    def advance(self, step_type: str) -> None:
        target = _STEP_TO_PHASE.get(
            step_type, self._phase,
        )
        self._phase = target
        self._history.append(target)

    def mark_done(self) -> None:
        self._phase = RfsnPhase.DONE
        self._history.append(RfsnPhase.DONE)

    def mark_failed(self) -> None:
        self._phase = RfsnPhase.FAILED
        self._history.append(RfsnPhase.FAILED)

    def reset(self) -> None:
        self._phase = RfsnPhase.IDLE
        self._history = [RfsnPhase.IDLE]
