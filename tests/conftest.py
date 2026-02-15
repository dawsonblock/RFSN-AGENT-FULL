"""Shared test fixtures for RFSN test suite.

Provides common kernel, state, and proposal builders
to reduce boilerplate across 37+ test files.
"""

import os
import tempfile

import pytest

from rfsn_kernel.kernel import HardKernel, KernelStepResult
from rfsn_kernel.state import Proposal, SystemState, Outcome


@pytest.fixture
def basic_state():
    """A minimal SystemState with deterministic seed."""
    return SystemState(rng_seed=42)


@pytest.fixture
def basic_proposal():
    """A valid Proposal using the repo_read_range action."""
    return Proposal(
        action="repo_read_range",
        params={"path": "src/main.py", "line_start": 1, "line_end": 50},
        context_hash="ctx_abc",
        planner_hash="plan_xyz",
        intent="read source file",
        bundle_id="b1",
    )


@pytest.fixture
def basic_outcome():
    """A successful Outcome."""
    return Outcome(
        success=True,
        exit_code=0,
        payload="file contents here",
        error=None,
    )


@pytest.fixture
def tmp_dir():
    """Create a temporary directory that is cleaned up after the test."""
    with tempfile.TemporaryDirectory(prefix="rfsn_test_") as d:
        yield d


@pytest.fixture
def kernel_with_tmp_ledger(tmp_dir):
    """A HardKernel instance with its ledger writing to a temp directory."""
    ledger_path = os.path.join(tmp_dir, "test_ledger.jsonl")
    return HardKernel(ledger_path=ledger_path)
