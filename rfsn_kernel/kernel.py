"""Hard RFSN Kernel — the ONLY path to execution.

State machine:
  IDLE → VALIDATE → SIMULATE → RISK → DECIDE
  → EXECUTE → VERIFY → COMMIT

No other component can execute tools.
If any step fails → abort, no side effects.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from rfsn_kernel.state import Proposal, SystemState, Outcome
from rfsn_kernel.normalize import normalize, proposal_to_step
from rfsn_kernel.validate import validate, ValidationResult
from rfsn_kernel.simulate import simulate, SimResult, OutcomeHistory
from rfsn_kernel.risk import risk_score, RiskBreakdown
from rfsn_kernel.decide import decide, Decision
from rfsn_kernel.verify import verify, VerificationResult
from rfsn_kernel.hard_ledger import HardLedger, LedgerRecord


@dataclass
class KernelStepResult:
    """Result of one kernel step — everything recorded."""

    phase: str                    # final phase reached
    proposal: Proposal = None     # type: ignore[assignment]
    validation: ValidationResult = None  # type: ignore[assignment]
    simulation: SimResult = None  # type: ignore[assignment]
    risk: RiskBreakdown = None    # type: ignore[assignment]
    decision: Decision = None     # type: ignore[assignment]
    outcome: Optional[Outcome] = None
    verification: Optional[VerificationResult] = None
    ledger_record: Optional[LedgerRecord] = None
    error: Optional[str] = None

    @property
    def approved(self) -> bool:
        return self.decision is not None and self.decision.approved

    @property
    def success(self) -> bool:
        return (
            self.outcome is not None
            and self.outcome.success
            and (self.verification is None or self.verification.ok)
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "phase": self.phase,
            "approved": self.approved,
            "success": self.success,
            "error": self.error,
            "decision_reason": (
                self.decision.reason if self.decision else None
            ),
            "risk": self.risk.to_dict() if self.risk else None,
            "simulation": self.simulation.to_dict() if self.simulation else None,
        }


# Execution callback type: takes a step dict,
# returns an Outcome. This is how the kernel
# delegates to the executor without coupling.
ExecuteCallback = Callable[[Dict[str, Any]], Outcome]


class HardKernel:
    """The single mandatory execution choke point.

    ALL tool execution MUST route through kernel_step().
    No exceptions.
    """

    def __init__(
        self,
        ledger_path: str = "/data/kernel_ledger.jsonl",
        policy: Dict[str, Any] | None = None,
    ) -> None:
        self.policy = policy or {}
        self.ledger = HardLedger(ledger_path)
        self.history = OutcomeHistory(
            max_entries=int(self.policy.get("history_max", 500)),
        )
        self.state = SystemState(
            rng_seed=int(self.policy.get("rng_seed", 42)),
            policy_hash=str(self.policy.get("policy_hash", "")),
        )
        self._step_count = 0

    def kernel_step(
        self,
        raw_step: Dict[str, Any],
        execute_fn: ExecuteCallback,
        context: str = "",
        intent: str = "",
        bundle_id: str = "",
    ) -> KernelStepResult:
        """The ONLY path to execution.

        Mandatory flow:
          1. Normalize
          2. Validate
          3. Simulate
          4. Risk score
          5. Decide
          6. Execute (if approved)
          7. Verify
          8. Ledger commit

        Returns KernelStepResult with full audit trail.
        """

        # ── 1. NORMALIZE ──
        proposal = normalize(
            raw_step, intent=intent,
            context_hash=context,
            bundle_id=bundle_id,
        )

        # ── 2. VALIDATE ──
        validation = validate(proposal, self.state, self.policy)
        if not validation.ok:
            record = self._commit_ledger(
                proposal, None, None, "REJECT",
                f"Validation failed: {validation.errors}",
                None, None,
            )
            return KernelStepResult(
                phase="VALIDATE",
                proposal=proposal,
                validation=validation,
                ledger_record=record,
                error="Validation failed",
            )

        # ── 3. SIMULATE ──
        sim = simulate(
            proposal, self.state,
            history=self.history,
            context=context,
        )

        # ── 4. RISK SCORE ──
        risk = risk_score(
            proposal, sim, self.state,
            policy=self.policy,
        )

        # ── 5. DECIDE ──
        decision = decide(proposal, sim, risk, self.policy)

        if not decision.approved:
            # Record rejection + update state.
            self.state.record_action(proposal.action)
            record = self._commit_ledger(
                proposal, sim, risk, "REJECT",
                decision.reason, None, None,
            )
            return KernelStepResult(
                phase="DECIDE",
                proposal=proposal,
                validation=validation,
                simulation=sim,
                risk=risk,
                decision=decision,
                ledger_record=record,
            )

        # ── 6. EXECUTE ──
        step_dict = proposal_to_step(proposal)
        try:
            outcome = execute_fn(step_dict)
        except Exception as exc:
            outcome = Outcome(
                success=False,
                exit_code=-1,
                error=str(exc),
            )

        # Update state based on outcome.
        self.state.advance_step()
        self.state.record_action(proposal.action)
        self.state.total_cost += sim.cost_est

        if outcome.success:
            self.state.record_success()
        else:
            self.state.record_failure()

        # Record in outcome history for future simulation.
        self.history.record(
            proposal.action, context,
            outcome.success, sim.cost_est,
        )

        # ── 7. VERIFY ──
        verification = verify(
            proposal, outcome, self.state, self.policy,
        )

        # Adaptive risk tightening: if verification
        # flags failure cluster, escalate safety level.
        if not verification.ok:
            for v in verification.violations:
                if v.get("code") == "FAILURE_CLUSTER":
                    self._adaptive_tighten()

        # ── 8. LEDGER COMMIT ──
        record = self._commit_ledger(
            proposal, sim, risk,
            "APPROVE", decision.reason,
            outcome, verification,
        )

        self._step_count += 1

        return KernelStepResult(
            phase="COMMIT",
            proposal=proposal,
            validation=validation,
            simulation=sim,
            risk=risk,
            decision=decision,
            outcome=outcome,
            verification=verification,
            ledger_record=record,
        )

    def _commit_ledger(
        self,
        proposal: Proposal,
        sim: Optional[SimResult],
        risk: Optional[RiskBreakdown],
        decision: str,
        reason: str,
        outcome: Optional[Outcome],
        verification: Optional[VerificationResult],
    ) -> LedgerRecord:
        """Write an immutable record to the ledger."""
        record = LedgerRecord(
            proposal_hash=proposal.deterministic_hash(),
            simulation=sim.to_dict() if sim else {},
            risk=risk.to_dict() if risk else {},
            decision=decision,
            decision_reason=reason,
            outcome_hash=(
                outcome.deterministic_hash() if outcome else None
            ),
            state_hash=self.state.deterministic_hash(),
            verification=(
                verification.to_dict() if verification else None
            ),
            metadata={
                "action": proposal.action,
                "intent": proposal.intent,
                "step_count": self.state.step_count,
            },
        )
        return self.ledger.append(record)

    def _adaptive_tighten(self) -> None:
        """Tighten safety envelope after failure cluster.

        This is the learner-driven adaptive risk mechanism.
        """
        self.state.safety_level = min(
            2, self.state.safety_level + 1,
        )
        # Tighten thresholds in policy.
        current_risk_max = float(self.policy.get("risk_max", 0.65))
        current_success_min = float(self.policy.get("success_min", 0.15))
        self.policy["risk_max"] = max(0.3, current_risk_max - 0.1)
        self.policy["success_min"] = min(0.5, current_success_min + 0.05)

    def adaptive_relax(self) -> None:
        """Relax safety envelope after sustained success.

        Called externally when consecutive successes detected.
        """
        if self.state.safety_level > 0:
            self.state.safety_level -= 1
        current_risk_max = float(self.policy.get("risk_max", 0.65))
        current_success_min = float(self.policy.get("success_min", 0.15))
        self.policy["risk_max"] = min(0.8, current_risk_max + 0.05)
        self.policy["success_min"] = max(0.1, current_success_min - 0.02)

    def reset_for_iteration(self) -> None:
        """Reset per-iteration state (not per-run)."""
        self.state.iter_count += 1
        self.state.recent_actions = []

    def get_stats(self) -> Dict[str, Any]:
        """Return current kernel statistics."""
        return {
            "step_count": self._step_count,
            "ledger_entries": self.ledger.count,
            "safety_level": self.state.safety_level,
            "recent_failures": self.state.recent_failures,
            "total_cost": round(self.state.total_cost, 4),
            "risk_max": float(self.policy.get("risk_max", 0.65)),
            "success_min": float(self.policy.get("success_min", 0.15)),
            "drift_variance": round(self.state.drift_variance, 6),
        }
