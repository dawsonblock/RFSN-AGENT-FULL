"""Execution simulation — predictive control layer.

Runs BEFORE execution to predict outcome, detect
unsafe actions, estimate cost, and prevent drift loops.

No side effects. No ML required initially —
heuristic models produce major stability gains.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from rfsn_kernel.state import Proposal, SystemState
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rfsn_kernel.memory import MemoryEntry


@dataclass
class SimResult:
    """Simulation output — no execution occurred."""

    success_prob: float  # 0.0–1.0
    failure_mode: Optional[str] = None
    cost_est: float = 0.0  # estimated resource cost
    drift_risk: float = 0.0  # 0.0–1.0
    loop_risk: float = 0.0  # 0.0–1.0
    predicted_state_delta: Dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.predicted_state_delta is None:
            self.predicted_state_delta = {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success_prob": round(self.success_prob, 4),
            "failure_mode": self.failure_mode,
            "cost_est": round(self.cost_est, 4),
            "drift_risk": round(self.drift_risk, 4),
            "loop_risk": round(self.loop_risk, 4),
        }


# ── Outcome history (lightweight in-memory) ──


class OutcomeHistory:
    """Tracks recent action outcomes for simulation.

    This is the simulation engine's knowledge base —
    NOT the learner's DuckDB. Kept lightweight for
    fast predictive scoring.
    """

    def __init__(self, max_entries: int = 500) -> None:
        self._max = max_entries
        # key = (action, context_cluster) → stats
        self._stats: Dict[str, _ActionStats] = {}

    def record(
        self,
        action: str,
        context: str,
        success: bool,
        cost: float = 0.0,
    ) -> None:
        key = f"{action}|{context}"
        if key not in self._stats:
            self._stats[key] = _ActionStats()
        s = self._stats[key]
        s.n += 1
        s.success += int(success)
        s.fail += int(not success)
        s.total_cost += cost
        # EMA for recent failure rate.
        alpha = 0.3
        s.recent_failure_rate = (
            alpha * (0.0 if success else 1.0) + (1 - alpha) * s.recent_failure_rate
        )
        # Prune if too many keys.
        if len(self._stats) > self._max:
            # Drop least-used entries — O(n) via heapq vs O(n log n) sort.
            import heapq

            n_drop = len(self._stats) // 4
            to_drop = heapq.nsmallest(
                n_drop,
                self._stats,
                key=lambda k: self._stats[k].n,
            )
            for k in to_drop:
                del self._stats[k]

    def lookup(self, action: str, context: str) -> Optional["_ActionStats"]:
        return self._stats.get(f"{action}|{context}")


class _ActionStats:
    __slots__ = ("n", "success", "fail", "total_cost", "recent_failure_rate")

    def __init__(self) -> None:
        self.n = 0
        self.success = 0
        self.fail = 0
        self.total_cost = 0.0
        self.recent_failure_rate = 0.0

    @property
    def success_rate(self) -> float:
        return self.success / max(self.n, 1)

    @property
    def avg_cost(self) -> float:
        return self.total_cost / max(self.n, 1)


# ── Cost estimation ──

_BASE_COSTS: Dict[str, float] = {
    "repo_search": 0.05,
    "repo_read_range": 0.03,
    "read_file": 0.02,
    "detect_project": 0.03,
    "detect_workdirs": 0.06,
    "apply_patch": 0.3,
    "run_tests": 0.5,
    "run_cmd_template": 0.4,
    "format_fix": 0.35,
    "ensure_deps": 0.2,
}


def _estimate_cost(proposal: Proposal) -> float:
    """Estimate resource cost of an action."""
    base = _BASE_COSTS.get(proposal.action, 0.1)
    # Larger patches cost more.
    if proposal.action == "apply_patch":
        patch = proposal.params.get("patch", "") or ""
        lines = len(patch.splitlines())
        base += lines * 0.005
    # Longer test runs cost more.
    if proposal.action == "run_tests":
        timeout = int(proposal.params.get("timeout_s", 240))
        base += timeout * 0.0005
    return round(base, 4)


def _detect_loop(
    proposal: Proposal,
    state: SystemState,
) -> float:
    """Detect if we're repeating the same action without progress.

    Returns loop_risk in [0.0, 1.0].
    """
    if not state.recent_actions:
        return 0.0

    action_key = proposal.action
    # Count how many of the last N actions were the same type.
    recent = state.recent_actions[-10:]
    same_count = sum(1 for a in recent if a == action_key)

    # Consecutive identical actions are worse.
    consecutive = 0
    for a in reversed(recent):
        if a == action_key:
            consecutive += 1
        else:
            break

    if consecutive >= 4:
        return 0.95  # almost certainly a loop
    if consecutive >= 3:
        return 0.7
    if same_count >= 6:
        return 0.6
    if same_count >= 4:
        return 0.3
    return 0.0


def _detect_drift(state: SystemState) -> float:
    """Estimate drift risk from state variance.

    High recent_failures + high step count = drift.
    """
    if state.step_count == 0:
        return 0.0

    failure_ratio = state.recent_failures / max(state.step_count, 1)
    # Drift increases with failures and step count.
    drift = min(1.0, failure_ratio * 2.0 + state.drift_variance)
    return round(drift, 4)


def _predict_failure_mode(
    proposal: Proposal,
    state: SystemState,
    history: Optional[OutcomeHistory] = None,
    context: str = "",
) -> Optional[str]:
    """Predict the most likely failure mode."""
    if history:
        stats = history.lookup(proposal.action, context)
        if stats and stats.recent_failure_rate > 0.7:
            return "cluster_failure"
        if stats and stats.avg_cost > 1.0:
            return "resource_failure"

    # High recent failures suggest instability.
    if state.recent_failures >= 5:
        return "instability"

    return None


def simulate(
    proposal: Proposal,
    state: SystemState,
    history: Optional[OutcomeHistory] = None,
    context: str = "",
    prior_success_prob: Optional[float] = None,
    prior_trials: int = 0,
    prior_loop_risk: Optional[float] = None,
    known_traps: Optional[list[str]] = None,
    memory_entries: Optional[list["MemoryEntry"]] = None,
) -> SimResult:
    """Run a fast predictive model — no side effects.

    Uses outcome history + state to predict:
    - Success probability
    - Failure mode
    - Resource cost
    - Drift risk
    - Loop risk
    """
    # Base success probability by action type.
    base_probs: Dict[str, float] = {
        "repo_search": 0.85,
        "repo_read_range": 0.95,
        "read_file": 0.96,
        "detect_project": 0.95,
        "detect_workdirs": 0.9,
        "apply_patch": 0.6,
        "run_tests": 0.5,
        "run_cmd_template": 0.55,
        "format_fix": 0.7,
        "ensure_deps": 0.8,
    }
    success_prob = base_probs.get(proposal.action, 0.5)

    # Adjust by history if available.
    if history:
        stats = history.lookup(proposal.action, context)
        if stats and stats.n >= 3:
            # Blend base prior with observed rate.
            weight = min(stats.n / 10.0, 1.0)
            success_prob = (1 - weight) * success_prob + weight * stats.success_rate

    # Blend in learner evidence as an upstream prior.
    if prior_success_prob is not None:
        prior = max(
            0.01,
            min(1.0, float(prior_success_prob)),
        )
        confidence = max(
            0.0,
            min(1.0, float(prior_trials) / 20.0),
        )
        weight = 0.25 + (0.5 * confidence)
        success_prob = (1.0 - weight) * success_prob + weight * prior

    # Penalize for high recent failure count.
    if state.recent_failures >= 3:
        success_prob *= 0.7
    if state.recent_failures >= 6:
        success_prob *= 0.5

    # Safety level penalty.
    if state.safety_level >= 1:
        success_prob *= 0.9

    # Known trap penalty.
    if known_traps:
        # Check if the proposal matches any trap.
        # Traps can be specific actions or strategies.
        # Since 'strategy' isn't on the proposal, we mostly check action based traps if supported.
        # But if the orchestrator passes "strategy:XYZ" and we know this step belongs to it...
        # Actually, simulate() is tactical.
        # Let's assume traps might be "action:run_tests" or similar.
        action_trap = f"action:{proposal.action}"
        if action_trap in known_traps:
            success_prob *= 0.1
        # Also generic string matching for now (e.g. if we trapped 'run_tests')
        if proposal.action in known_traps:
            success_prob *= 0.1

    success_prob = max(0.01, min(1.0, success_prob))

    # Memory-informed prediction boost.
    if memory_entries:
        total_s = 0
        total_f = 0
        for me in memory_entries:
            if proposal.action in me.content.lower():
                total_s += me.success_count
                total_f += me.failure_count
        total_obs = total_s + total_f
        if total_obs >= 2:
            memory_rate = total_s / total_obs
            # Blend 15% memory signal.
            success_prob = 0.85 * success_prob + 0.15 * memory_rate
            success_prob = max(0.01, min(1.0, success_prob))

    cost_est = _estimate_cost(proposal)
    loop_risk = _detect_loop(proposal, state)
    if prior_loop_risk is not None:
        loop_risk = max(
            loop_risk,
            max(0.0, min(1.0, float(prior_loop_risk))),
        )
    drift_risk = _detect_drift(state)
    failure_mode = _predict_failure_mode(
        proposal,
        state,
        history,
        context,
    )

    return SimResult(
        success_prob=round(success_prob, 4),
        failure_mode=failure_mode,
        cost_est=cost_est,
        drift_risk=drift_risk,
        loop_risk=loop_risk,
    )
