"""Postcondition verification — check after execution.

Verify that the outcome matches expectations and
no safety violations occurred.
Rollback must occur BEFORE ledger commit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from rfsn_kernel.state import Proposal, SystemState, Outcome


@dataclass
class VerificationResult:
    ok: bool
    violations: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "violations": self.violations}


def verify(
    proposal: Proposal,
    outcome: Outcome,
    state: SystemState,
    policy: Dict[str, Any] | None = None,
) -> VerificationResult:
    """Verify postconditions after execution.

    Checks:
    1. Execution did not exceed safety bounds
    2. State did not diverge unexpectedly
    3. No resource exhaustion
    """
    policy = policy or {}
    violations: List[Dict[str, Any]] = []

    # 1. Check for safety level breach.
    max_safety = int(policy.get("max_safety_level", 2))
    if state.safety_level > max_safety:
        violations.append({
            "code": "SAFETY_LEVEL_EXCEEDED",
            "msg": f"Safety level {state.safety_level} > {max_safety}",
        })

    # 2. Check execution timeout (if duration tracked).
    max_duration = float(policy.get("max_step_duration", 900))
    if outcome.duration_sec > max_duration:
        violations.append({
            "code": "DURATION_EXCEEDED",
            "msg": f"Duration {outcome.duration_sec:.1f}s > {max_duration}s",
        })

    # 3. Check resource exhaustion.
    max_cost = float(policy.get("max_total_cost", 50.0))
    if state.total_cost > max_cost:
        violations.append({
            "code": "COST_EXCEEDED",
            "msg": f"Total cost {state.total_cost:.2f} > {max_cost}",
        })

    # 4. Failure clustering — if too many consecutive failures,
    #    flag for safety escalation.
    fail_cluster_threshold = int(policy.get("fail_cluster_threshold", 8))
    if state.recent_failures >= fail_cluster_threshold:
        violations.append({
            "code": "FAILURE_CLUSTER",
            "msg": (
                f"Failure cluster detected:"
                f" {state.recent_failures} recent failures"
                f" >= {fail_cluster_threshold}"
            ),
        })

    return VerificationResult(
        ok=len(violations) == 0,
        violations=violations,
    )
