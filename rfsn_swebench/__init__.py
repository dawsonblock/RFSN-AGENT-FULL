"""rfsn_swebench — SWE-bench patch/test bench runner."""

__version__ = "0.1.0"

from .contracts import (
    BenchResult,
    BenchTask,
    RiskReport,
    Status,
    TaskCommands,
    TaskHints,
    TaskLimits,
    TestRun,
)
from .gate import DiffStats, diff_analysis, patch_risk_gate, reload_policies
from .runner import Proposer, bench_run

__all__ = [
    "BenchResult",
    "BenchTask",
    "DiffStats",
    "Proposer",
    "RiskReport",
    "Status",
    "TaskCommands",
    "TaskHints",
    "TaskLimits",
    "TestRun",
    "bench_run",
    "diff_analysis",
    "patch_risk_gate",
    "reload_policies",
]
