"""Deterministic Consensus via Raft-lite."""

from dataclasses import dataclass
from typing import List, Optional


@dataclass
class LogEntry:
    term: int
    index: int
    content: dict
    leader_id: str


class ConsensusLog:
    """Raft-style appended log."""

    def __init__(self, node_id: str):
        self.node_id = node_id
        self.current_term = 0
        self.log: List[LogEntry] = []
        self.commit_index = -1

    def append(self, prev_index: int, prev_term: int, entries: List[LogEntry]) -> bool:
        """Append entries if consistency check passes."""
        # Check consistency
        if prev_index >= 0:
            if prev_index >= len(self.log):
                return False  # Gap
            if self.log[prev_index].term != prev_term:
                return False  # Term mismatch (divergence)

        # Append new entries (handling conflict/truncate if needed)
        # Simplified: just append for now
        start = prev_index + 1
        for i, entry in enumerate(entries):
            idx = start + i
            if idx < len(self.log):
                if self.log[idx].term != entry.term:
                    # Conflict: delete existing and following
                    self.log = self.log[:idx]
                    self.log.append(entry)
            else:
                self.log.append(entry)

        return True

    def propose(self, content: dict) -> LogEntry:
        """Propose a new entry as leader."""
        entry = LogEntry(
            term=self.current_term,
            index=len(self.log),
            content=content,
            leader_id=self.node_id,
        )
        self.log.append(entry)
        return entry


class ConsensusNode:
    def __init__(self, node_id: str):
        self.log = ConsensusLog(node_id)

    def receive_append_entries(
        self, leader_id: str, prev_index: int, prev_term: int, entries: List[dict]
    ):
        # TODO: implement Raft AppendEntries RPC
        # This requires: term validation, log conflict resolution,
        # commit index advancement, and follower state machine updates.
        # See Raft paper §5.3 for specification.
        raise NotImplementedError(
            "Raft AppendEntries RPC is not yet implemented. "
            "ConsensusLog.append() handles local log operations only."
        )
