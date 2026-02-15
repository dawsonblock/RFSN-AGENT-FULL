"""Multi-Agent Swarm — SwarmCoordinator.

Orchestrates the Architect → Coder → QA debate loop:

    1. Architect decomposes the task into subtasks
    2. For each subtask, Coder proposes a patch
    3. QA reviews the patch
    4. If APPROVE → commit; if REQUEST_CHANGES → revise (up to max_revisions)
    5. If REJECT or max revisions exceeded → escalate

All agent interactions go through pluggable callbacks so the coordinator
itself has no LLM dependency — it is pure control flow.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from rfsn_kernel.swarm.roles import AgentRole, ARCHITECT, CODER, QA
from rfsn_kernel.swarm.protocol import (
    Subtask,
    TaskDecomposition,
    PatchProposal,
    ReviewVerdict,
    RevisionRequest,
    Verdict,
)

log = logging.getLogger(__name__)


# ── Agent Callback Types ──────────────────────────────────────────────

# architect_fn(task, context) -> TaskDecomposition
ArchitectFn = Callable[[str, str], TaskDecomposition]

# coder_fn(subtask, context, revision_request?) -> PatchProposal
CoderFn = Callable[[Subtask, str, Optional[RevisionRequest]], PatchProposal]

# qa_fn(subtask, patch) -> ReviewVerdict
QAFn = Callable[[Subtask, PatchProposal], ReviewVerdict]


# ── Swarm Result ──────────────────────────────────────────────────────


@dataclass
class SubtaskResult:
    """Result of processing a single subtask through the debate loop."""

    subtask: Subtask
    final_patch: Optional[PatchProposal] = None
    final_verdict: Optional[ReviewVerdict] = None
    revisions: int = 0
    status: str = "pending"  # approved, rejected, escalated


@dataclass
class SwarmResult:
    """Full result of the swarm's work on a task."""

    task: str
    decomposition: Optional[TaskDecomposition] = None
    subtask_results: List[SubtaskResult] = field(default_factory=list)
    status: str = "pending"  # completed, partial, failed
    total_revisions: int = 0
    elapsed: float = 0.0
    ledger_entries: List[Dict[str, Any]] = field(default_factory=list)

    def approved_patches(self) -> List[PatchProposal]:
        """Return only patches that were approved by QA."""
        return [
            r.final_patch
            for r in self.subtask_results
            if r.status == "approved" and r.final_patch
        ]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task": self.task,
            "status": self.status,
            "total_revisions": self.total_revisions,
            "elapsed": round(self.elapsed, 3),
            "subtasks": [
                {
                    "description": r.subtask.description,
                    "status": r.status,
                    "revisions": r.revisions,
                }
                for r in self.subtask_results
            ],
        }


# ── Coordinator ───────────────────────────────────────────────────────


