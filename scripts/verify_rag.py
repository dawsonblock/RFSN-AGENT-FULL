import os
import sys
import json
import shutil
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.learner_service.playbooks import REGISTRY, retrieve_playbooks, Playbook


class TestRAG(unittest.TestCase):
    def setUp(self):
        self.test_dir = "temp_rag_playbooks"
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)
        os.makedirs(self.test_dir)

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    def test_static_retrieval(self):
        """Verify retrieval of static playbooks."""
        # 1. Import Fix
        results = retrieve_playbooks("import error missing module", limit=1)
        self.assertTrue(len(results) > 0)
        self.assertEqual(results[0].playbook_id, "PB_import_fix")

        # 2. Syntax Fix
        results = retrieve_playbooks("syntax error invalid syntax", limit=1)
        self.assertTrue(len(results) > 0)
        self.assertEqual(results[0].playbook_id, "PB_syntax_fix")

    def test_dynamic_persistence(self):
        """Verify loading dynamic playbooks from disk."""
        # Create a custom playbook JSON
        custom_pb = {
            "playbook_id": "PB_dynamic_custom",
            "name": "Custom Dynamic Fix",
            "description": "A dynamically loaded playbook for specific edge cases.",
            "target_failures": ["CustomError"],
            "phases": [
                {
                    "label": "detect",
                    "step_type": "repo_search",
                    "guidance": "Find the custom error.",
                    "max_calls": 1,
                }
            ],
        }

        with open(os.path.join(self.test_dir, "custom.json"), "w") as f:
            json.dump(custom_pb, f)

        # Load it
        count = REGISTRY.load_from_directory(self.test_dir)
        self.assertEqual(count, 1)

        # Retrieve it
        results = retrieve_playbooks("custom dynamic fix", limit=1)
        self.assertTrue(len(results) > 0)
        self.assertEqual(results[0].playbook_id, "PB_dynamic_custom")
        print(f"Successfully retrieved dynamic playbook: {results[0].name}")


if __name__ == "__main__":
    unittest.main()
