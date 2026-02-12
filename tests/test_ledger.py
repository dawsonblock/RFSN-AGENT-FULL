"""Tests for the hard ledger hash-chain."""

import json

from rfsn_kernel.hard_ledger import (
    HardLedger,
    LedgerRecord,
)


def _rec(action: str, run_id: str = "") -> LedgerRecord:
    return LedgerRecord(
        proposal_hash=f"p-{action}",
        simulation={"success_prob": 0.5},
        risk={"effective_risk": 0.2},
        decision="APPROVE",
        decision_reason="ok",
        outcome_hash=f"o-{action}",
        state_hash=f"s-{action}",
        metadata={
            "action": action,
            "run_id": run_id,
        },
    )


def test_ledger_writes_jsonl(tmp_path):
    path = str(tmp_path / "test_ledger.jsonl")
    lg = HardLedger(path)
    lg.append(_rec("a"))
    lg.append(_rec("b"))
    with open(path, "r", encoding="utf-8") as f:
        lines = [
            json.loads(line) for line in f
            if line.strip()
        ]
    assert len(lines) == 2
    assert lines[0]["metadata"]["action"] == "a"
    assert lines[1]["metadata"]["action"] == "b"


def test_ledger_has_timestamp(tmp_path):
    path = str(tmp_path / "test_ledger.jsonl")
    lg = HardLedger(path)
    lg.append(_rec("x"))
    with open(path, "r", encoding="utf-8") as f:
        rec = json.loads(f.readline())
    assert "ts" in rec
    assert "chain_hash" in rec


def test_ledger_hash_chain(tmp_path):
    path = str(tmp_path / "test_ledger.jsonl")
    lg = HardLedger(path)
    lg.append(_rec("a"))
    lg.append(_rec("b"))
    lg.append(_rec("c"))
    with open(path, "r", encoding="utf-8") as f:
        lines = [
            json.loads(line) for line in f
            if line.strip()
        ]
    for i in range(1, len(lines)):
        assert (
            lines[i]["prev_chain_hash"]
            == lines[i - 1]["chain_hash"]
        )


def test_ledger_creates_file(tmp_path):
    path = str(tmp_path / "sub" / "ledger.jsonl")
    lg = HardLedger(path)
    lg.append(_rec("init"))
    assert (tmp_path / "sub" / "ledger.jsonl").is_file()
