"""Risk engine — multi-dimensional risk scoring.

Risk is COMPUTED, not guessed.
Kernel rejects actions above threshold.

Dimensions:
  - Execution risk (action danger level)
  - Environment risk (system instability)
  - Uncertainty (1 - success_prob)
  - Cost risk (resource exhaustion)
  - Loop risk (repeating ineffective actions)

Final risk = weighted sum of dimensions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from rfsn_kernel.state import Proposal, SystemState
from rfsn_kernel.simulate import SimResult


@dataclass
class RiskBreakdown:
    """Detailed risk breakdown for audit."""

    execution_risk: float
    environment_risk: float
    uncertainty_risk: float
    cost_risk: float
    loop_risk: float
    total_risk: float
    effective_risk: float   # after λ-weighting
    ev_bonus: float         # expected value offset

    def to_dict(self) -> Dict[str, Any]:
        return {
            "execution_risk": round(self.execution_risk, 4),
            "environment_risk": round(self.environment_risk, 4),
            "uncertainty_risk": round(self.uncertainty_risk, 4),
            "cost_risk": round(self.cost_risk, 4),
            "loop_risk": round(self.loop_risk, 4),
            "total_risk": round(self.total_risk, 4),
            "effective_risk": round(self.effective_risk, 4),
            "ev_bonus": round(self.ev_bonus, 4),
        }


# ── Execution risk by action type ──

_ACTION_RISK: Dict[str, float] = {
    "repo_search": 0.05,     # read-only, safe
    "repo_read_range": 0.02, # read-only, safe
    "read_file": 0.02,       # read-only, safe
    "detect_project": 0.03,  # read-only, safe
    "detect_workdirs": 0.05, # read-only directory walk
    "apply_patch": 0.6,      # mutates repo
    "run_tests": 0.15,       # runs subprocess
    "run_cmd_template": 0.2, # constrained command templates
    "format_fix": 0.25,      # mutates files via constrained templates
    "ensure_deps": 0.3,      # installs packages
}


def _execution_risk(proposal: Proposal) -> float:
    """Risk from the action type itself."""
    base = _ACTION_RISK.get(proposal.action, 0.3)
    # Larger patches are riskier.
    if proposal.action == "apply_patch":
        patch = proposal.params.get("patch", "") or ""
        lines = len(patch.splitlines())
        if lines > 100:
            base = min(1.0, base + 0.2)
        elif lines > 50:
            base = min(1.0, base + 0.1)
    return base


def _environment_risk(state: SystemState) -> float:
    """Risk from current system state (instability)."""
    risk = 0.0
    # High failure rate.
    if state.step_count > 0:
        fail_ratio = state.recent_failures / max(state.step_count, 1)
        risk += fail_ratio * 0.5
    # High drift.
    risk += state.drift_variance * 0.3
    # Safety level already elevated.
    risk += state.safety_level * 0.15
    return min(1.0, risk)


def _normalize_cost(cost: float) -> float:
    """Normalize cost to [0, 1] range."""
    # Cost > 1.0 is expensive.
    return min(1.0, cost / 2.0)


def risk_score(
    proposal: Proposal,
    sim: SimResult,
    state: SystemState,
    policy: Dict[str, Any] | None = None,
) -> RiskBreakdown:
    """Compute multi-dimensional risk score.

    R = w_exec * r_exec + w_env * r_env
      + w_unc * r_unc + w_cost * r_cost
      + w_loop * r_loop

    Effective risk = λ * R - (1-λ) * EV
    """
    policy = policy or {}

    r_exec = _execution_risk(proposal)
    r_env = _environment_risk(state)
    r_unc = 1.0 - sim.success_prob
    r_cost = _normalize_cost(sim.cost_est)
    r_loop = sim.loop_risk

    # Configurable weights.
    w_exec = float(policy.get("w_exec", 0.30))
    w_env = float(policy.get("w_env", 0.20))
    w_unc = float(policy.get("w_unc", 0.20))
    w_cost = float(policy.get("w_cost", 0.10))
    w_loop = float(policy.get("w_loop", 0.20))

    total = (
        w_exec * r_exec
        + w_env * r_env
        + w_unc * r_unc
        + w_cost * r_cost
        + w_loop * r_loop
    )
    total = min(1.0, total)

    # Expected value bonus for focused patches.
    ev = 0.0
    if proposal.action == "apply_patch":
        patch = proposal.params.get("patch", "") or ""
        lines = len(patch.splitlines())
        if lines <= 20:
            ev = 0.3   # small, focused patch
        elif lines <= 50:
            ev = 0.15  # medium patch
        else:
            ev = 0.05  # large patch

    lam = float(policy.get("risk_lambda", 0.7))
    effective = lam * total - (1 - lam) * ev

    return RiskBreakdown(
        execution_risk=r_exec,
        environment_risk=r_env,
        uncertainty_risk=r_unc,
        cost_risk=r_cost,
        loop_risk=r_loop,
        total_risk=total,
        effective_risk=effective,
        ev_bonus=ev,
    )
