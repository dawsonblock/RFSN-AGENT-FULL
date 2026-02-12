from __future__ import annotations

from typing import Dict


_PHASE_ORDER = [
    "SEARCH",
    "LOCALIZE",
    "PATCH",
    "VERIFY",
    "ISOLATE",
]


def next_phase(state: Dict[str, object]) -> str:
    """Deterministic phase transition for repair cycles."""
    phase = str(state.get("phase", "SEARCH"))
    last_status = int(state.get("last_status", 1) or 1)
    if last_status == 0 and phase in {"VERIFY", "ISOLATE"}:
        return "DONE"
    try:
        idx = _PHASE_ORDER.index(phase)
    except ValueError:
        return "SEARCH"
    if idx + 1 < len(_PHASE_ORDER):
        return _PHASE_ORDER[idx + 1]
    return "PATCH"


def should_retry(state: Dict[str, object]) -> bool:
    attempt = int(state.get("attempt", 0) or 0)
    max_attempts = int(state.get("max_attempts", 3) or 3)
    return attempt < max_attempts


def update_state(
    state: Dict[str, object],
    phase: str,
    status: int,
) -> Dict[str, object]:
    st = dict(state)
    st["phase"] = str(phase)
    st["last_status"] = int(status)
    if phase == "PATCH":
        st["attempt"] = int(st.get("attempt", 0) or 0) + 1
    return st
