"""Memory Immune System (CIS-style) — selective memory governance.

Memory must become selective, not append-heavy.

Admission pipeline:
  New Memory → Quality Score → Risk Scan
  → Contradiction Check → Admit / Quarantine / Reject

Governance rules:
  - Promote only repeated-success memory
  - Decay stale entries
  - Protect core axioms
  - Detect memory poisoning
  - Maintain provenance chain
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from enum import Enum


class MemoryDecision(str, Enum):
    ADMIT = "ADMIT"
    QUARANTINE = "QUARANTINE"
    REJECT = "REJECT"


@dataclass
class MemoryEntry:
    """One memory entry with provenance."""

    content: str
    source: str  # where this came from
    entry_type: str  # "action_outcome", "strategy_result", "context"
    provenance_hash: str = ""  # hash chain to origin
    quality_score: float = 0.0
    risk_score: float = 0.0
    contradiction_score: float = 0.0
    access_count: int = 0
    success_count: int = 0  # how often this led to success
    failure_count: int = 0  # how often this led to failure
    created_at: float = 0.0
    last_accessed: float = 0.0
    version: int = 0
    status: str = "active"  # active, quarantined, decayed, core
    _word_cache: set = field(default_factory=set, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.created_at:
            fixed = __import__("os").getenv("LEDGER_FIXED_TS")
            self.created_at = float(fixed) if fixed else time.time()
        if not self.provenance_hash:
            blob = f"{self.content}|{self.source}|{self.created_at}"
            self.provenance_hash = hashlib.sha256(
                blob.encode("utf-8"),
            ).hexdigest()[:16]

    @property
    def word_set(self) -> set:
        """Lazily cached word set for conflict/relevance checks."""
        if not self._word_cache:
            self._word_cache = set(self.content.lower().split())
        return self._word_cache

    def to_dict(self) -> Dict[str, Any]:
        return {
            "content": self.content[:200],
            "source": self.source,
            "type": self.entry_type,
            "provenance": self.provenance_hash,
            "quality": round(self.quality_score, 4),
            "risk": round(self.risk_score, 4),
            "contradiction": round(self.contradiction_score, 4),
            "status": self.status,
            "access_count": self.access_count,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "version": self.version,
        }


@dataclass
class AdmissionResult:
    """Result of memory admission pipeline."""

    decision: MemoryDecision
    reason: str
    quality_score: float
    risk_score: float
    contradiction_score: float


class MemoryImmuneSystem:
    """Selective memory governance.

    Controls what enters long-term memory,
    detects poisoning, resolves contradictions,
    and decays stale entries.
    """

    def __init__(
        self,
        quality_min: float = 0.3,
        risk_max: float = 0.7,
        contradiction_max: float = 0.6,
        decay_rate: float = 0.01,
        max_entries: int = 2000,
    ) -> None:
        self.quality_min = quality_min
        self.risk_max = risk_max
        self.contradiction_max = contradiction_max
        self.decay_rate = decay_rate
        self.max_entries = max_entries

        self._store: Dict[str, MemoryEntry] = {}
        self._core_axioms: Dict[str, MemoryEntry] = {}
        self._quarantine: Dict[str, MemoryEntry] = {}
        self._version = 0
        self._unsaved: list[MemoryEntry] = []  # entries not yet flushed
        self._needs_full_save = False  # set on decay/eviction

    @property
    def memory_version(self) -> str:
        return str(self._version)

    @property
    def active_count(self) -> int:
        return len(self._store)

    @property
    def quarantine_count(self) -> int:
        return len(self._quarantine)

    # ── Admission Pipeline ──

    def admit(self, entry: MemoryEntry) -> AdmissionResult:
        """Run the full admission pipeline.

        Score → Risk → Contradiction → Decide
        """
        q = self._quality_score(entry)
        r = self._risk_score(entry)
        c = self._contradiction_score(entry)

        entry.quality_score = q
        entry.risk_score = r
        entry.contradiction_score = c

        # 1. Risk gate.
        if r > self.risk_max:
            return AdmissionResult(
                decision=MemoryDecision.REJECT,
                reason=f"Risk too high: {r:.2f} > {self.risk_max}",
                quality_score=q,
                risk_score=r,
                contradiction_score=c,
            )

        # 2. Contradiction gate.
        if c > self.contradiction_max:
            self._quarantine[entry.provenance_hash] = entry
            entry.status = "quarantined"
            self._version += 1
            return AdmissionResult(
                decision=MemoryDecision.QUARANTINE,
                reason=f"Contradiction detected: {c:.2f} > {self.contradiction_max}",
                quality_score=q,
                risk_score=r,
                contradiction_score=c,
            )

        # 3. Quality gate.
        if q < self.quality_min:
            return AdmissionResult(
                decision=MemoryDecision.REJECT,
                reason=f"Quality too low: {q:.2f} < {self.quality_min}",
                quality_score=q,
                risk_score=r,
                contradiction_score=c,
            )

        # 4. Capacity gate.
        if len(self._store) >= self.max_entries:
            self._evict_weakest()

        # Admit.
        entry.status = "active"
        entry.version = self._version
        self._store[entry.provenance_hash] = entry
        self._unsaved.append(entry)
        self._version += 1

        return AdmissionResult(
            decision=MemoryDecision.ADMIT,
            reason="admitted",
            quality_score=q,
            risk_score=r,
            contradiction_score=c,
        )

    # ── Scoring Functions ──

    def _quality_score(self, entry: MemoryEntry) -> float:
        """Score memory quality [0, 1].

        Higher for entries with:
        - Clear source
        - Action outcome data
        - Historical success
        """
        score = 0.3  # base

        # Source reliability.
        if entry.source in ("kernel", "learner", "test_result"):
            score += 0.3
        elif entry.source in ("planner", "llm"):
            score += 0.1

        # Success history.
        total = entry.success_count + entry.failure_count
        if total > 0:
            win_rate = entry.success_count / total
            score += 0.3 * win_rate

        # Content length (too short = low info, too long = noise).
        clen = len(entry.content)
        if 10 < clen < 5000:
            score += 0.1

        return min(1.0, score)

    def _risk_score(self, entry: MemoryEntry) -> float:
        """Detect potentially poisonous memory [0, 1].

        High risk for:
        - Conflicting with core axioms
        - Unusual patterns
        - High failure association
        """
        risk = 0.0

        # High failure rate from this source.
        total = entry.success_count + entry.failure_count
        if total >= 3:
            fail_rate = entry.failure_count / total
            risk += fail_rate * 0.5

        # Conflicts with core axioms.
        for axiom in self._core_axioms.values():
            if self._content_conflicts(entry.content, axiom.content):
                risk += 0.4
                break

        return min(1.0, risk)

    def _contradiction_score(self, entry: MemoryEntry) -> float:
        """Check for contradictions with existing memory [0, 1]."""
        if not self._store:
            return 0.0

        conflicts = 0
        checked = 0
        for existing in list(self._store.values())[-50:]:
            if existing.entry_type == entry.entry_type:
                checked += 1
                if self._content_conflicts(entry.content, existing.content):
                    conflicts += 1

        if checked == 0:
            return 0.0
        return conflicts / checked

    def _content_conflicts(self, a: str, b: str) -> bool:
        """Simple conflict detection between two content strings.

        Looks for direct negation patterns.
        """
        a_lower = a.lower()
        b_lower = b.lower()

        # Same topic, opposite outcome.
        negation_pairs = [
            ("success", "failure"),
            ("passed", "failed"),
            ("fixed", "broken"),
            ("works", "broken"),
            ("correct", "incorrect"),
        ]
        for pos, neg in negation_pairs:
            if (pos in a_lower and neg in b_lower) or (
                neg in a_lower and pos in b_lower
            ):
                # Use cached word sets for overlap check.
                a_words = set(a_lower.split())
                b_words = set(b_lower.split())
                if len(a_words & b_words) >= 3:
                    return True
        return False

    # ── Memory Management ──

    def protect_axiom(self, entry: MemoryEntry) -> None:
        """Mark an entry as a core axiom (never decayed)."""
        entry.status = "core"
        self._core_axioms[entry.provenance_hash] = entry
        self._store[entry.provenance_hash] = entry

    def decay(self) -> int:
        """Decay stale entries. Returns count decayed."""
        now = time.time()
        decayed = 0
        to_remove: List[str] = []

        for key, entry in self._store.items():
            if entry.status == "core":
                continue
            age = now - entry.created_at
            idle = now - (entry.last_accessed or entry.created_at)

            # Decay score based on age and idle time.
            decay_factor = self.decay_rate * (age / 3600) * (idle / 3600)

            if decay_factor > 1.0 and entry.access_count < 3:
                entry.status = "decayed"
                to_remove.append(key)
                decayed += 1
            elif decay_factor > 2.0:
                entry.status = "decayed"
                to_remove.append(key)
                decayed += 1

        for key in to_remove:
            del self._store[key]

        if decayed:
            self._version += 1
            self._needs_full_save = True
        return decayed

    def _evict_weakest(self) -> None:
        """Remove the weakest non-core entry."""
        if not self._store:
            return

        weakest_key = None
        weakest_score = float("inf")

        for key, entry in self._store.items():
            if entry.status == "core":
                continue
            score = entry.quality_score - entry.risk_score
            if score < weakest_score:
                weakest_score = score
                weakest_key = key
                if score < 0:
                    break  # early-out: can't beat negative

        if weakest_key:
            del self._store[weakest_key]
            self._needs_full_save = True

    def record_outcome(
        self,
        provenance_hash: str,
        success: bool,
    ) -> None:
        """Update an entry's success/failure counts with ELO-style scoring.

        Quality score adapts based on actual vs expected outcome:
          new_q = old_q + K * (actual - expected)
        K decays as observation count grows (less volatile over time).
        """
        entry = self._store.get(provenance_hash)
        if entry:
            if success:
                entry.success_count += 1
            else:
                entry.failure_count += 1
            entry.last_accessed = time.time()
            entry.access_count += 1

            # ELO-style adaptive quality adjustment.
            total = entry.success_count + entry.failure_count
            k_factor = max(0.05, 0.4 / (1 + total * 0.1))  # K decays
            expected = entry.quality_score
            actual = 1.0 if success else 0.0
            entry.quality_score = max(
                0.01,
                min(1.0, entry.quality_score + k_factor * (actual - expected)),
            )

    def promote_quarantined(self, provenance_hash: str) -> bool:
        """Promote a quarantined entry to active."""
        entry = self._quarantine.pop(provenance_hash, None)
        if entry:
            entry.status = "active"
            self._store[provenance_hash] = entry
            self._version += 1
            return True
        return False

    def get_stats(self) -> Dict[str, Any]:
        """Return memory system statistics."""
        return {
            "version": self._version,
            "active": len(self._store),
            "quarantined": len(self._quarantine),
            "core_axioms": len(self._core_axioms),
            "max_entries": self.max_entries,
        }

    def lookup(self, entry_type: str, limit: int = 10) -> List[MemoryEntry]:
        """Retrieve active entries by type."""
        results = [
            e
            for e in self._store.values()
            if e.entry_type == entry_type and e.status == "active"
        ]
        # Sort by quality descending.
        results.sort(key=lambda e: e.quality_score, reverse=True)
        return results[:limit]

    # ── Relevance-Based Retrieval ──

    def retrieve_relevant(
        self,
        query: str,
        entry_type: str = "",
        limit: int = 5,
    ) -> List[MemoryEntry]:
        """Retrieve entries ranked by relevance to a query.

        Score = keyword_overlap * 0.4 + recency * 0.3 + quality * 0.3
        """
        candidates = [
            e
            for e in self._store.values()
            if e.status in ("active", "core")
            and (not entry_type or e.entry_type == entry_type)
        ]
        if not candidates:
            return []

        query_words = set(query.lower().split())
        now = time.time()
        scored: List[tuple] = []

        for entry in candidates:
            # Use cached word set.
            overlap = len(query_words & entry.word_set)
            max_possible = max(len(query_words), 1)
            keyword_score = min(1.0, overlap / max_possible)

            # Recency (decay over 24 hours).
            age_hours = (now - entry.created_at) / 3600.0
            recency_score = max(0.0, 1.0 - (age_hours / 24.0))

            # Quality.
            quality = entry.quality_score

            relevance = keyword_score * 0.4 + recency_score * 0.3 + quality * 0.3
            scored.append((relevance, entry))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [entry for _, entry in scored[:limit]]

    # ── Persistence (JSONL) ──

    def save(self, path: str) -> int:
        """Persist memory state to a JSONL file. Returns count saved."""
        import os

        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        count = 0
        with open(path, "w", encoding="utf-8") as f:
            for entry in self._store.values():
                f.write(
                    json.dumps(self._entry_to_record(entry), ensure_ascii=False) + "\n"
                )
                count += 1
        self._unsaved.clear()
        self._needs_full_save = False
        return count

    def append_save(self, path: str) -> int:
        """Append only unsaved entries. Falls back to full save if needed."""
        import os

        if self._needs_full_save:
            return self.save(path)
        if not self._unsaved:
            return 0
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        count = 0
        with open(path, "a", encoding="utf-8") as f:
            for entry in self._unsaved:
                f.write(
                    json.dumps(self._entry_to_record(entry), ensure_ascii=False) + "\n"
                )
                count += 1
        self._unsaved.clear()
        return count

    @staticmethod
    def _entry_to_record(entry: MemoryEntry) -> Dict[str, Any]:
        return {
            "content": entry.content,
            "source": entry.source,
            "entry_type": entry.entry_type,
            "provenance_hash": entry.provenance_hash,
            "quality_score": entry.quality_score,
            "risk_score": entry.risk_score,
            "contradiction_score": entry.contradiction_score,
            "access_count": entry.access_count,
            "success_count": entry.success_count,
            "failure_count": entry.failure_count,
            "created_at": entry.created_at,
            "last_accessed": entry.last_accessed,
            "version": entry.version,
            "status": entry.status,
        }

    def load(self, path: str) -> int:
        """Load memory state from a JSONL file. Returns count of active entries loaded.

        Behaviour by entry status:
        * ``active``     — inserted into ``_store``; counted.
        * ``core``       — inserted into both ``_core_axioms`` and ``_store``; counted.
        * ``quarantined``— inserted into ``_quarantine`` only; not counted as active.
        * corrupt/missing required fields — skipped with no crash.
        """
        import os

        if not os.path.isfile(path):
            return 0
        count = 0
        corrupt = 0
        try:
            with open(path, "r", encoding="utf-8") as f:
                for raw_line in f:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        entry = MemoryEntry(
                            content=d["content"],
                            source=d["source"],
                            entry_type=d["entry_type"],
                            provenance_hash=d.get("provenance_hash", ""),
                            quality_score=float(d.get("quality_score", 0.3)),
                            risk_score=float(d.get("risk_score", 0.0)),
                            contradiction_score=float(
                                d.get("contradiction_score", 0.0)
                            ),
                            access_count=int(d.get("access_count", 0)),
                            success_count=int(d.get("success_count", 0)),
                            failure_count=int(d.get("failure_count", 0)),
                            created_at=float(d.get("created_at", 0.0)),
                            last_accessed=float(d.get("last_accessed", 0.0)),
                            version=int(d.get("version", 0)),
                            status=d.get("status", "active"),
                        )
                        key = entry.provenance_hash
                        if entry.status == "core":
                            # Core axioms live in both stores (mirrors protect_axiom).
                            self._core_axioms[key] = entry
                            self._store[key] = entry
                            count += 1
                        elif entry.status == "quarantined":
                            # Quarantined entries do not enter active _store.
                            self._quarantine[key] = entry
                        else:
                            # Treat "active" entries as active; preserve other
                            # statuses (e.g., "decayed") without overwriting them.
                            if entry.status != "active":
                                # Log decayed entries but still reload them as active
                                # since they were explicitly saved — pruning is the
                                # caller's responsibility via decay().
                                pass
                            self._store[key] = entry
                            count += 1
                    except (json.JSONDecodeError, TypeError, KeyError, ValueError):
                        corrupt += 1
                        continue
        except Exception:
            pass  # file inaccessible; caller receives partial count
        if corrupt:
            import logging
            logging.getLogger(__name__).warning(
                "MemoryImmuneSystem.load: skipped %d corrupt record(s) in %s",
                corrupt,
                path,
            )
        return count


@dataclass
class FrustrationSignal:
    """A signal indicating potential agent frustration/looping."""

    severity: float  # 0.0 to 1.0
    reason: str
    suggested_action: str  # "compress", "stop", "hint"


class FrustrationManager:
    """Monitors execution stream for repetitive failures and noise.

    Phase 3.2 of Master Upgrade.
    """

    def __init__(self, history_size: int = 10) -> None:
        self.history: List[str] = []
        self._last_error_hash = ""
        self._consecutive_error_count = 0
        self.history_size = history_size

    def check(self, output: str, is_error: bool) -> Optional[FrustrationSignal]:
        """Analyze latest output for frustration signals."""

        # 1. Update history
        self.history.append(output[:1000])  # store prefix
        if len(self.history) > self.history_size:
            self.history.pop(0)

        # 2. Check repetitive errors
        if is_error:
            # Simple content hash for "same error" detection
            curr_hash = hashlib.md5(output.encode("utf-8", errors="ignore")).hexdigest()
            if curr_hash == self._last_error_hash:
                self._consecutive_error_count += 1
            else:
                self._last_error_hash = curr_hash
                self._consecutive_error_count = 1

            if self._consecutive_error_count >= 3:
                return FrustrationSignal(
                    severity=0.8,
                    reason=f"Identical error detected {self._consecutive_error_count} times in a row.",
                    suggested_action="stop",
                )
        else:
            # Reset on success? Or keep track of "spinning"?
            # For now, strict reset on clear success might be too optimistic if we are flaky.
            # But let's reset to be safe.
            self._consecutive_error_count = 0

        # 3. Check for massive stack trace noise (Context Compression)
        if len(output) > 2000 and "Traceback" in output:
            return FrustrationSignal(
                severity=0.4,
                reason="Large stack trace detected.",
                suggested_action="compress",
            )

        return None

    def compress_context(self, text: str) -> str:
        """Compress a large text block (e.g. stack trace) to essential info."""
        if len(text) < 1000:
            return text

        lines = text.splitlines()
        if len(lines) < 20:
            return text

        # Keep head and tail
        head = lines[:5]
        tail = lines[-15:]

        compressed = []
        compressed.extend(head)
        compressed.append(f"... [Compressed {len(lines) - 20} lines of output] ...")
        compressed.extend(tail)

        return "\n".join(compressed)
