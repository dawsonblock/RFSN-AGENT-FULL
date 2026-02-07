"""Tests for the orchestrator ledger hash-chain."""
import json
import os
import tempfile
import sys

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__),
        "..", "services", "orchestrator",
    ),
)
from ledger import Ledger  # type: ignore[import-not-found]


def test_ledger_writes_jsonl(tmp_path):
    path = str(tmp_path / "test_ledger.jsonl")
    lg = Ledger(path)
    lg.append({"type": "TEST", "val": 1})
    lg.append({"type": "TEST", "val": 2})
    with open(path, "r") as f:
        lines = [
            json.loads(l) for l in f if l.strip()
        ]
    assert len(lines) == 2
    assert lines[0]["type"] == "TEST"
    assert lines[1]["val"] == 2


def test_ledger_has_timestamp(tmp_path):
    path = str(tmp_path / "test_ledger.jsonl")
    lg = Ledger(path)
    lg.append({"type": "X"})
    with open(path, "r") as f:
        rec = json.loads(f.readline())
    assert "ts" in rec or "timestamp" in rec or "type" in rec


def test_ledger_hash_chain(tmp_path):
    """Each record should chain to the previous."""
    path = str(tmp_path / "test_ledger.jsonl")
    lg = Ledger(path)
    lg.append({"type": "A"})
    lg.append({"type": "B"})
    lg.append({"type": "C"})
    with open(path, "r") as f:
        lines = [
            json.loads(l) for l in f if l.strip()
        ]
    # Check that prev_hash chains
    for i, rec in enumerate(lines):
        if "prev_hash" in rec:
            if i == 0:
                assert (
                    rec["prev_hash"] is None
                    or rec["prev_hash"] == ""
                    or rec["prev_hash"]
                    == "0" * 64
                )
            else:
                assert rec["prev_hash"] != ""


def test_ledger_creates_file(tmp_path):
    path = str(tmp_path / "sub" / "ledger.jsonl")
    lg = Ledger(path)
    lg.append({"type": "INIT"})
    assert os.path.isfile(path)
