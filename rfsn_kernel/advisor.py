"""Advisor module for ingesting and managing external feedback (PR comments, etc.)."""

from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional


@dataclass
class Advice:
    source: str
    content: str
    timestamp: float
    acknowledged: bool = False


class Advisor:
    """Manages advice and feedback from external sources (e.g. GitHub PRs)."""

    def __init__(self):
        self._advice_log: List[Advice] = []
        self._pending_feedback: List[Advice] = []

    def ingest_pr_comment(self, body: str, author: str):
        """Ingest a comment from a PR review."""
        import time

        advice = Advice(
            source=f"pr_comment:{author}", content=body, timestamp=time.time()
        )
        self._advice_log.append(advice)
        self._pending_feedback.append(advice)

    def has_pending_advice(self) -> bool:
        return bool(self._pending_feedback)

    def get_pending_advice(self) -> List[Advice]:
        """Retrieve and clear pending advice."""
        advice = list(self._pending_feedback)
        self._pending_feedback.clear()
        return advice

    def get_history(self) -> List[Advice]:
        return list(self._advice_log)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pending_count": len(self._pending_feedback),
            "history_count": len(self._advice_log),
        }
