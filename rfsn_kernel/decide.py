"""Decision law — APPROVE or REJECT.

Simple but powerful: reject actions above risk
threshold, below success threshold, or in loop/drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

from rfsn_kernel.simulate import SimResult
from rfsn_kernel.risk import RiskBreakdown
from rfsn_kernel.state import Proposal


@dataclass
class Decision:
    approved: bool
    reason: str
    risk_breakdown: RiskBreakdown = None  # type: ignore[assignment]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "approved": self.approved,
            "reason": self.reason,
            "risk": self.risk_breakdown.to_dict() if self.risk_breakdown else None,
        }


def decide(
    proposal: Proposal,
    sim: SimResult,
    risk: RiskBreakdown,
    policy: Dict[str, Any] | None = None,
) -> Decision:
    """Approve or reject based on simulation + risk.

    Thresholds are configurable via policy.
    """
    policy = policy or {}

    risk_max = float(policy.get("risk_max", 0.65))
    success_min = float(policy.get("success_min", 0.15))
    loop_max = float(policy.get("loop_max", 0.8))
    drift_max = float(policy.get("drift_max", 0.85))

    # 1. Loop trap detection.
    if sim.loop_risk > loop_max:
        return Decision(
            approved=False,
            reason=f"Loop detected: loop_risk={sim.loop_risk:.2f} > {loop_max}",
            risk_breakdown=risk,
        )

    # 2. Drift detection.
    if sim.drift_risk > drift_max:
        return Decision(
            approved=False,
            reason=f"Drift detected: drift_risk={sim.drift_risk:.2f} > {drift_max}",
            risk_breakdown=risk,
        )

    # 3. Predicted failure mode: cluster failure.
    if sim.failure_mode == "cluster_failure":
        return Decision(
            approved=False,
            reason=f"Cluster failure predicted for {proposal.action}",
            risk_breakdown=risk,
        )

    # 4. Risk threshold.
    if risk.effective_risk > risk_max:
        return Decision(
            approved=False,
            reason=(
                f"Risk too high: effective={risk.effective_risk:.2f}"
                f" > {risk_max} (total={risk.total_risk:.2f}, ev={risk.ev_bonus:.2f})"
            ),
            risk_breakdown=risk,
        )

    # 5. Success probability floor.
    if sim.success_prob < success_min:
        return Decision(
            approved=False,
            reason=f"Success too low: {sim.success_prob:.2f} < {success_min}",
            risk_breakdown=risk,
        )

    return Decision(
        approved=True,
        reason="approved",
        risk_breakdown=risk,
    )
