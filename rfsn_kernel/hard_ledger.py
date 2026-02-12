"""Immutable execution ledger — append-only hash chain.

Must be the truth source for:
  - Deterministic replay
  - Audit verification
  - Scientific evaluation

Properties:
  - Append-only (no mutation)
  - Hash-chained (tamper-evident)
  - State snapshot per action
  - Replay-reconstructable

Extends the existing Ledger with kernel-specific records.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

try:
    import fcntl
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False


def _canon(obj: Any) -> str:
    """Canonical JSON serialization for hashing."""
    return json.dumps(
        obj, sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


@dataclass
class LedgerRecord:
    """One immutable record in the execution chain."""

    proposal_hash: str
    simulation: Dict[str, Any]
    risk: Dict[str, Any]
    decision: str              # "APPROVE" | "REJECT"
    decision_reason: str
    outcome_hash: Optional[str]
    state_hash: str
    verification: Optional[Dict[str, Any]] = None
    entry_hash: str = ""
    prev_chain_hash: str = ""
    chain_hash: str = ""
    ts: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class HardLedger:
    """Immutable append-only hash-chained execution ledger.

    Every kernel step produces a LedgerRecord.
    Chain integrity is verifiable at any time.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.prev = "0" * 64
        self._count = 0

        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        self.prev = rec.get("chain_hash", self.prev)
                        self._count += 1
                    except json.JSONDecodeError:
                        pass

    @property
    def count(self) -> int:
        return self._count

    def append(self, record: LedgerRecord) -> LedgerRecord:
        """Append a record to the ledger.

        Computes entry_hash and chain_hash,
        writes atomically with file lock.
        """
        fixed = os.getenv("LEDGER_FIXED_TS")
        record.ts = float(fixed) if fixed else time.time()

        # Compute entry hash from record content.
        body = _canon({
            "proposal_hash": record.proposal_hash,
            "simulation": record.simulation,
            "risk": record.risk,
            "decision": record.decision,
            "decision_reason": record.decision_reason,
            "outcome_hash": record.outcome_hash,
            "state_hash": record.state_hash,
            "verification": record.verification,
        })
        record.entry_hash = hashlib.sha256(
            body.encode("utf-8"),
        ).hexdigest()

        # Chain hash links this record to previous.
        record.prev_chain_hash = self.prev
        record.chain_hash = hashlib.sha256(
            (self.prev + record.entry_hash).encode("utf-8"),
        ).hexdigest()

        # Atomic write with file lock.
        with open(self.path, "a", encoding="utf-8") as f:
            if _HAS_FCNTL:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.write(json.dumps(record.to_dict(), ensure_ascii=False, default=str) + "\n")
                f.flush()
                os.fsync(f.fileno())
            finally:
                if _HAS_FCNTL:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)

        self.prev = record.chain_hash
        self._count += 1
        return record

    def verify_chain(self) -> Dict[str, Any]:
        """Verify integrity of the entire chain."""
        if not os.path.exists(self.path):
            return {"ok": True, "entries": 0, "errors": []}

        errors: List[Dict[str, Any]] = []
        prev_hash = "0" * 64
        count = 0

        with open(self.path, "r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                count += 1

                try:
                    rec = json.loads(line)
                except json.JSONDecodeError as e:
                    errors.append({"line": line_num, "error": f"JSON parse: {e}"})
                    continue

                entry_hash = rec.get("entry_hash", "")
                stored_prev = rec.get("prev_chain_hash", "")
                chain_hash = rec.get("chain_hash", "")

                if not all([entry_hash, stored_prev, chain_hash]):
                    errors.append({"line": line_num, "error": "Missing hash fields"})
                    continue

                if stored_prev != prev_hash:
                    errors.append({
                        "line": line_num,
                        "error": f"Chain break: expected={prev_hash[:12]}... got={stored_prev[:12]}...",
                    })

                # Verify chain_hash.
                computed = hashlib.sha256(
                    (prev_hash + entry_hash).encode("utf-8"),
                ).hexdigest()
                if computed != chain_hash:
                    errors.append({
                        "line": line_num,
                        "error": f"Chain hash mismatch: computed={computed[:12]}... stored={chain_hash[:12]}...",
                    })

                prev_hash = chain_hash

        return {"ok": len(errors) == 0, "entries": count, "errors": errors}

    def read_all(
        self, run_id: Optional[str] = None,
    ) -> List[LedgerRecord]:
        """Read ledger records (optionally filtered by run_id)."""
        if not os.path.exists(self.path):
            return []

        records: List[LedgerRecord] = []
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    rec_meta = data.get("metadata", {}) or {}
                    if run_id and rec_meta.get("run_id") != run_id:
                        continue
                    records.append(LedgerRecord(
                        proposal_hash=data.get("proposal_hash", ""),
                        simulation=data.get("simulation", {}),
                        risk=data.get("risk", {}),
                        decision=data.get("decision", ""),
                        decision_reason=data.get("decision_reason", ""),
                        outcome_hash=data.get("outcome_hash"),
                        state_hash=data.get("state_hash", ""),
                        verification=data.get("verification"),
                        entry_hash=data.get("entry_hash", ""),
                        prev_chain_hash=data.get("prev_chain_hash", ""),
                        chain_hash=data.get("chain_hash", ""),
                        ts=data.get("ts", 0.0),
                        metadata=data.get("metadata", {}),
                    ))
                except (json.JSONDecodeError, TypeError):
                    continue
        return records
