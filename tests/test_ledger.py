"""Tests for the orchestrator ledger hash-chain."""
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
from ledger import Ledger  # noqa: E402  # type: ignore[import-not-found]


def test_ledger_writes_jsonl(tmp_path):
    path = str(tmp_path / "test_ledger.jsonl")
    lg = Ledger(path)
    lg.append({"type": "TEST", "val": 1})
    lg.append({"type": "TEST", "val": 2})
    with open(path, "r") as f:
        lines = [
            json.loads(line) for line in f
            if line.strip()
        ]
    assert len(lines) == 2
    # event is nested: rec["event"]["type"]
    assert lines[0]["event"]["type"] == "TEST"
    assert lines[1]["event"]["val"] == 2


def test_ledger_has_timestamp(tmp_path):
    path = str(tmp_path / "test_ledger.jsonl")
    lg = Ledger(path)
    lg.append({"type": "X"})
    with open(path, "r") as f:
        rec = json.loads(f.readline())
    assert "ts" in rec
    assert "chain_hash" in rec


def test_ledger_hash_chain(tmp_path):
    """Each record should chain to the previous."""
    path = str(tmp_path / "test_ledger.jsonl")
    lg = Ledger(path)
    lg.append({"type": "A"})
    lg.append({"type": "B"})
    lg.append({"type": "C"})
    with open(path, "r") as f:
        lines = [
            json.loads(line) for line in f
            if line.strip()
        ]
    # chain_hash[i] feeds into prev_chain_hash[i+1]
    for i in range(1, len(lines)):
        assert (
            lines[i]["prev_chain_hash"]
            == lines[i - 1]["chain_hash"]
        )


def test_ledger_creates_file(tmp_path):
    path = str(tmp_path / "sub" / "ledger.jsonl")
    lg = Ledger(path)
    lg.append({"type": "INIT"})
    assert os.path.isfile(path)
