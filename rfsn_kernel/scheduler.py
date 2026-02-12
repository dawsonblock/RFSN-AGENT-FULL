from __future__ import annotations

import threading
import time
from typing import Dict


class RunBudget:
    def __init__(self, max_seconds: int = 900):
        self.start = time.time()
        self.max_seconds = int(max_seconds)

    def ok(self) -> bool:
        return (time.time() - self.start) <= self.max_seconds


class Scheduler:
    """Concurrency + runtime budget control for active runs."""

    def __init__(self, max_concurrent: int = 2):
        self.max_concurrent = max(1, int(max_concurrent))
        self._lock = threading.Lock()
        self._active: Dict[str, RunBudget] = {}

    def start_run(self, run_id: str, max_seconds: int = 900) -> bool:
        rid = run_id or "__default__"
        with self._lock:
            if rid in self._active:
                return True
            if len(self._active) >= self.max_concurrent:
                return False
            self._active[rid] = RunBudget(max_seconds=max_seconds)
            return True

    def end_run(self, run_id: str) -> None:
        rid = run_id or "__default__"
        with self._lock:
            self._active.pop(rid, None)

    def budget_ok(self, run_id: str) -> bool:
        rid = run_id or "__default__"
        with self._lock:
            b = self._active.get(rid)
            return b.ok() if b else False

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {
                "max_concurrent": self.max_concurrent,
                "active": len(self._active),
            }
