"""Multi-Agent Swarm — rfsn_kernel.swarm package.

Provides the SwarmCoordinator and agent role definitions for
multi-agent collaboration (Architect / Coder / QA).
"""

from rfsn_kernel.swarm.roles import (
    AgentRole,
    ARCHITECT,
    CODER,
    QA,
    ALL_ROLES,
)
from rfsn_kernel.swarm.protocol import (
    Subtask,
    TaskDecomposition,
    PatchProposal,
    ReviewVerdict,
    RevisionRequest,
    Verdict,
)
from rfsn_kernel.swarm.coordinator import (
    SwarmCoordinator,
    SwarmResult,
    SubtaskResult,
)

__all__ = [
    "AgentRole",
    "ARCHITECT",
    "CODER",
    "QA",
    "ALL_ROLES",
    "Subtask",
    "TaskDecomposition",
    "PatchProposal",
    "ReviewVerdict",
    "RevisionRequest",
    "Verdict",
    "SwarmCoordinator",
    "SwarmResult",
    "SubtaskResult",
]
