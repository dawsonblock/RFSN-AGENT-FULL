import os
import sys
import shutil
import unittest
import threading
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mock duckdb before importing services
sys.modules["duckdb"] = MagicMock()

from services.orchestrator.run_engine import run_logic, RunReq
from services.orchestrator.session_state import clear_run_context


class TestTrajectoryHarvesting(unittest.TestCase):
    def setUp(self):
        self.test_db = "test_learner.duckdb"
        if os.path.exists(self.test_db):
            os.remove(self.test_db)

    def tearDown(self):
        if os.path.exists(self.test_db):
            os.remove(self.test_db)

    @patch("services.orchestrator.run_engine.sandbox_create")
    @patch("services.orchestrator.run_engine.sandbox_destroy")
    @patch("services.orchestrator.run_engine.init_replay_manifest")
    @patch("services.orchestrator.run_engine.finalize_replay_manifest")
    @patch("services.orchestrator.run_engine.execute_approved_step")
    @patch("services.learner_service.store_duckdb.DuckStore")
    def test_run_logic_harvests_trajectory(
        self, MockStore, mock_exec, mock_finalize, mock_init, mock_destroy, mock_create
    ):
        # Setup mocks
        mock_create.return_value = {"id": "sandbox-1"}
        mock_exec.return_value = {"ok": True, "output": "done"}
        mock_init.return_value = {"run_id": "test-run-1"}

        # We need a real store instance to test the logic, but we can mock the DB connection if needed.
        # However, run_engine instantiates DuckStore internally.
        # We patched DuckStore class, so mock_store_instance is what run_engine gets.
        mock_store_instance = MockStore.return_value

        # Run — provide a manual_plan with one step so the loop executes.
        req = RunReq(
            repo_id="test-repo",
            task="fix bug",
            max_iters=1,
            manual_plan=[{"type": "read_file", "path": "src/main.py"}],
        )
        kernel = MagicMock()
        ledger = MagicMock()

        run_logic("test-run-1", req, kernel, ledger)

        # Verify record_trajectory was called
        mock_store_instance.record_trajectory.assert_called_once()

        call_args = mock_store_instance.record_trajectory.call_args
        self.assertIsNotNone(call_args)

        _, kwargs = call_args
        self.assertEqual(kwargs["run_id"], "test-run-1")
        self.assertEqual(kwargs["repo_id"], "test-repo")
        self.assertTrue(kwargs["success"])
        self.assertEqual(len(kwargs["steps"]), 1)
        self.assertEqual(kwargs["steps"][0]["iteration"], 1)


if __name__ == "__main__":
    unittest.main()
