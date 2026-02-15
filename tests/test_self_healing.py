"""Unit tests for the Self-Healing Core (Phase 7.3)."""

import unittest
from rfsn_kernel.self_healing.signals import extract_signals, Signal
from rfsn_kernel.self_healing.memory import FailureMemory, FailureCluster
from rfsn_kernel.self_healing.core import SelfHealingCore, HardeningLevel


class TestSignalExtraction(unittest.TestCase):

    def test_success_signal(self):
        sig = extract_signals("all tests passed", 0)
        self.assertEqual(sig.failure_type, "success")
        self.assertEqual(sig.anomaly_score, 0.0)

    def test_import_error(self):
        logs = (
            "Traceback (most recent call last):\n"
            '  File "main.py", line 1, in <module>\n'
            "ModuleNotFoundError: No module named 'foo'\n"
        )
        sig = extract_signals(logs, 1)
        self.assertEqual(sig.failure_type, "import_error")
        self.assertIn("foo", sig.detail)
        self.assertGreater(sig.anomaly_score, 0.0)

    def test_timeout_signal(self):
        sig = extract_signals("running...\n[TIMEOUT]\n", 124)
        self.assertEqual(sig.failure_type, "timeout")
        self.assertGreaterEqual(sig.anomaly_score, 0.7)

    def test_unknown_failure(self):
        sig = extract_signals("something weird happened", 1)
        self.assertEqual(sig.failure_type, "unknown")

    def test_fingerprint_stability(self):
        logs = (
            "Traceback (most recent call last):\n"
            '  File "a.py", line 10, in func\n'
            "TypeError: bad arg\n"
        )
        s1 = extract_signals(logs, 1)
        # Same traceback with different line number should fingerprint the same
        logs2 = logs.replace("line 10", "line 42")
        s2 = extract_signals(logs2, 1)
        self.assertEqual(s1.stack_fingerprint, s2.stack_fingerprint)


class TestFailureMemory(unittest.TestCase):

    def test_record_and_retrieve(self):
        mem = FailureMemory()
        c = mem.record("fp1", "import_error", "no module foo")
        self.assertEqual(c.count, 1)
        self.assertEqual(c.failure_type, "import_error")

    def test_cluster_count_increments(self):
        mem = FailureMemory()
        mem.record("fp1", "import_error")
        mem.record("fp1", "import_error")
        c = mem.record("fp1", "import_error")
        self.assertEqual(c.count, 3)

    def test_auto_root_cause(self):
        mem = FailureMemory()
        c = mem.record("fp1", "syntax_error", "unexpected indent")
        self.assertIsNotNone(c.root_cause)
        self.assertEqual(c.root_cause.cause_id, "bad_patch")

    def test_eviction(self):
        mem = FailureMemory(max_clusters=3)
        mem.record("a", "t1")
        mem.record("b", "t2")
        mem.record("c", "t3")
        mem.record("d", "t4")  # Should evict oldest
        self.assertEqual(len(mem.clusters), 3)
        self.assertNotIn("a", mem.clusters)

    def test_summary(self):
        mem = FailureMemory()
        mem.record("fp1", "timeout")
        mem.record("fp2", "import_error")
        s = mem.summary()
        self.assertEqual(s["total_clusters"], 2)


class TestSelfHealingCore(unittest.TestCase):

    def test_initial_state(self):
        core = SelfHealingCore()
        self.assertEqual(core.hardening_level, HardeningLevel.BALANCED)
        self.assertEqual(core.stability_score, 1.0)

    def test_stability_drops_on_failures(self):
        core = SelfHealingCore(window_size=5)
        for _ in range(5):
            core.ingest("error", 1)
        self.assertEqual(core.stability_score, 0.0)
        self.assertEqual(core.hardening_level, HardeningLevel.HARDENED)

    def test_stability_recovers(self):
        core = SelfHealingCore(window_size=5)
        # Start bad
        for _ in range(3):
            core.ingest("error", 1)
        # Recover
        for _ in range(5):
            core.ingest("ok", 0)
        self.assertGreaterEqual(core.stability_score, 0.8)
        self.assertEqual(core.hardening_level, HardeningLevel.FAST)

    def test_snapshot(self):
        core = SelfHealingCore()
        core.ingest("ok", 0)
        snap = core.snapshot()
        self.assertEqual(snap.total_runs, 1)
        self.assertEqual(snap.stability_score, 1.0)

    def test_reset(self):
        core = SelfHealingCore()
        core.ingest("err", 1)
        core.reset()
        self.assertEqual(core.stability_score, 1.0)
        self.assertEqual(core._total_runs, 0)

    def test_signal_returned(self):
        core = SelfHealingCore()
        sig = core.ingest("ModuleNotFoundError: No module named 'bar'", 1)
        self.assertEqual(sig.failure_type, "import_error")


if __name__ == "__main__":
    unittest.main()
