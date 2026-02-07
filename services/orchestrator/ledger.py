import fcntl
import hashlib
import json
import os
import time
from typing import Any


def _canon(obj: Any) -> str:
    return json.dumps(
        obj, sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _strip_ts(x: Any) -> Any:
    if isinstance(x, dict):
        return {
            k: _strip_ts(v)
            for k, v in x.items()
            if k not in ("ts", "time", "timestamp")
        }
    if isinstance(x, list):
        return [_strip_ts(v) for v in x]
    return x


def normalize_event(event: dict) -> dict:  # type: ignore[type-arg]
    e: dict = _strip_ts(dict(event))
    if e.get("type") == "STEP_RESULT":
        out: dict = e.get("out", {}) or {}
        logs: str = out.get("logs") or ""
        logs = logs.replace("\r\n", "\n")
        logs_norm = "\n".join([ln.rstrip() for ln in logs.split("\n")]).strip()
        e["out"] = {
            "status": int(out.get("status", 0)),
            "seconds": round(float(out.get("seconds", 0.0)), 3),
            "logs_sha256": hashlib.sha256(
                logs_norm.encode("utf-8")
            ).hexdigest(),
        }
    return e


class Ledger:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.prev = "0"*64
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    rec = json.loads(line)
                    self.prev = rec.get("chain_hash", self.prev)

    def append(self, event: dict) -> dict:
        fixed = os.getenv("LEDGER_FIXED_TS")
        event = dict(event)
        event["ts"] = float(fixed) if fixed is not None else time.time()

        norm = normalize_event(event)
        body = _canon(norm)

        entry_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        chain_hash = hashlib.sha256(
            (self.prev + entry_hash).encode("utf-8")
        ).hexdigest()

        rec = {
            "event": norm,
            "entry_hash": entry_hash,
            "prev_chain_hash": self.prev,
            "chain_hash": chain_hash,
            "ts": event["ts"],
        }

        with open(self.path, "a", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

        self.prev = chain_hash
        return rec
