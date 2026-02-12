"""Replay directory management: events + blobs.

Supports optional bridging into the RFSN hash-chained Ledger when running in
integrated mode (pass ``ledger_path`` to ``init_replay_dir``).
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, Optional

from .util import now_ms, sha256_bytes

# ---------------------------------------------------------------------------
# Optional import: HardLedger bridge (available in full stack)
# ---------------------------------------------------------------------------
_LedgerBridge = None


def _try_import_ledger():
    """Lazily import a bridge that appends events to HardLedger."""
    global _LedgerBridge
    if _LedgerBridge is not None:
        return _LedgerBridge
    try:
        from rfsn_kernel.hard_ledger import (
            HardLedger,
            LedgerRecord,
        )

        class _ReplayBridge:
            def __init__(self, path: str):
                self._ledger = HardLedger(path)

            def append(self, event: Dict[str, Any]) -> None:
                ev = dict(event or {})
                ev_type = str(ev.get("type", "REPLAY_EVENT"))
                run_id = str(ev.get("run_id", ""))
                blob = json.dumps(
                    ev,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    default=str,
                )
                import hashlib
                ev_hash = hashlib.sha256(
                    blob.encode("utf-8"),
                ).hexdigest()
                state_hash = hashlib.sha256(
                    f"replay_event:{ev_hash}".encode("utf-8"),
                ).hexdigest()
                rec = LedgerRecord(
                    proposal_hash=ev_hash,
                    simulation={},
                    risk={},
                    decision="REJECT",
                    decision_reason=f"event:{ev_type}",
                    outcome_hash=None,
                    state_hash=state_hash,
                    metadata={
                        "record_type": "replay_event",
                        "event_type": ev_type,
                        "run_id": run_id,
                        "event": ev,
                    },
                )
                self._ledger.append(rec)

        _LedgerBridge = _ReplayBridge
    except ImportError:
        _LedgerBridge = None  # type: ignore[assignment]
    return _LedgerBridge


# ---------------------------------------------------------------------------
# Replay directory
# ---------------------------------------------------------------------------
_ledger_instance: Any = None


def init_replay_dir(
    base: str,
    task_id: str,
    *,
    ledger_path: Optional[str] = None,
) -> str:
    """Create replay dir and optionally bind a Ledger.

    Path: ``<base>/replays/<task_id>_<ts>/``.
    """
    global _ledger_instance
    d = os.path.join(base, "replays", f"{task_id}_{now_ms()}")
    os.makedirs(d, exist_ok=True)

    if ledger_path:
        Klass = _try_import_ledger()
        if Klass is not None:
            _ledger_instance = Klass(ledger_path)
        else:
            print(
                "[replay] HardLedger not importable — "
                "falling back to flat JSONL replay log",
                file=sys.stderr,
            )
    return d


def log_event(replay_dir: str, event: Dict[str, Any]) -> None:
    """Append *event* to ``events.jsonl``.

    Also writes to hash-chained Ledger if bound.
    """
    path = os.path.join(replay_dir, "events.jsonl")
    line = json.dumps(event, ensure_ascii=False)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")

    # Also write to hash-chained Ledger if available
    if _ledger_instance is not None:
        try:
            _ledger_instance.append(event)
        except Exception:
            pass  # best-effort


def save_blob(replay_dir: str, name: str, data: bytes) -> str:
    """Save a binary blob and return its path."""
    h = sha256_bytes(data)
    path = os.path.join(replay_dir, "blobs", f"{name}.{h[:12]}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path
