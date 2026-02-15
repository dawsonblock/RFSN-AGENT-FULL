"""SelfHealingCore — The Immune System.

Monitors system health by ingesting run outcomes, tracking stability,
and dynamically adjusting the hardening level.

Hardening Levels:
    FAST      — Optimistic. Skip extra verification. Max throughput.
    BALANCED  — Default. Standard verification pipeline.
    HARDENED  — Pessimistic. Extra diff scans, coverage checks, slower execution.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .signals import Signal, extract_signals
from .memory import FailureMemory, FailureCluster


class HardeningLevel(str, Enum):
    FAST = "fast"
    BALANCED = "balanced"
    HARDENED = "hardened"


@dataclass
class HealthSnapshot:
    """Point-in-time system health reading."""

    timestamp: float
    stability_score: float  # 0.0 (critical) → 1.0 (healthy)
    hardening_level: HardeningLevel
    recent_success_rate: float
    chronic_failure_count: int
    total_runs: int


class SelfHealingCore:
    """Central stability monitor and adaptive hardener.

    Usage:
        core = SelfHealingCore()
        signal = core.ingest(logs="...", status=1)
        level = core.hardening_level  # "fast" / "balanced" / "hardened"
    """

    def __init__(
        self,
        window_size: int = 20,
        harden_threshold: float = 0.4,
        relax_threshold: float = 0.8,
    ) -> None:
        self._memory = FailureMemory()
        self._window_size = window_size
        self._harden_threshold = harden_threshold
        self._relax_threshold = relax_threshold

        # Sliding window of recent outcomes (True=success, False=failure)
        self._outcomes: list[bool] = []
        self._total_runs: int = 0
        self._hardening: HardeningLevel = HardeningLevel.BALANCED

    # ── Public API ────────────────────────────────────────────────────

    def ingest(self, logs: str, status: int) -> Signal:
        """Ingest a run result and update system health.

        Args:
            logs: Raw stdout/stderr from the execution.
            status: Exit code (0 = success).

        Returns:
            The extracted Signal for caller inspection.
        """
        signal = extract_signals(logs, status)
        success = status == 0

        # Record in sliding window
        self._outcomes.append(success)
        if len(self._outcomes) > self._window_size:
            self._outcomes = self._outcomes[-self._window_size :]
        self._total_runs += 1

        # Record failure in memory
        if not success:
            cluster = self._memory.record(
                fingerprint=signal.stack_fingerprint,
                failure_type=signal.failure_type,
                detail=signal.detail,
            )
            # If chronic, escalate hardening
            if cluster.is_chronic:
                self._hardening = HardeningLevel.HARDENED

        # Adjust hardening based on stability
        self._adjust_hardening()

        return signal

    @property
    def hardening_level(self) -> HardeningLevel:
        """Current hardening level."""
        return self._hardening

    @property
    def stability_score(self) -> float:
        """Current stability as a 0.0–1.0 score."""
        if not self._outcomes:
            return 1.0  # No data = assume healthy
        return sum(1 for o in self._outcomes if o) / len(self._outcomes)

    @property
    def memory(self) -> FailureMemory:
        """Access the failure memory for inspection."""
        return self._memory

    def snapshot(self) -> HealthSnapshot:
        """Capture a point-in-time health reading."""
        return HealthSnapshot(
            timestamp=time.time(),
            stability_score=self.stability_score,
            hardening_level=self._hardening,
            recent_success_rate=self.stability_score,
            chronic_failure_count=len(self._memory.get_chronic_failures()),
            total_runs=self._total_runs,
        )

    def reset(self) -> None:
        """Reset all state (for testing or fresh start)."""
        self._outcomes.clear()
        self._total_runs = 0
        self._hardening = HardeningLevel.BALANCED
        self._memory.clear()

    # ── Internal ──────────────────────────────────────────────────────

    def _adjust_hardening(self) -> None:
        """Adjust hardening level based on stability score."""
        score = self.stability_score

        if score <= self._harden_threshold:
            self._hardening = HardeningLevel.HARDENED
        elif score >= self._relax_threshold:
            self._hardening = HardeningLevel.FAST
        else:
            self._hardening = HardeningLevel.BALANCED

    def __repr__(self) -> str:
        return (
            f"SelfHealingCore("
            f"stability={self.stability_score:.2f}, "
            f"level={self._hardening.value}, "
            f"runs={self._total_runs})"
        )
