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
        self,
        term: int,
        leader_id: str,
        prev_log_index: int,
        prev_log_term: int,
        entries: List[dict],
        leader_commit: int,
    ) -> dict:
        """Handle incoming AppendEntries RPC from leader."""
        # 1. Reply false if term < currentTerm (§5.1)
        if term < self.log.current_term:
            return {"term": self.log.current_term, "success": False}

        # Update current term if we see a valid leader
        if term > self.log.current_term:
            self.log.current_term = term
            # In a full implementation, we would convert to Follower here

        # 2. Reply false if log doesn't contain an entry at prevLogIndex
        # whose term matches prevLogTerm (§5.3)
        # Note: ConsensusLog.append handles the consistency check internally
        # but needs LogEntry objects, so we convert them here.
        log_entries = [
            LogEntry(
                term=e.get("term", term),
                index=e.get("index", prev_log_index + 1 + i),
                content=e.get("content", e),
                leader_id=leader_id,
            )
            for i, e in enumerate(entries)
        ]

        success = self.log.append(prev_log_index, prev_log_term, log_entries)

        if success:
            # 5. If leaderCommit > commitIndex, set commitIndex =
            # min(leaderCommit, index of last new entry)
            if leader_commit > self.log.commit_index:
                last_new_index = (
                    log_entries[-1].index if log_entries else prev_log_index
                )
                self.log.commit_index = min(leader_commit, last_new_index)

        return {"term": self.log.current_term, "success": success}
