from __future__ import annotations

from typing import Dict, Optional


class WorkdirStore:
    def __init__(self) -> None:
        self._runs: Dict[str, Dict[str, str]] = {}

    def set_run_workdirs(self, run_id: str, mapping: Dict[str, str]) -> None:
        rid = run_id or "__default__"
        self._runs[rid] = dict(mapping)

    def get_rel(self, run_id: str, workdir_id: str) -> Optional[str]:
        rid = run_id or "__default__"
        m = self._runs.get(rid, {})
        return m.get(workdir_id)

    def clear(self, run_id: str) -> None:
        rid = run_id or "__default__"
        self._runs.pop(rid, None)
