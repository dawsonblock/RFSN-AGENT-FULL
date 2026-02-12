"""RFSN Hard Kernel — non-bypassable execution core.

All execution MUST flow through kernel_step().
No planner, tool, or memory write can bypass it.

Mandatory flow:
  PROPOSE → NORMALIZE → SIMULATE → RISK SCORE
  → DECIDE → EXECUTE → VERIFY → LEDGER
"""

from rfsn_kernel.state import SystemState, Proposal
from rfsn_kernel.normalize import normalize
from rfsn_kernel.validate import validate
from rfsn_kernel.simulate import simulate, SimResult
from rfsn_kernel.risk import risk_score, RiskBreakdown
from rfsn_kernel.decide import decide, Decision
from rfsn_kernel.verify import verify, VerificationResult
from rfsn_kernel.hard_ledger import HardLedger, LedgerRecord
from rfsn_kernel.kernel import HardKernel, KernelStepResult
from rfsn_kernel.run_state import RunState, RunStateStore
from rfsn_kernel.tier_policy import (
    TierDecision,
    tier_allows_step,
    pick_next_tier,
)
from rfsn_kernel.failure_kinds import extract_failure_kinds
from rfsn_kernel.command_infer import infer_commands
from rfsn_kernel.repair_loop import (
    next_phase,
    should_retry,
    update_state,
)
from rfsn_kernel.patch_minimize import minimize_unified_diff
from rfsn_kernel.sim_cache import SimCache
from rfsn_kernel.scheduler import Scheduler, RunBudget

__all__ = [
    "HardKernel",
    "KernelStepResult",
    "SystemState",
    "Proposal",
    "normalize",
    "validate",
    "simulate",
    "SimResult",
    "risk_score",
    "RiskBreakdown",
    "decide",
    "Decision",
    "verify",
    "VerificationResult",
    "HardLedger",
    "LedgerRecord",
    "RunState",
    "RunStateStore",
    "TierDecision",
    "tier_allows_step",
    "pick_next_tier",
    "extract_failure_kinds",
    "infer_commands",
    "next_phase",
    "should_retry",
    "update_state",
    "minimize_unified_diff",
    "SimCache",
    "Scheduler",
    "RunBudget",
]
