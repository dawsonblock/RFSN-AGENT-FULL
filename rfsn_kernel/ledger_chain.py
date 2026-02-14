"""Hash-chain ledger for tamper detection.

Each ledger entry includes the hash of the previous entry,
forming an immutable chain. Any modification to a past entry
invalidates all subsequent hashes.
"""

import hashlib
import json
import os
from typing import Any, Dict, List, Optional


def _entry_hash(entry: Dict[str, Any], prev_hash: str) -> str:
    """Compute the hash of a ledger entry including the previous hash.

    The entry is serialized canonically (sorted keys, no whitespace)
    and concatenated with the previous hash.
    """
    canonical = json.dumps(entry, sort_keys=True, separators=(",", ":"))
    payload = f"{prev_hash}|{canonical}"
    return hashlib.sha256(payload.encode()).hexdigest()


class LedgerChain:
    """An append-only hash-chain ledger.

    Each entry stores its chain_hash linking to the previous entry.
    """

    def __init__(self, seed: str = "GENESIS"):
        self._entries: List[Dict[str, Any]] = []
        self._seed = seed
        self._prev_hash = hashlib.sha256(seed.encode()).hexdigest()

    def append(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        """Append an entry to the chain.

        The entry dict is augmented with:
          - _chain_index: position in the chain
          - _chain_hash: hash linking to previous entry
          - _prev_hash: hash of the previous entry

        Returns the augmented entry.
        """
        chain_entry = dict(entry)
        chain_entry["_chain_index"] = len(self._entries)
        chain_entry["_prev_hash"] = self._prev_hash

        # Compute hash WITHOUT _chain_hash in the entry
        new_hash = _entry_hash(entry, self._prev_hash)
        chain_entry["_chain_hash"] = new_hash

        self._entries.append(chain_entry)
        self._prev_hash = new_hash
        return chain_entry

    def verify(self) -> Dict[str, Any]:
        """Verify the entire chain's integrity.

        Returns:
            dict with keys: ok, length, first_invalid_index
        """
        prev_hash = hashlib.sha256(self._seed.encode()).hexdigest()

        for i, entry in enumerate(self._entries):
            stored_prev = entry.get("_prev_hash")
            if stored_prev != prev_hash:
                return {
                    "ok": False,
                    "length": len(self._entries),
                    "first_invalid_index": i,
                    "reason": "prev_hash mismatch",
                }

            # Reconstruct the entry without chain metadata
            clean = {
                k: v
                for k, v in entry.items()
                if k not in ("_chain_index", "_chain_hash", "_prev_hash")
            }
            expected_hash = _entry_hash(clean, prev_hash)
            actual_hash = entry.get("_chain_hash")

            if expected_hash != actual_hash:
                return {
                    "ok": False,
                    "length": len(self._entries),
                    "first_invalid_index": i,
                    "reason": "chain_hash mismatch (content tampered)",
                }

            prev_hash = actual_hash

        return {
            "ok": True,
            "length": len(self._entries),
            "first_invalid_index": None,
        }

    def head_hash(self) -> str:
        """Return the hash of the most recent entry."""
        return self._prev_hash

    def entries(self) -> List[Dict[str, Any]]:
        """Return a copy of all entries."""
        return list(self._entries)

    def save(self, path: str) -> None:
        """Persist the ledger to a JSON file."""
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump(
                {
                    "seed": self._seed,
                    "entries": self._entries,
                },
                f,
                indent=2,
            )

    @classmethod
    def load(cls, path: str) -> "LedgerChain":
        """Load a ledger from a JSON file."""
        with open(path) as f:
            data = json.load(f)
        chain = cls(seed=data.get("seed", "GENESIS"))
        chain._entries = data.get("entries", [])
        if chain._entries:
            chain._prev_hash = chain._entries[-1]["_chain_hash"]
        return chain

    def __len__(self) -> int:
        return len(self._entries)
