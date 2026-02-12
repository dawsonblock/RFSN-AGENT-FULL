from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Optional


class SimCache:
    """Per-run deterministic execution-result cache for repeated steps."""

    def __init__(self) -> None:
        self._m: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def key(step: Dict[str, Any], workdir: str = "") -> str:
        blob = json.dumps(
            {
                "step": step,
                "workdir": workdir,
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        v = self._m.get(key)
        return dict(v) if isinstance(v, dict) else None

    def put(self, key: str, value: Dict[str, Any]) -> None:
        self._m[key] = dict(value)

    def size(self) -> int:
        return len(self._m)
