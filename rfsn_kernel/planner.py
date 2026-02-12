"""Hierarchical planner — three-layer control model.

Prevents planner overload, enables multi-stage
execution, and maintains bounded behavior over
extended runtime.

Layers:
  Strategic (long horizon) — subgoal decomposition
  Tactical  (mid horizon)  — plan construction
  Execution (short horizon) — proposal generation

Each layer reduces complexity for the next.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Subgoal:
    """One unit of strategic work."""

    goal_id: str
    description: str
    completed: bool = False
    attempts: int = 0
    max_attempts: int = 3

    def to_dict(self) -> Dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "description": self.description,
            "completed": self.completed,
            "attempts": self.attempts,
        }


@dataclass
class TacticalPlan:
    """A structured plan for achieving a subgoal."""

    subgoal: Subgoal
    actions: List[Dict[str, Any]]     # ordered step types
    expected_outcome: str
    fallback: List[Dict[str, Any]] = field(default_factory=list)
    cost_estimate: float = 0.0
    risk_estimate: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "subgoal": self.subgoal.to_dict(),
            "actions": self.actions,
            "expected_outcome": self.expected_outcome,
            "fallback_count": len(self.fallback),
            "cost_estimate": self.cost_estimate,
        }


@dataclass
class StrategicState:
    """Global task controller state."""

    goal: str = ""
    subgoals: List[Subgoal] = field(default_factory=list)
    current_subgoal_idx: int = 0
    progress: float = 0.0         # 0.0–1.0
    stability: float = 1.0        # 1.0=stable, 0.0=unstable
    stagnation_count: int = 0     # steps without progress
    total_attempts: int = 0
    escalation_count: int = 0     # times we re-planned

    def to_dict(self) -> Dict[str, Any]:
        return {
            "goal": self.goal,
            "subgoal_count": len(self.subgoals),
            "completed_count": sum(1 for s in self.subgoals if s.completed),
            "current_idx": self.current_subgoal_idx,
            "progress": round(self.progress, 3),
            "stability": round(self.stability, 3),
            "stagnation": self.stagnation_count,
        }


# ── Subgoal decomposition templates ──
# These map task patterns to common subgoal sequences.

_DECOMPOSITION_TEMPLATES: Dict[str, List[str]] = {
    "fix_test": [
        "Locate the failing test and read its assertions",
        "Search for the implementation under test",
        "Read the implementation to understand the bug",
        "Apply a minimal fix to the implementation",
        "Run targeted tests to verify the fix",
        "Run full test suite to confirm no regressions",
    ],
    "fix_import": [
        "Identify the missing module or broken import",
        "Determine if it's an internal or external dependency",
        "Fix the import path or install the dependency",
        "Verify the import resolves correctly",
        "Run tests to confirm no regressions",
    ],
    "fix_syntax": [
        "Locate the syntax error from the traceback",
        "Read the surrounding code context",
        "Apply a surgical syntax fix",
        "Run tests to verify the fix",
    ],
    "fix_generic": [
        "Search for code relevant to the task",
        "Read relevant source files",
        "Apply a minimal, targeted fix",
        "Run targeted tests",
        "Run full test suite",
    ],
}


class HierarchicalPlanner:
    """Three-layer control model for long-horizon tasks.

    Strategic → Tactical → Execution
    """

    def __init__(
        self,
        max_stagnation: int = 5,
        max_escalations: int = 3,
    ) -> None:
        self.state = StrategicState()
        self.max_stagnation = max_stagnation
        self.max_escalations = max_escalations
        self._plans: List[TacticalPlan] = []
        self._current_plan: Optional[TacticalPlan] = None

    def reset(self) -> None:
        """Reset planner state for a new run."""
        self.state = StrategicState()
        self._plans = []
        self._current_plan = None

    # ── Strategic Layer ──

    def set_goal(
        self, goal: str, task_type: str = "fix_generic",
    ) -> List[Subgoal]:
        """Decompose a goal into subgoals.

        Uses decomposition templates based on task type.
        """
        self.state.goal = goal
        template = _DECOMPOSITION_TEMPLATES.get(
            task_type,
            _DECOMPOSITION_TEMPLATES["fix_generic"],
        )

        subgoals: List[Subgoal] = []
        for i, desc in enumerate(template):
            subgoals.append(Subgoal(
                goal_id=f"sg-{i + 1}",
                description=desc,
            ))

        self.state.subgoals = subgoals
        self.state.current_subgoal_idx = 0
        self.state.progress = 0.0
        return subgoals

    def current_subgoal(self) -> Optional[Subgoal]:
        """Get the current active subgoal."""
        idx = self.state.current_subgoal_idx
        if idx < len(self.state.subgoals):
            sg = self.state.subgoals[idx]
            if not sg.completed:
                return sg
        # Find next incomplete.
        for i, sg in enumerate(self.state.subgoals):
            if not sg.completed:
                self.state.current_subgoal_idx = i
                return sg
        return None  # all complete

    def advance_subgoal(self) -> Optional[Subgoal]:
        """Mark current subgoal complete and advance."""
        sg = self.current_subgoal()
        if sg:
            sg.completed = True
            self._update_progress()
            self.state.stagnation_count = 0
        return self.current_subgoal()

    def record_no_progress(self) -> bool:
        """Record a step without progress.

        Returns True if stagnation threshold hit.
        """
        self.state.stagnation_count += 1
        self.state.stability = max(
            0.0,
            self.state.stability - 0.1,
        )
        return self.state.stagnation_count >= self.max_stagnation

    def escalate(self) -> Optional[Subgoal]:
        """Escalate: re-plan or revise current subgoal.

        Called when stagnation is detected.
        """
        self.state.escalation_count += 1
        self.state.stagnation_count = 0

        sg = self.current_subgoal()
        if sg:
            sg.attempts += 1
            if sg.attempts >= sg.max_attempts:
                # Skip this subgoal — too many failures.
                sg.completed = True
                self._update_progress()
                return self.current_subgoal()

        return sg

    def _update_progress(self) -> None:
        """Recalculate progress from completed subgoals."""
        if not self.state.subgoals:
            self.state.progress = 0.0
            return
        done = sum(1 for sg in self.state.subgoals if sg.completed)
        self.state.progress = done / len(self.state.subgoals)

    # ── Tactical Layer ──

    def tactical_plan(
        self,
        subgoal: Subgoal,
        available_actions: Optional[List[str]] = None,
    ) -> TacticalPlan:
        """Convert a subgoal into an executable plan.

        Uses the subgoal description to select
        appropriate action sequence.
        """
        actions: List[Dict[str, Any]] = []
        desc = subgoal.description.lower()

        if "search" in desc or "locate" in desc or "find" in desc:
            actions.append({"type": "repo_search", "phase": "search"})
        if "read" in desc or "understand" in desc:
            actions.append({"type": "repo_read_range", "phase": "read"})
        if "fix" in desc or "patch" in desc or "apply" in desc:
            actions.append({"type": "apply_patch", "phase": "patch"})
        if "test" in desc or "verify" in desc or "confirm" in desc:
            actions.append({"type": "run_tests", "phase": "test"})
        if "install" in desc or "dependency" in desc:
            actions.append({"type": "ensure_deps", "phase": "deps"})

        # Default: search + read if no clear action.
        if not actions:
            actions = [
                {"type": "repo_search", "phase": "search"},
                {"type": "repo_read_range", "phase": "read"},
            ]

        # Build fallback.
        fallback: List[Dict[str, Any]] = []
        if any(a["type"] == "apply_patch" for a in actions):
            fallback = [
                {"type": "repo_search", "phase": "retry_search"},
                {"type": "repo_read_range", "phase": "retry_read"},
                {"type": "apply_patch", "phase": "retry_patch"},
            ]

        plan = TacticalPlan(
            subgoal=subgoal,
            actions=actions,
            expected_outcome=f"Complete: {subgoal.description}",
            fallback=fallback,
        )

        self._current_plan = plan
        self._plans.append(plan)
        return plan

    # ── Execution Layer Integration ──

    def get_execution_context(self) -> Dict[str, Any]:
        """Build context for the execution layer (kernel + LLM).

        Returns strategic context to guide the planner.
        """
        sg = self.current_subgoal()
        plan = self._current_plan

        return {
            "goal": self.state.goal,
            "progress": round(self.state.progress, 3),
            "stability": round(self.state.stability, 3),
            "current_subgoal": sg.to_dict() if sg else None,
            "current_plan": plan.to_dict() if plan else None,
            "remaining_subgoals": sum(
                1 for s in self.state.subgoals if not s.completed
            ),
            "escalation_count": self.state.escalation_count,
            "stagnation": self.state.stagnation_count,
        }

    def get_planner_guidance(self) -> str:
        """Generate guidance text for the LLM planner.

        Tells the planner what subgoal to focus on
        and what phase to use.
        """
        sg = self.current_subgoal()
        if not sg:
            return "All subgoals completed. Verify final state."

        plan = self._current_plan
        progress_pct = int(self.state.progress * 100)

        lines = [
            f"## Strategic Context (progress: {progress_pct}%)",
            f"Current subgoal [{sg.goal_id}]: {sg.description}",
        ]

        if plan:
            phase_types = [a["type"] for a in plan.actions]
            lines.append(f"Expected actions: {', '.join(phase_types)}")

        if self.state.stagnation_count > 0:
            lines.append(
                f"WARNING: {self.state.stagnation_count} steps"
                f" without progress. Focus or escalate."
            )

        if self.state.stability < 0.5:
            lines.append(
                "WARNING: System stability low."
                " Use conservative actions."
            )

        remaining = sum(
            1 for s in self.state.subgoals if not s.completed
        )
        lines.append(f"Remaining subgoals: {remaining}")

        return "\n".join(lines)

    def classify_task(self, task: str, failure_class: str = "") -> str:
        """Classify a task into a decomposition template type."""
        task_lower = task.lower()
        fail_lower = failure_class.lower()

        if "import" in fail_lower or "module" in fail_lower:
            return "fix_import"
        if "syntax" in fail_lower:
            return "fix_syntax"
        if "assert" in fail_lower or "test" in task_lower:
            return "fix_test"
        return "fix_generic"

    def get_stats(self) -> Dict[str, Any]:
        """Return planner statistics."""
        return {
            "goal": self.state.goal,
            "subgoals_total": len(self.state.subgoals),
            "subgoals_completed": sum(
                1 for s in self.state.subgoals if s.completed
            ),
            "progress": round(self.state.progress, 3),
            "stability": round(self.state.stability, 3),
            "plans_created": len(self._plans),
            "escalations": self.state.escalation_count,
            "stagnation": self.state.stagnation_count,
        }
