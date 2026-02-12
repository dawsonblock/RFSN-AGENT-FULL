from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class RunState:
    run_id: str
    tier: int = 0
    failure_kinds: List[str] = field(default_factory=list)


class RunStateStore:
    def __init__(self) -> None:
        self._runs: Dict[str, RunState] = {}

    def get(self, run_id: str) -> RunState:
        rid = run_id or "__default__"
        rs = self._runs.get(rid)
        if rs is None:
            rs = RunState(run_id=rid, tier=0)
            self._runs[rid] = rs
        return rs

    def set_tier(self, run_id: str, tier: int) -> None:
        rs = self.get(run_id)
        rs.tier = int(tier)

    def clear(self, run_id: str) -> None:
        rid = run_id or "__default__"
        self._runs.pop(rid, None)

    def snapshot(self) -> Dict[str, Dict[str, object]]:
        return {
            rid: {
                "tier": rs.tier,
                "failure_kinds": list(rs.failure_kinds),
            }
            for rid, rs in self._runs.items()
        }
