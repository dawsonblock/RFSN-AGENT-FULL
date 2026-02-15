"""Multi-Agent Swarm — Agent Role Definitions.

Defines the three specialized roles that collaborate via the SwarmCoordinator:
  - Architect: Plans and decomposes tasks
  - Coder: Writes and applies patches
  - QA: Reviews, tests, and critiques
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet, Dict, Any


@dataclass(frozen=True)
class AgentRole:
    """Immutable definition of an agent role in the swarm."""

    name: str
    persona: str
    allowed_actions: FrozenSet[str]
    temperature: float = 0.3
    max_tokens: int = 4096

    def can_do(self, action: str) -> bool:
        """Check if this role is allowed to perform the given action."""
        return action in self.allowed_actions

    def enforce(self, action: str) -> None:
        """Raise if this role cannot perform the given action."""
        if not self.can_do(action):
            raise PermissionError(
                f"Role '{self.name}' cannot perform '{action}'. "
                f"Allowed: {sorted(self.allowed_actions)}"
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "persona": self.persona,
            "allowed_actions": sorted(self.allowed_actions),
            "temperature": self.temperature,
        }


# ── Predefined Roles ──────────────────────────────────────────────────

ARCHITECT = AgentRole(
    name="architect",
    persona=(
        "You are the Architect. Your job is to analyze the task, "
        "understand the codebase structure, and decompose the work into "
        "clear, actionable subtasks. You decide WHAT to do and WHERE, "
        "but you never write code yourself. Output a structured plan "
        "with acceptance criteria for each subtask."
    ),
    allowed_actions=frozenset(
        {
            "read_file",
            "search",
            "list_dir",
            "plan",
            "generate_repo_map",
            "analyze",
        }
    ),
    temperature=0.2,
)

CODER = AgentRole(
    name="coder",
    persona=(
        "You are the Coder. You receive subtasks from the Architect and "
        "implement them precisely. Write clean, minimal patches that "
        "satisfy the acceptance criteria. You can read files for context "
        "and run commands to test, but defer all planning decisions to "
        "the Architect and all review to QA."
    ),
    allowed_actions=frozenset(
        {
            "read_file",
            "write_file",
            "apply_patch",
            "run_command",
            "search",
        }
    ),
    temperature=0.3,
)

QA = AgentRole(
    name="qa",
    persona=(
        "You are QA. You review patches from the Coder with a critical eye. "
        "Check for correctness, edge cases, security issues, and style. "
        "Run tests to verify behavior. You MUST issue a clear verdict: "
        "APPROVE (ready to commit), REQUEST_CHANGES (specific feedback), "
        "or REJECT (fundamental flaw requiring re-planning)."
    ),
    allowed_actions=frozenset(
        {
            "read_file",
            "run_tests",
            "run_command",
            "critique",
            "search",
        }
    ),
    temperature=0.1,
)

ALL_ROLES = {"architect": ARCHITECT, "coder": CODER, "qa": QA}
