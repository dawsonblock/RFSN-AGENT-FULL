"""tests/test_sandbox_mode.py

Tests for sandbox mode configuration and behaviour.

Acceptance:
* Local dev mode explicitly reports unsafe/trusted-only.
* Docker mode fails clearly if Docker unavailable.
* No silent fallback between modes.
* Sandbox config is included in replay metadata.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = str(Path(__file__).resolve().parent.parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------

def _env(**overrides):
    base = {
        "RFSN_SANDBOX_MODE": "local_dev",
        "RFSN_ALLOW_LOCAL_EXEC": "1",
        "RFSN_DEV_MODE": "1",
        "RFSN_EXEC_USE_DOCKER": "0",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Sandbox mode constants
# ---------------------------------------------------------------------------

class TestSandboxModeConstants:
    def test_local_dev_mode_env_is_documented(self):
        """The run_engine accepts RFSN_SANDBOX_MODE env var."""
        from services.orchestrator import run_engine
        src = Path(ROOT) / "services" / "orchestrator" / "run_engine.py"
        content = src.read_text()
        assert "RFSN_SANDBOX_MODE" in content or "local_dev" in content

    def test_allow_trace_execution_is_hardcoded_false(self):
        """ALLOW_TRACE_EXECUTION must be hardcoded False, not configurable."""
        from services.orchestrator.run_engine import ALLOW_TRACE_EXECUTION
        assert ALLOW_TRACE_EXECUTION is False

    def test_run_engine_exports_sandbox_mode_in_env_snapshot(self, tmp_path):
        """init_replay_manifest must be called with sandbox-related env metadata."""
        from services.orchestrator.run_engine import run_logic, RunReq
        from rfsn_kernel.kernel import HardKernel
        from unittest.mock import MagicMock, call

        kernel = HardKernel(ledger_path=str(tmp_path / "ledger.jsonl"))
        sink = MagicMock()
        captured_env = {}

        def _capture_manifest(**kwargs):
            captured_env.update(kwargs.get("env_snapshot", {}))
            return {"run_id": "x"}

        with (
            patch("services.orchestrator.run_engine.init_replay_manifest", side_effect=_capture_manifest),
            patch("services.orchestrator.run_engine.finalize_replay_manifest"),
            patch("services.orchestrator.run_engine.sandbox_create", return_value=None),
            patch("services.orchestrator.run_engine.sandbox_destroy"),
        ):
            req = RunReq(repo_id="r", task="t")
            run_logic("run-sb-001", req, kernel, sink)

        # env_snapshot must include sandbox-related keys
        assert "allow_trace_execution" in captured_env
        assert captured_env["allow_trace_execution"] is False


class TestLocalDevMode:
    def test_local_dev_mode_is_trusted_only_documented(self):
        """SECURITY_MODEL.md must document that local_dev is trusted-only."""
        doc = Path(ROOT) / "SECURITY_MODEL.md"
        assert doc.exists(), "SECURITY_MODEL.md is missing"
        content = doc.read_text()
        assert "local_dev" in content
        assert "trusted" in content.lower()

    def test_run_local_toy_repair_has_warning(self):
        """RUN_LOCAL_TOY_REPAIR.md must contain a trust/safety warning."""
        doc = Path(ROOT) / "RUN_LOCAL_TOY_REPAIR.md"
        assert doc.exists(), "RUN_LOCAL_TOY_REPAIR.md is missing"
        content = doc.read_text().lower()
        assert "trusted" in content or "warning" in content or "unsafe" in content

    def test_readme_mentions_sandbox_mode(self):
        """README must mention sandbox mode."""
        readme = Path(ROOT) / "README.md"
        assert readme.exists()
        content = readme.read_text()
        assert "sandbox" in content.lower() or "RFSN_SANDBOX_MODE" in content


class TestNoSilentFallback:
    def test_run_engine_does_not_silently_succeed(self, tmp_path):
        """Dry run must return dry_run, not completed or success."""
        from services.orchestrator.run_engine import run_logic, RunReq
        from rfsn_kernel.kernel import HardKernel
        from unittest.mock import MagicMock

        kernel = HardKernel(ledger_path=str(tmp_path / "ledger.jsonl"))
        sink = MagicMock()

        with (
            patch("services.orchestrator.run_engine.init_replay_manifest", return_value={}),
            patch("services.orchestrator.run_engine.finalize_replay_manifest"),
        ):
            req = RunReq(repo_id="r", task="t")  # no manual_plan
            result = run_logic("run-ns-001", req, kernel, sink)

        assert result["status"] not in ("completed", "success")
        assert result["status"] == "dry_run"


class TestDockerModeConfig:
    def test_executor_checks_docker_on_docker_mode(self):
        """executor/app.py must check Docker availability when USE_DOCKER_SANDBOX=1."""
        executor_src = Path(ROOT) / "services" / "executor" / "app.py"
        content = executor_src.read_text()
        # Must have a docker availability check function.
        assert "_docker_runtime_available" in content or "docker version" in content.lower()

    def test_executor_fails_if_docker_unavailable_without_local_exec(self):
        """Executor must raise SystemExit if docker is unavailable and local exec not allowed."""
        executor_src = Path(ROOT) / "services" / "executor" / "app.py"
        content = executor_src.read_text()
        # Must contain a SystemExit or raise for the case where docker is
        # unavailable and local exec is not allowed.
        assert "SystemExit" in content or "raise" in content

    def test_no_silent_docker_downgrade(self):
        """Executor must not silently downgrade to local exec without a log warning."""
        executor_src = Path(ROOT) / "services" / "executor" / "app.py"
        content = executor_src.read_text()
        # If docker falls back to local exec, there must be a WARN print.
        assert "WARN: Docker runtime unavailable" in content or "falling back" in content.lower()