class SwarmCoordinator:
    """Orchestrates the multi-agent debate loop.

    This class is LLM-agnostic — it accepts callable "agent functions"
    that can be backed by any LLM, mock, or human.
    """

    def __init__(
        self,
        architect_fn: ArchitectFn,
        coder_fn: CoderFn,
        qa_fn: QAFn,
        max_revisions: int = 3,
        ledger_fn: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self.architect_fn = architect_fn
        self.coder_fn = coder_fn
        self.qa_fn = qa_fn
        self.max_revisions = max_revisions
        self._ledger_fn = ledger_fn or (lambda x: None)

    def _log(self, entry: Dict[str, Any], result: SwarmResult) -> None:
        """Append to both ledger and result."""
        entry["timestamp"] = time.time()
        self._ledger_fn(entry)
        result.ledger_entries.append(entry)

    def run(self, task: str, context: str = "") -> SwarmResult:
        """Execute the full swarm pipeline for a task.

        Args:
            task: The task description.
            context: Optional codebase context (repo map, file contents, etc.)

        Returns:
            SwarmResult with all subtask outcomes.
        """
        start = time.time()
        result = SwarmResult(task=task)

        # ── Phase 1: Architect decomposes ─────────────────────────
        self._log({"type": "SWARM_START", "task": task}, result)

        decomposition = self.architect_fn(task, context)
        result.decomposition = decomposition

        self._log(
            {
                "type": "DECOMPOSITION",
                "agent": "architect",
                "subtask_count": len(decomposition.subtasks),
                "strategy": decomposition.strategy,
                "fingerprint": decomposition.fingerprint(),
            },
            result,
        )

        if not decomposition.subtasks:
            result.status = "failed"
            self._log(
                {"type": "SWARM_END", "status": "failed", "reason": "no subtasks"},
                result,
            )
            result.elapsed = time.time() - start
            return result

        # ── Phase 2: For each subtask, Coder → QA debate ─────────
        for idx, subtask in enumerate(decomposition.subtasks):
            st_result = self._process_subtask(idx, subtask, context, result)
            result.subtask_results.append(st_result)
            result.total_revisions += st_result.revisions

        # ── Phase 3: Determine overall status ─────────────────────
        approved = sum(1 for r in result.subtask_results if r.status == "approved")
        total = len(result.subtask_results)

        if approved == total:
            result.status = "completed"
        elif approved > 0:
            result.status = "partial"
        else:
            result.status = "failed"

        result.elapsed = time.time() - start
        self._log(
            {
                "type": "SWARM_END",
                "status": result.status,
                "approved": approved,
                "total": total,
                "elapsed": round(result.elapsed, 3),
            },
            result,
        )

        return result

    def _process_subtask(
        self,
        idx: int,
        subtask: Subtask,
        context: str,
        result: SwarmResult,
    ) -> SubtaskResult:
        """Process a single subtask through the Coder → QA loop."""

        st_result = SubtaskResult(subtask=subtask)
        revision_request: Optional[RevisionRequest] = None

        for revision in range(self.max_revisions + 1):
            # ── Coder proposes ──
            patch = self.coder_fn(subtask, context, revision_request)
            st_result.final_patch = patch

            self._log(
                {
                    "type": "PATCH_PROPOSAL",
                    "agent": "coder",
                    "subtask_index": idx,
                    "revision": revision,
                    "fingerprint": patch.fingerprint(),
                    "files": list(patch.files_modified),
                },
                result,
            )

            # ── QA reviews ──
            verdict = self.qa_fn(subtask, patch)
            st_result.final_verdict = verdict

            self._log(
                {
                    "type": "REVIEW_VERDICT",
                    "agent": "qa",
                    "subtask_index": idx,
                    "verdict": verdict.verdict.value,
                    "revision": revision,
                },
                result,
            )

            if verdict.verdict == Verdict.APPROVE:
                st_result.status = "approved"
                st_result.revisions = revision
                return st_result

            elif verdict.verdict == Verdict.REJECT:
                st_result.status = "rejected"
                st_result.revisions = revision
                self._log(
                    {
                        "type": "SUBTASK_REJECTED",
                        "subtask_index": idx,
                        "reason": verdict.comments,
                    },
                    result,
                )
                return st_result

            elif verdict.verdict == Verdict.REQUEST_CHANGES:
                if revision >= self.max_revisions:
                    st_result.status = "escalated"
                    st_result.revisions = revision
                    self._log(
                        {
                            "type": "SUBTASK_ESCALATED",
                            "subtask_index": idx,
                            "reason": f"Max revisions ({self.max_revisions}) exceeded",
                        },
                        result,
                    )
                    return st_result

                # Create revision request for next round
                revision_request = RevisionRequest(
                    original_patch=patch,
                    qa_feedback=verdict.comments,
                    issues_to_fix=verdict.issues,
                    revision_number=revision + 1,
                )
                st_result.revisions = revision + 1

        # Should not reach here, but just in case
        st_result.status = "escalated"
        return st_result
