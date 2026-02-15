"""Tests for Phase 7.2: Cryptographic Ledger Chain hardening."""

import json
import os
import tempfile
import unittest

from rfsn_kernel.hard_ledger import HardLedger, LedgerRecord


def _make_record(**overrides) -> LedgerRecord:
    defaults = dict(
        proposal_hash="abc123",
        simulation={"steps": 3},
        risk={"score": 0.2},
        decision="APPROVE",
        decision_reason="low risk",
        outcome_hash="def456",
        state_hash="ghi789",
    )
    defaults.update(overrides)
    return LedgerRecord(**defaults)


class TestHardLedgerChain(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "ledger.jsonl")

    def test_append_and_verify(self):
        ledger = HardLedger(self.path, auto_verify=False)
        ledger.append(_make_record())
        ledger.append(_make_record(proposal_hash="xyz"))
        result = ledger.verify_chain()
        self.assertTrue(result["ok"])
        self.assertEqual(result["entries"], 2)

    def test_tamper_detection(self):
        ledger = HardLedger(self.path, auto_verify=False)
        ledger.append(_make_record())
        ledger.append(_make_record())

        # Tamper with the file
        with open(self.path, "r") as f:
            lines = f.readlines()
        rec = json.loads(lines[0])
        rec["decision"] = "REJECT"  # Tamper
        lines[0] = json.dumps(rec) + "\n"
        with open(self.path, "w") as f:
            f.writelines(lines)

        ledger2 = HardLedger(self.path, auto_verify=False)
        result = ledger2.verify_chain()
        self.assertFalse(result["ok"])
        self.assertGreater(len(result["errors"]), 0)

    def test_chain_continuity(self):
        ledger = HardLedger(self.path, auto_verify=False)
        r1 = ledger.append(_make_record())
        r2 = ledger.append(_make_record())
        self.assertEqual(r2.prev_chain_hash, r1.chain_hash)

    def test_reload_preserves_chain(self):
        ledger = HardLedger(self.path, auto_verify=False)
        ledger.append(_make_record())
        ledger.append(_make_record())

        # Reload
        ledger2 = HardLedger(self.path, auto_verify=True)
        r3 = ledger2.append(_make_record())
        result = ledger2.verify_chain()
        self.assertTrue(result["ok"])
        self.assertEqual(result["entries"], 3)

    def test_read_all(self):
        ledger = HardLedger(self.path, auto_verify=False)
        ledger.append(_make_record(metadata={"run_id": "run1"}))
        ledger.append(_make_record(metadata={"run_id": "run2"}))
        all_recs = ledger.read_all()
        self.assertEqual(len(all_recs), 2)
        filtered = ledger.read_all(run_id="run1")
        self.assertEqual(len(filtered), 1)


if __name__ == "__main__":
    unittest.main()
