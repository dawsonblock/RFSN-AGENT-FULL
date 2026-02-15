"""Failure Memory — Long-term storage of failure patterns and root causes.

Tracks recurring error fingerprints, clusters them into FailureClusters,
and maps clusters to likely RootCauses for faster future diagnosis.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RootCause:
    """A diagnosed root cause for a failure cluster."""

    cause_id: str  # e.g. "missing_dep", "bad_patch", "env_drift"
    description: str  # Human-readable explanation
    suggested_action: str  # e.g. "reinstall_deps", "rollback_patch"
    confidence: float = 0.5  # 0.0 → 1.0


@dataclass
class FailureCluster:
    """A group of failures sharing the same fingerprint."""

    fingerprint: str
    failure_type: str
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    count: int = 1
    details: list[str] = field(default_factory=list)
    root_cause: Optional[RootCause] = None

    def record_occurrence(self, detail: str = "") -> None:
        """Record another occurrence of this failure."""
        self.last_seen = time.time()
        self.count += 1
        if detail and detail not in self.details:
            self.details.append(detail)
            # Keep detail list bounded
            if len(self.details) > 10:
                self.details = self.details[-10:]

    @property
    def recurrence_rate(self) -> float:
        """How many times per hour this failure recurs."""
        elapsed = max(self.last_seen - self.first_seen, 1.0)
        return self.count / (elapsed / 3600.0)

    @property
    def is_chronic(self) -> bool:
        """True if this failure keeps happening (>3 times, >2/hr)."""
        return self.count >= 3 and self.recurrence_rate > 2.0


# ── Root Cause Templates ──────────────────────────────────────────────
_ROOT_CAUSE_TEMPLATES: dict[str, RootCause] = {
    "import_error": RootCause(
        cause_id="missing_dep",
        description="A required Python package is not installed.",
        suggested_action="reinstall_deps",
        confidence=0.8,
    ),
    "syntax_error": RootCause(
        cause_id="bad_patch",
        description="A patch introduced invalid syntax.",
        suggested_action="rollback_patch",
        confidence=0.9,
    ),
    "timeout": RootCause(
        cause_id="infinite_loop",
        description="Execution exceeded time limit, likely infinite loop.",
        suggested_action="rollback_patch",
        confidence=0.7,
    ),
    "recursion_error": RootCause(
        cause_id="infinite_recursion",
        description="Unbounded recursion detected.",
        suggested_action="rollback_patch",
        confidence=0.85,
    ),
    "oom": RootCause(
        cause_id="memory_exhaustion",
        description="Process consumed all available memory.",
        suggested_action="increase_mem_limit",
        confidence=0.7,
    ),
    "permission_denied": RootCause(
        cause_id="sandbox_escape_attempt",
        description="Code tried to access a restricted resource.",
        suggested_action="harden_capsule",
        confidence=0.6,
    ),
    "patch_apply_failed": RootCause(
        cause_id="stale_patch",
        description="Patch does not match current file state.",
        suggested_action="regenerate_patch",
        confidence=0.8,
    ),
}


class FailureMemory:
    """In-memory store of failure clusters.

    Thread-safe for single-writer scenarios (the kernel loop).
    """

    def __init__(self, max_clusters: int = 256) -> None:
        self._clusters: dict[str, FailureCluster] = {}
        self._max_clusters = max_clusters

    @property
    def clusters(self) -> dict[str, FailureCluster]:
        return dict(self._clusters)

    def record(
        self,
        fingerprint: str,
        failure_type: str,
        detail: str = "",
    ) -> FailureCluster:
        """Record a failure occurrence. Returns the updated cluster."""
        if fingerprint in self._clusters:
            cluster = self._clusters[fingerprint]
            cluster.record_occurrence(detail)
        else:
            # Evict oldest if at capacity
            if len(self._clusters) >= self._max_clusters:
                oldest_key = min(
                    self._clusters,
                    key=lambda k: self._clusters[k].last_seen,
                )
                del self._clusters[oldest_key]

            cluster = FailureCluster(
                fingerprint=fingerprint,
                failure_type=failure_type,
                details=[detail] if detail else [],
            )
            self._clusters[fingerprint] = cluster

        # Auto-diagnose root cause from templates
        if cluster.root_cause is None and failure_type in _ROOT_CAUSE_TEMPLATES:
            cluster.root_cause = _ROOT_CAUSE_TEMPLATES[failure_type]

        return cluster

    def get_chronic_failures(self) -> list[FailureCluster]:
        """Return all clusters that are recurring chronically."""
        return [c for c in self._clusters.values() if c.is_chronic]

    def get_cluster(self, fingerprint: str) -> Optional[FailureCluster]:
        """Retrieve a specific cluster by fingerprint."""
        return self._clusters.get(fingerprint)

    def clear(self) -> None:
        """Reset all memory."""
        self._clusters.clear()

    def summary(self) -> dict:
        """Return a summary dict for logging/inspection."""
        chronic = self.get_chronic_failures()
        return {
            "total_clusters": len(self._clusters),
            "chronic_count": len(chronic),
            "top_failures": [
                {
                    "fingerprint": c.fingerprint[:8],
                    "type": c.failure_type,
                    "count": c.count,
                    "root_cause": c.root_cause.cause_id if c.root_cause else None,
                }
                for c in sorted(
                    self._clusters.values(),
                    key=lambda x: x.count,
                    reverse=True,
                )[:5]
            ],
        }
