"""Multi-Agent Swarm — Message Protocol.

Structured, immutable message types for inter-agent communication.
All messages are JSON-serializable and logged to the Hard Ledger.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional


class Verdict(str, Enum):
    """QA review verdict."""

    APPROVE = "APPROVE"
    REQUEST_CHANGES = "REQUEST_CHANGES"
    REJECT = "REJECT"


# ── Message Types ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class Subtask:
    """A single subtask in the Architect's decomposition."""

    description: str
    target_files: tuple = ()
    acceptance_criteria: str = ""
    priority: int = 0


@dataclass(frozen=True)
class TaskDecomposition:
    """Architect → Coordinator: analysis and subtask breakdown."""

    analysis: str
    subtasks: tuple  # tuple of Subtask
    strategy: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["subtasks"] = [
            asdict(s) if hasattr(s, "__dataclass_fields__") else s
            for s in self.subtasks
        ]
        return d

    def fingerprint(self) -> str:
        body = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(body.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class PatchProposal:
    """Coder → Coordinator: a proposed code change."""

    subtask_index: int
    diff: str
    reasoning: str
    files_modified: tuple = ()
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def fingerprint(self) -> str:
        body = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(body.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class ReviewVerdict:
    """QA → Coordinator: review of a patch proposal."""

    verdict: Verdict
    comments: str
    issues: tuple = ()  # specific issues found
    tests_passed: Optional[bool] = None
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["verdict"] = self.verdict.value
        return d


@dataclass(frozen=True)
class RevisionRequest:
    """Coordinator → Coder: feedback to address in next revision."""

    original_patch: PatchProposal
    qa_feedback: str
    issues_to_fix: tuple = ()
    revision_number: int = 1
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "original_patch_fingerprint": self.original_patch.fingerprint(),
            "qa_feedback": self.qa_feedback,
            "issues_to_fix": list(self.issues_to_fix),
            "revision_number": self.revision_number,
            "timestamp": self.timestamp,
        }
        return d
