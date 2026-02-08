"""Tests for new intelligence features:
parse_failure_signature, compute_dense_reward,
and ledger verify_chain.
"""
import json
import os
import sys

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__),
        "..", "services", "orchestrator",
    ),
)
from context_fingerprint import (  # noqa: E402
    parse_failure_signature,
    compute_dense_reward,
    _parse_test_counts,
)
from ledger import Ledger  # noqa: E402


# ── parse_failure_signature ──────────────────

class TestParseFailureSignature:

    def test_empty_text(self):
        r = parse_failure_signature("")
        assert r["failure_class"] == "none"
        assert r["signature_hash"] == "0" * 16

    def test_import_error(self):
        text = (
            "Traceback (most recent call last):\n"
            "  File 'x.py', line 1\n"
            "ImportError: No module named 'foo.bar'\n"
        )
        r = parse_failure_signature(text)
        assert r["failure_class"] == "ImportError"
        assert r["failure_module"] == "foo.bar"
        assert r["failure_message"] != ""
        assert len(r["signature_hash"]) == 16

    def test_attribute_error_module(self):
        text = (
            "AttributeError: module 'os' has no"
            " attribute 'magic'\n"
        )
        r = parse_failure_signature(text)
        assert r["failure_class"] == "AttributeError"
        assert r["failure_module"] == "os"

    def test_pytest_failed_test_name(self):
        text = (
            "FAILED tests/test_x.py::TestFoo"
            "::test_bar - AssertionError\n"
            "=== 3 passed, 1 failed in 2.0s ===\n"
        )
        r = parse_failure_signature(text)
        assert r["failure_test"] == (
            "tests/test_x.py::TestFoo::test_bar"
        )
        assert r["failure_class"] == "AssertionError"

    def test_test_name_short_form(self):
        text = "test_something FAILED\n"
        r = parse_failure_signature(text)
        assert r["failure_test"] == "test_something"

    def test_signature_hash_deterministic(self):
        text = "ImportError: No module named 'foo'\n"
        r1 = parse_failure_signature(text)
        r2 = parse_failure_signature(text)
        assert r1["signature_hash"] == r2["signature_hash"]

    def test_test_counts_parsed(self):
        text = (
            "=== 5 passed, 2 failed, 1 error"
            " in 3.45s ===\n"
        )
        r = parse_failure_signature(text)
        tc = r["test_counts"]
        assert tc is not None
        assert tc["passed"] == 5
        assert tc["failed"] == 2
        assert tc["error"] == 1
        assert tc["total"] == 8

    def test_no_test_counts_when_absent(self):
        text = "some random log output\n"
        r = parse_failure_signature(text)
        assert r["test_counts"] is None


# ── _parse_test_counts ───────────────────────

class TestParseTestCounts:

    def test_standard_format(self):
        text = "=== 12 passed in 1.23s ==="
        tc = _parse_test_counts(text)
        assert tc is not None
        assert tc["passed"] == 12
        assert tc["failed"] == 0
        assert tc["total"] == 12

    def test_mixed_results(self):
        text = (
            "=== 5 passed, 3 failed,"
            " 1 error in 4.0s ==="
        )
        tc = _parse_test_counts(text)
        assert tc["passed"] == 5
        assert tc["failed"] == 3
        assert tc["error"] == 1
        assert tc["total"] == 9

    def test_empty_string(self):
        assert _parse_test_counts("") is None

    def test_no_test_info(self):
        tc = _parse_test_counts(
            "just some random output"
        )
        assert tc is None


# ── compute_dense_reward ─────────────────────

class TestComputeDenseReward:

    def test_full_pass(self):
        curr = {"passed": 10, "failed": 0,
                "error": 0, "total": 10}
        r = compute_dense_reward(None, curr)
        assert r == 1.0

    def test_no_data(self):
        r = compute_dense_reward(None, None)
        assert r == 0.0

    def test_improvement(self):
        prev = {"passed": 5, "failed": 5,
                "error": 0, "total": 10}
        curr = {"passed": 8, "failed": 2,
                "error": 0, "total": 10}
        r = compute_dense_reward(prev, curr)
        assert r > 0.2  # improved

    def test_regression_from_green(self):
        prev = {"passed": 10, "failed": 0,
                "error": 0, "total": 10}
        curr = {"passed": 8, "failed": 2,
                "error": 0, "total": 10}
        r = compute_dense_reward(prev, curr)
        assert r == -1.0  # was green, now red

    def test_same_failures(self):
        prev = {"passed": 5, "failed": 5,
                "error": 0, "total": 10}
        curr = {"passed": 5, "failed": 5,
                "error": 0, "total": 10}
        r = compute_dense_reward(prev, curr)
        assert r == 0.2  # no change

    def test_first_measurement_with_failures(self):
        curr = {"passed": 5, "failed": 3,
                "error": 0, "total": 8}
        r = compute_dense_reward(None, curr)
        assert r == -0.2

    def test_zero_total(self):
        curr = {"passed": 0, "failed": 0,
                "error": 0, "total": 0}
        r = compute_dense_reward(None, curr)
        assert r == 0.0


# ── Ledger verify_chain ─────────────────────

class TestLedgerVerifyChain:

    def test_empty_ledger(self, tmp_path):
        path = str(tmp_path / "ledger.jsonl")
        lg = Ledger(path)
        result = lg.verify_chain()
        assert result["ok"] is True
        assert result["entries"] == 0

    def test_valid_chain(self, tmp_path):
        path = str(tmp_path / "ledger.jsonl")
        lg = Ledger(path)
        lg.append({"type": "A", "val": 1})
        lg.append({"type": "B", "val": 2})
        lg.append({"type": "C", "val": 3})
        result = lg.verify_chain()
        assert result["ok"] is True
        assert result["entries"] == 3
        assert result["errors"] == []

    def test_tampered_entry(self, tmp_path):
        path = str(tmp_path / "ledger.jsonl")
        lg = Ledger(path)
        lg.append({"type": "A"})
        lg.append({"type": "B"})

        # Tamper with the second entry.
        with open(path, "r") as f:
            lines = f.readlines()
        rec = json.loads(lines[1])
        rec["event"]["type"] = "TAMPERED"
        lines[1] = json.dumps(rec) + "\n"
        with open(path, "w") as f:
            f.writelines(lines)

        # Verify detects the tampering.
        lg2 = Ledger(path)
        result = lg2.verify_chain()
        assert result["ok"] is False
        assert len(result["errors"]) > 0

    def test_broken_chain_link(self, tmp_path):
        path = str(tmp_path / "ledger.jsonl")
        lg = Ledger(path)
        lg.append({"type": "A"})
        lg.append({"type": "B"})
        lg.append({"type": "C"})

        # Break the chain by swapping lines 2 and 3.
        with open(path, "r") as f:
            lines = f.readlines()
        lines[1], lines[2] = lines[2], lines[1]
        with open(path, "w") as f:
            f.writelines(lines)

        lg2 = Ledger(path)
        result = lg2.verify_chain()
        assert result["ok"] is False
