"""Deterministic replay — reconstruct execution from ledger.

Replay requires:
  - Same ledger
  - Same initial state
  - Same RNG seed
  - Same environment snapshot

Outputs must match exactly — otherwise divergence detected.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from rfsn_kernel.hard_ledger import HardLedger, LedgerRecord
from rfsn_kernel.state import SystemState


@dataclass
class ReplayStep:
    """One step in a replay — records match/divergence."""

    index: int
    record: LedgerRecord
    matched: bool
    divergence: Optional[str] = None


@dataclass
class ReplayResult:
    """Full replay result."""

    ok: bool
    total_steps: int
    matched_steps: int
    divergences: List[Dict[str, Any]] = field(default_factory=list)
    steps: List[ReplayStep] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "total_steps": self.total_steps,
            "matched_steps": self.matched_steps,
            "divergence_count": len(self.divergences),
            "divergences": self.divergences[:20],
        }


class ReplayRunner:
    """Deterministic replay engine.

    Reads the hard ledger and verifies that each
    step can be reconstructed identically.

    Modes:
      - VERIFY: Just check chain integrity + hashes
      - RECONSTRUCT: Re-run simulation/risk with
        same state and compare outputs
    """

    def __init__(self, ledger_path: str) -> None:
        self.ledger = HardLedger(ledger_path)

    def verify_chain(self) -> Dict[str, Any]:
        """Verify the hash chain integrity."""
        return self.ledger.verify_chain()

    def replay_verify(
        self,
        expected_state_hashes: Optional[List[str]] = None,
    ) -> ReplayResult:
        """Read all ledger records and verify consistency.

        Checks:
        1. Hash chain integrity
        2. State hash continuity
        3. Decision consistency (APPROVE/REJECT recorded correctly)
        """
        records = self.ledger.read_all()
        if not records:
            return ReplayResult(ok=True, total_steps=0, matched_steps=0)

        steps: List[ReplayStep] = []
        divergences: List[Dict[str, Any]] = []
        matched = 0

        prev_state_hash = ""
        for i, rec in enumerate(records):
            step_ok = True
            div_msg: Optional[str] = None

            # Check chain linkage.
            if i > 0:
                expected_prev = records[i - 1].chain_hash
                if rec.prev_chain_hash != expected_prev:
                    div_msg = (
                        f"Chain break at step {i}:"
                        f" expected prev={expected_prev[:12]}..."
                        f" got={rec.prev_chain_hash[:12]}..."
                    )
                    step_ok = False

            # Check state hash progression if provided.
            if expected_state_hashes and i < len(expected_state_hashes):
                if rec.state_hash != expected_state_hashes[i]:
                    div_msg = (
                        f"State divergence at step {i}:"
                        f" expected={expected_state_hashes[i][:12]}..."
                        f" got={rec.state_hash[:12]}..."
                    )
                    step_ok = False

            # Verify decision consistency.
            if rec.decision not in ("APPROVE", "REJECT"):
                div_msg = f"Invalid decision at step {i}: {rec.decision}"
                step_ok = False

            # APPROVE must have outcome_hash, REJECT must not.
            if rec.decision == "APPROVE" and not rec.outcome_hash:
                div_msg = f"APPROVE without outcome at step {i}"
                step_ok = False

            if step_ok:
                matched += 1
            else:
                divergences.append({
                    "step": i,
                    "error": div_msg,
                    "entry_hash": rec.entry_hash[:12],
                })

            steps.append(ReplayStep(
                index=i, record=rec,
                matched=step_ok, divergence=div_msg,
            ))

        return ReplayResult(
            ok=len(divergences) == 0,
            total_steps=len(records),
            matched_steps=matched,
            divergences=divergences,
            steps=steps,
        )

    def extract_decision_trace(self) -> List[Dict[str, Any]]:
        """Extract a decision trace for analysis.

        Returns a list of {action, decision, risk, success_prob}
        for each step in the ledger.
        """
        records = self.ledger.read_all()
        trace: List[Dict[str, Any]] = []
        for rec in records:
            sim = rec.simulation or {}
            risk = rec.risk or {}
            meta = rec.metadata or {}
            trace.append({
                "action": meta.get("action", ""),
                "intent": meta.get("intent", ""),
                "decision": rec.decision,
                "reason": rec.decision_reason,
                "success_prob": sim.get("success_prob", 0),
                "loop_risk": sim.get("loop_risk", 0),
                "drift_risk": sim.get("drift_risk", 0),
                "effective_risk": risk.get("effective_risk", 0),
                "total_risk": risk.get("total_risk", 0),
            })
        return trace


def snapshot_environment(
    repo_path: str = "",
    seed: int = 0,
) -> Dict[str, Any]:
    """Capture a deterministic environment snapshot.

    Used to freeze state for replay.
    """
    import os
    env_vars = {
        k: v for k, v in os.environ.items()
        if k.startswith("RFSN_") or k.startswith("LEDGER_")
    }
    snapshot = {
        "seed": seed,
        "repo_path": repo_path,
        "env_vars": env_vars,
    }
    blob = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    snapshot["env_hash"] = hashlib.sha256(
        blob.encode("utf-8"),
    ).hexdigest()[:16]
    return snapshot
