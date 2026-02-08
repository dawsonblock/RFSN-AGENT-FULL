"""Input/output contracts for SWE-bench-compatible task execution."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

Status = Literal["PASS", "FAIL", "ABORT"]
RiskDecision = Literal["ALLOW", "REJECT"]


@dataclass
class TaskLimits:
    max_iters: int = 8
    max_patch_bytes: int = 250_000
    max_files_touched: int = 25
    max_new_files: int = 5
    max_runtime_sec: int = 1800


@dataclass
class TaskCommands:
    setup: List[str] = field(default_factory=list)
    test_quick: str = "pytest -q"
    test_full: str = "pytest -q"


@dataclass
class TaskHints:
    failing_tests: List[str] = field(default_factory=list)
    focus_files: List[str] = field(default_factory=list)
    test_patch: str = ""


@dataclass
class BenchTask:
    task_id: str
    repo_url: str
    workdir: str
    issue_text: str
    repo_ref: Optional[str] = None
    hints: TaskHints = field(default_factory=TaskHints)
    commands: TaskCommands = field(default_factory=TaskCommands)
    limits: TaskLimits = field(default_factory=TaskLimits)


@dataclass
class TestRun:
    __test__ = False  # not a pytest test class

    exit_code: int
    stdout_tail: str
    stderr_tail: str
    duration_sec: float


@dataclass
class RiskReport:
    decision: RiskDecision
    reasons: List[str] = field(default_factory=list)


@dataclass
class BenchResult:
    task_id: str
    status: Status
    iters: int
    final_patch_unified_diff: str
    tests: Dict[str, TestRun]
    risk: RiskReport
    replay_dir: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "iters": self.iters,
            "final_patch_unified_diff": self.final_patch_unified_diff,
            "tests": {
                k: {
                    "exit_code": v.exit_code,
                    "stdout_tail": v.stdout_tail,
                    "stderr_tail": v.stderr_tail,
                    "duration_sec": v.duration_sec,
                }
                for k, v in self.tests.items()
            },
            "risk": {
                "decision": self.risk.decision,
                "reasons": self.risk.reasons,
            },
            "replay_dir": self.replay_dir,
        }
