"""Deterministic state snapshots and proposal contracts.

Every planner output normalizes into a Proposal.
System state must be serializable for replay.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Optional


@dataclass
class Proposal:
    """Normalized planner output — strict contract.

    No free-form execution allowed.
    """

    action: str                   # step type (repo_search, apply_patch, etc.)
    params: Dict[str, Any]        # step parameters
    context_hash: str = ""        # hash of context at proposal time
    planner_hash: str = ""        # hash of planner output
    timestamp: float = 0.0        # epoch seconds
    intent: str = ""              # one-line intent from planner
    bundle_id: str = ""           # bundle correlation ID

    def __post_init__(self) -> None:
        if not self.timestamp:
            fixed = os.getenv("LEDGER_FIXED_TS")
            self.timestamp = (
                float(fixed) if fixed else time.time()
            )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def deterministic_hash(self) -> str:
        """Produce a reproducible hash of this proposal."""
        blob = json.dumps(
            {"action": self.action, "params": self.params,
             "context_hash": self.context_hash,
             "intent": self.intent},
            sort_keys=True, separators=(",", ":"),
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass
class SystemState:
    """Deterministic snapshot of the entire system.

    Must be serializable for replay.
    """

    memory_version: str = "0"
    env_hash: str = ""
    rng_seed: int = 0
    safety_level: int = 0          # 0=normal, 1=cautious, 2=locked
    resource_state: Dict[str, Any] = field(default_factory=dict)
    step_count: int = 0
    iter_count: int = 0
    total_cost: float = 0.0
    recent_failures: int = 0       # rolling window
    recent_actions: list = field(default_factory=list)   # last N actions for loop detection
    drift_variance: float = 0.0    # state change variance
    policy_hash: str = ""          # hash of loaded policies

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def deterministic_hash(self) -> str:
        """Produce a reproducible hash of this state."""
        blob = json.dumps(
            self.to_dict(), sort_keys=True,
            separators=(",", ":"), default=str,
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def record_action(self, action: str, max_history: int = 20) -> None:
        """Record an action for loop detection."""
        self.recent_actions.append(action)
        if len(self.recent_actions) > max_history:
            self.recent_actions = self.recent_actions[-max_history:]

    def record_failure(self) -> None:
        self.recent_failures += 1

    def record_success(self) -> None:
        self.recent_failures = max(0, self.recent_failures - 1)

    def advance_step(self) -> None:
        self.step_count += 1

    def snapshot(self) -> Dict[str, Any]:
        """Create a frozen snapshot for ledger."""
        return {
            "memory_version": self.memory_version,
            "env_hash": self.env_hash,
            "rng_seed": self.rng_seed,
            "safety_level": self.safety_level,
            "step_count": self.step_count,
            "iter_count": self.iter_count,
            "recent_failures": self.recent_failures,
            "drift_variance": round(self.drift_variance, 6),
            "state_hash": self.deterministic_hash(),
        }


@dataclass
class Outcome:
    """Result of a kernel-controlled execution."""

    success: bool
    exit_code: int = 0
    payload: str = ""
    logs: str = ""
    duration_sec: float = 0.0
    error: Optional[str] = None
    state_delta_hash: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def deterministic_hash(self) -> str:
        blob = json.dumps(
            {"success": self.success, "exit_code": self.exit_code,
             "state_delta_hash": self.state_delta_hash},
            sort_keys=True, separators=(",", ":"),
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()
