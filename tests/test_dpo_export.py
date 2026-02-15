import os
import sys
import unittest
import json
import sqlite3
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mock duckdb
sys.modules["duckdb"] = MagicMock()

from services.learner_service.dpo_export import export_for_dpo


class TestDPOExport(unittest.TestCase):
    def setUp(self):
        self.db_path = "test_dpo.duckdb"
        self.output_path = "test_dpo_output.jsonl"
        # Mock file existence checks
        self.patcher = patch("os.path.exists")
        self.mock_exists = self.patcher.start()
        self.mock_exists.return_value = True

    def tearDown(self):
        self.patcher.stop()
        if os.path.exists(self.output_path):
            os.remove(self.output_path)

    @patch("services.learner_service.dpo_export.DuckStore")
    def test_export_logic(self, MockStore):
        # Setup mock data
        mock_store = MockStore.return_value

        # task_hash, success, steps, run_id
        mock_rows = [
            # Chosen run
            (
                "task_1",
                True,
                json.dumps([{"intent": {"task": "Fix Bug A"}, "result": "Done"}]),
                "run_1",
            ),
            # Rejected run
            (
                "task_1",
                False,
                json.dumps([{"intent": {"cmd": "ls"}, "result": "Error"}]),
                "run_2",
            ),
            # Unpaired chosen
            ("task_2", True, "[]", "run_3"),
        ]

        # DuckDB connection execute returns a cursor, which we call fetchall on.
        # So mock_store.con.execute(query) -> mock_cursor
        # mock_cursor.fetchall() -> mock_rows
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = mock_rows
        mock_store.con.execute.return_value = mock_cursor

        # Run export
        count = export_for_dpo(self.db_path, self.output_path)

        # Verify
        self.assertEqual(count, 1)  # Only 1 pair

        with open(self.output_path, "r") as f:
            lines = f.readlines()
            self.assertEqual(len(lines), 1)
            entry = json.loads(lines[0])

            self.assertEqual(entry["prompt"], "Fix Bug A")
            self.assertEqual(len(entry["chosen"]), 2)  # User + Assist
            self.assertEqual(len(entry["rejected"]), 2)
            self.assertEqual(entry["metadata"]["task_hash"], "task_1")


if __name__ == "__main__":
    unittest.main()
