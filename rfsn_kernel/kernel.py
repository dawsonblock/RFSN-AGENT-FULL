"""Hard RFSN Kernel — the ONLY path to execution.

State machine:
  IDLE → VALIDATE → SIMULATE → RISK → DECIDE
  → EXECUTE → VERIFY → COMMIT

No other component can execute tools.
If any step fails → abort, no side effects.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional
import yaml

from rfsn_kernel.state import Proposal, SystemState, Outcome
from rfsn_kernel.normalize import normalize, proposal_to_step
from rfsn_kernel.validate import validate, ValidationResult
from rfsn_kernel.simulate import simulate, SimResult, OutcomeHistory
from rfsn_kernel.risk import risk_score, RiskBreakdown
from rfsn_kernel.decide import decide, Decision
from rfsn_kernel.verify import verify, VerificationResult
from rfsn_kernel.hard_ledger import HardLedger, LedgerRecord
from rfsn_kernel.tier_policy import (
    tier_allows_step,
    pick_next_tier,
    step_touches,
)
from rfsn_kernel.failure_kinds import extract_failure_kinds
from rfsn_kernel.run_state import RunStateStore


@dataclass
class KernelStepResult:
    """Result of one kernel step — everything recorded."""

    phase: str  # final phase reached
    proposal: Proposal = None  # type: ignore[assignment]
    validation: ValidationResult = None  # type: ignore[assignment]
    simulation: SimResult = None  # type: ignore[assignment]
    risk: RiskBreakdown = None  # type: ignore[assignment]
    decision: Decision = None  # type: ignore[assignment]
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
            "decision_reason": (self.decision.reason if self.decision else None),
            "risk": self.risk.to_dict() if self.risk else None,
            "simulation": (self.simulation.to_dict() if self.simulation else None),
        }


# Execution callback type: takes a step dict,
# returns an Outcome. This is how the kernel
# delegates to the executor without coupling.
ExecuteCallback = Callable[[Dict[str, Any]], Outcome]


_DEFAULT_TIER_POLICY: Dict[str, Any] = {
    "tiers": {
        0: {
            "name": "code-only",
            "allow": {
                "edit_tests": False,
                "edit_deps": False,
                "edit_ci": False,
            },
            "budgets": {},
        },
        1: {
            "name": "tests-allowed",
            "allow": {
                "edit_tests": True,
                "edit_deps": False,
                "edit_ci": False,
            },
            "budgets": {},
        },
        2: {
            "name": "deps-allowed",
            "allow": {
                "edit_tests": True,
                "edit_deps": True,
                "edit_ci": False,
            },
            "budgets": {},
        },
        3: {
            "name": "ci-allowed",
            "allow": {
                "edit_tests": True,
                "edit_deps": True,
                "edit_ci": True,
            },
            "budgets": {},
        },
    },
    "escalation_rules": {
        "to_tier_1": {
            "requires_any": [
                {"failure_kind": "tests_failed"},
            ],
        },
        "to_tier_2": {
            "requires_any": [
                {"failure_kind": "deps_install_failed"},
                {"failure_kind": "import_error_missing_module"},
                {"failure_kind": "build_system_missing_dependency"},
            ],
        },
        "to_tier_3": {
            "requires_any": [
                {"failure_kind": "ci_failed"},
                {"failure_kind": "ci_config_invalid"},
                {"failure_kind": "ci_env_mismatch"},
            ],
        },
    },
    "classifiers": {
        "tests_globs": ["**/tests/**"],
        "deps_globs": ["**/requirements.txt", "**/pyproject.toml"],
        "ci_globs": ["**/.github/workflows/**"],
    },
}


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
        self.tier_policy_path = os.environ.get(
            "RFSN_TIER_POLICY_PATH",
            "/policies/gate_policy_tiers.yaml",
        )
        self.tier_policy = self._load_tier_policy(
            self.tier_policy_path,
        )
        self.classifiers = self.tier_policy.get("classifiers") or {}
        self.run_state = RunStateStore()
        self._step_count = 0

        # MCTS / Anti-Looping State
        self.consecutive_failures = 0
        self.active_hypothesis_hash = ""

    def _check_failure_escalation(self, run_id: str):
        """Detect consecutive failures and signal strategy change.

        After 3 consecutive execution failures, resets the counter
        and returns True so the caller can inject a strategy-change
        signal into the outcome.
        """
        import logging

        if self.consecutive_failures >= 3:
            logging.warning(
                "Kernel: 3 consecutive failures for run %s — escalating strategy.",
                run_id,
            )
            self.consecutive_failures = 0
            return True
        return False

    def _load_tier_policy(self, path: str) -> Dict[str, Any]:
        """Load tier policy from YAML file, falling back to defaults."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            if not isinstance(data, dict):
                return dict(_DEFAULT_TIER_POLICY)
            # Normalize tier keys to ints.
            tiers = data.get("tiers", {})
            fixed_tiers: Dict[int, Dict[str, Any]] = {}
            if isinstance(tiers, dict):
                for k, v in tiers.items():
                    try:
                        ik = int(k)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(v, dict):
                        fixed_tiers[ik] = v
            data["tiers"] = fixed_tiers or dict(
                _DEFAULT_TIER_POLICY["tiers"],
            )
            return data
        except Exception:
            return dict(_DEFAULT_TIER_POLICY)

    def _tier_cfg(self, tier: int) -> Dict[str, Any]:
        tiers = self.tier_policy.get("tiers", {})
        if not isinstance(tiers, dict):
            return {}
        cfg = tiers.get(tier)
        if isinstance(cfg, dict):
            return cfg
        # Handle string-keyed tiers in external policy.
        cfg = tiers.get(str(tier))
        if isinstance(cfg, dict):
            return cfg
        return {}

    def _reject_tier_budget(
        self,
        reason: str,
        proposal: "Proposal",
        run_id: str,
        intent: str,
        bundle_id: str,
        tier: int,
    ) -> KernelStepResult:
        """Reject a step due to tier budget violation.

        Centralizes the event-append + ledger-commit + result-build
        pattern used by all tier budget checks.
        """
        self._append_kernel_event(
            event_type="TIER_STEP_REJECTED",
            run_id=run_id,
            intent=intent,
            bundle_id=bundle_id,
            fields={"tier": tier, "reason": reason},
        )
        record = self._commit_ledger(
            proposal, None, None, "REJECT", reason, None, None, run_id,
        )
        return KernelStepResult(
            phase="VALIDATE",
            proposal=proposal,
            decision=Decision(approved=False, reason=reason),
            ledger_record=record,
            error="tier budget rejected step",
        )

    def _append_kernel_event(
        self,
        *,
        event_type: str,
        run_id: str,
        intent: str,
        bundle_id: str,
        fields: Dict[str, Any],
    ) -> None:
        payload = {
            "type": event_type,
            "run_id": run_id,
            "intent": intent,
            "bundle_id": bundle_id,
            **fields,
        }
        blob = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        )
        ev_hash = hashlib.sha256(
            blob.encode("utf-8"),
        ).hexdigest()
        state_hash = hashlib.sha256(
            f"kernel_event:{ev_hash}".encode("utf-8"),
        ).hexdigest()
        record = LedgerRecord(
            proposal_hash=ev_hash,
            simulation={},
            risk={},
            decision="REJECT",
            decision_reason=f"event:{event_type}",
            outcome_hash=None,
            state_hash=state_hash,
            metadata={
                "record_type": "kernel_event",
                "run_id": run_id,
                "event_type": event_type,
                "action": f"event:{event_type}",
                "intent": intent,
                "bundle_id": bundle_id,
                "event": payload,
                "ts": time.time(),
            },
        )
        self.ledger.append(record)

    def kernel_step(
        self,
        raw_step: Dict[str, Any],
        execute_fn: ExecuteCallback,
        context: str = "",
        intent: str = "",
        bundle_id: str = "",
        run_id: str = "",
        learner_evidence: Optional[Dict[str, Any]] = None,
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
            raw_step,
            intent=intent,
            context_hash=context,
            bundle_id=bundle_id,
        )
        rs = self.run_state.get(run_id)
        tier_cfg = self._tier_cfg(rs.tier)
        budgets = tier_cfg.get("budgets", {}) if isinstance(tier_cfg, dict) else {}

        max_total_steps = int(
            budgets.get("max_total_steps", 0) or 0,
        )
        if max_total_steps > 0 and self.state.step_count >= max_total_steps:
            return self._reject_tier_budget(
                f"tier max_total_steps exceeded:"
                f" {self.state.step_count} >= {max_total_steps}",
                proposal, run_id, intent, bundle_id, rs.tier,
            )

        if raw_step.get("type") == "apply_patch":
            patch = str(raw_step.get("patch", ""))
            max_patch_bytes = int(
                budgets.get("max_patch_bytes", 0) or 0,
            )
            patch_size = len(patch.encode("utf-8", errors="replace"))
            if max_patch_bytes > 0 and patch_size > max_patch_bytes:
                return self._reject_tier_budget(
                    f"tier max_patch_bytes exceeded: > {max_patch_bytes}",
                    proposal, run_id, intent, bundle_id, rs.tier,
                )

            max_files_touched = int(
                budgets.get("max_files_touched", 0) or 0,
            )
            touched = step_touches(raw_step)
            if max_files_touched > 0 and len(touched) > max_files_touched:
                return self._reject_tier_budget(
                    f"tier max_files_touched exceeded:"
                    f" {len(touched)} > {max_files_touched}",
                    proposal, run_id, intent, bundle_id, rs.tier,
                )

            max_lines_changed = int(
                budgets.get(
                    "max_total_lines_changed",
                    0,
                )
                or 0,
            )
            if max_lines_changed > 0:
                added = 0
                deleted = 0
                for ln in patch.splitlines():
                    if ln.startswith("+") and not ln.startswith("+++"):
                        added += 1
                    elif ln.startswith("-") and not ln.startswith("---"):
                        deleted += 1
                if (added + deleted) > max_lines_changed:
                    return self._reject_tier_budget(
                        f"tier max_total_lines_changed exceeded:"
                        f" {added + deleted} > {max_lines_changed}",
                        proposal, run_id, intent, bundle_id, rs.tier,
                    )

        # Tier gate is enforced before any simulation/execution.
        tier_ok, tier_reason = tier_allows_step(
            raw_step,
            tier_cfg,
            self.classifiers,
        )
        if not tier_ok:
            return self._reject_tier_budget(
                tier_reason or "tier_policy_reject",
                proposal, run_id, intent, bundle_id, rs.tier,
            )

        # ── 2. VALIDATE ──
        validation = validate(proposal, self.state, self.policy)
        if not validation.ok:
            record = self._commit_ledger(
                proposal,
                None,
                None,
                "REJECT",
                f"Validation failed: {validation.errors}",
                None,
                None,
                run_id,
            )
            return KernelStepResult(
                phase="VALIDATE",
                proposal=proposal,
                validation=validation,
                ledger_record=record,
                error="Validation failed",
            )

        # ── 3. SIMULATE ──
        prior_success_prob: Optional[float] = None
        prior_trials = 0
        prior_loop_risk: Optional[float] = None
        if isinstance(learner_evidence, dict):
            try:
                v = learner_evidence.get(
                    "prior_success_prob",
                )
                if v is not None:
                    prior_success_prob = float(v)
            except (TypeError, ValueError):
                prior_success_prob = None
            try:
                prior_trials = int(
                    learner_evidence.get(
                        "prior_trials",
                        0,
                    )
                    or 0,
                )
            except (TypeError, ValueError):
                prior_trials = 0

            try:
                failure_occurrence = int(
                    learner_evidence.get(
                        "failure_occurrence",
                        0,
                    )
                    or 0,
                )
            except (TypeError, ValueError):
                failure_occurrence = 0
            try:
                failure_best_win_rate = float(
                    learner_evidence.get(
                        "failure_best_win_rate",
                        0.0,
                    )
                    or 0.0,
                )
            except (TypeError, ValueError):
                failure_best_win_rate = 0.0
            if failure_occurrence >= 2:
                prior_loop_risk = min(
                    0.9,
                    0.2
                    + (
                        0.1
                        * min(
                            failure_occurrence,
                            5,
                        )
                    ),
                )
                prior_loop_risk = max(
                    prior_loop_risk or 0.0,
                    0.7,
                )

            try:
                known_traps_list = learner_evidence.get("known_traps")
                if isinstance(known_traps_list, list):
                    known_traps = [str(t) for t in known_traps_list]
                else:
                    known_traps = None
            except (TypeError, ValueError):
                known_traps = None
        else:
            known_traps = None

        sim = simulate(
            proposal,
            self.state,
            history=self.history,
            context=context,
            prior_success_prob=prior_success_prob,
            prior_trials=prior_trials,
            prior_loop_risk=prior_loop_risk,
            known_traps=known_traps,
        )

        # ── 4. RISK SCORE ──
        risk = risk_score(
            proposal,
            sim,
            self.state,
            policy=self.policy,
        )

        # ── 5. DECIDE ──
        decision = decide(proposal, sim, risk, self.policy)

        if not decision.approved:
            # Record rejection + update state.
            self.state.record_action(proposal.action)
            record = self._commit_ledger(
                proposal,
                sim,
                risk,
                "REJECT",
                decision.reason,
                None,
                None,
                run_id,
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
            self.consecutive_failures = 0  # Reset on success
        else:
            self.state.record_failure()
            self.consecutive_failures += 1

        # Check for failure escalation
        escalation_triggered = self._check_failure_escalation(run_id)
        if escalation_triggered:
            outcome.error = (
                (outcome.error or "")
                + "\n[KERNEL]: 3 consecutive failures — escalating strategy."
            )

        # Record in outcome history for future simulation.
        self.history.record(
            proposal.action,
            context,
            outcome.success,
            sim.cost_est,
        )

        executor_out_like = {
            "status": outcome.exit_code,
            "logs": outcome.logs or "",
            "payload": outcome.payload or "",
        }
        kinds = []
        if outcome.payload:
            try:
                parsed_payload = json.loads(
                    outcome.payload,
                )
                if isinstance(parsed_payload, dict):
                    fk = parsed_payload.get("failure_kind")
                    if isinstance(fk, str) and fk:
                        kinds = [fk]
            except Exception:
                kinds = []
        if not kinds:
            kinds = extract_failure_kinds(
                executor_out_like,
            )
        rs.failure_kinds = kinds
        td = pick_next_tier(
            rs.tier,
            kinds,
            self.tier_policy,
        )
        if td.tier != rs.tier:
            old_tier = rs.tier
            rs.tier = td.tier
            self._append_kernel_event(
                event_type="TIER_ESCALATED",
                run_id=run_id,
                intent=intent,
                bundle_id=bundle_id,
                fields={
                    "from": old_tier,
                    "to": td.tier,
                    "reason": td.reason,
                    "failure_kinds": kinds,
                },
            )

        # ── 7. VERIFY ──
        verification = verify(
            proposal,
            outcome,
            self.state,
            self.policy,
        )

        # Adaptive risk tightening: if verification
        # flags failure cluster, escalate safety level.
        if not verification.ok:
            for v in verification.violations:
                if v.get("code") == "FAILURE_CLUSTER":
                    self._adaptive_tighten()

        # ── 8. LEDGER COMMIT ──
        record = self._commit_ledger(
            proposal,
            sim,
            risk,
            "APPROVE",
            decision.reason,
            outcome,
            verification,
            run_id,
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
        run_id: str = "",
    ) -> LedgerRecord:
        """Write an immutable record to the ledger."""
        record = LedgerRecord(
            proposal_hash=proposal.deterministic_hash(),
            simulation=sim.to_dict() if sim else {},
            risk=risk.to_dict() if risk else {},
            decision=decision,
            decision_reason=reason,
            outcome_hash=(outcome.deterministic_hash() if outcome else None),
            state_hash=self.state.deterministic_hash(),
            verification=(verification.to_dict() if verification else None),
            metadata={
                "action": proposal.action,
                "intent": proposal.intent,
                "run_id": run_id,
                "bundle_id": proposal.bundle_id,
                "tier": self.run_state.get(run_id).tier,
                "context_hash": proposal.context_hash,
                "memory_version": self.state.memory_version,
                "env_hash": self.state.env_hash,
                "step_count": self.state.step_count,
            },
        )
        return self.ledger.append(record)

    def reset_for_run(
        self,
        *,
        run_id: str = "",
        rng_seed: Optional[int] = None,
        env_hash: str = "",
        memory_version: str = "0",
        policy_hash: Optional[str] = None,
        reset_history: bool = True,
    ) -> None:
        """Reset deterministic state for a new run.

        This prevents cross-run state leakage while keeping
        the immutable ledger intact.
        """
        seed = int(
            rng_seed if rng_seed is not None else self.policy.get("rng_seed", 42)
        )
        p_hash = str(
            policy_hash
            if policy_hash is not None
            else self.policy.get("policy_hash", "")
        )
        self.state = SystemState(
            memory_version=str(memory_version),
            env_hash=str(env_hash),
            rng_seed=seed,
            policy_hash=p_hash,
            resource_state={
                "run_id": run_id,
            },
        )
        if reset_history:
            self.history = OutcomeHistory(
                max_entries=int(
                    self.policy.get("history_max", 500),
                ),
            )
        rs = self.run_state.get(run_id)
        rs.tier = 0
        rs.failure_kinds = []

    def _adaptive_tighten(self) -> None:
        """Tighten safety envelope after failure cluster.

        This is the learner-driven adaptive risk mechanism.
        """
        self.state.safety_level = min(
            2,
            self.state.safety_level + 1,
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

    def end_run(self, run_id: str) -> None:
        """Clear per-run tier state."""
        self.run_state.clear(run_id)

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
            "tier_policy_path": self.tier_policy_path,
            "active_runs": self.run_state.snapshot(),
        }
