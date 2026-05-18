import sqlite3
import json
import os
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Optional


@dataclass
class Outcome:
    """A single task attempt outcome."""

    task_id: str
    status: str  # PASS, FAIL, ABORT, GATE_REJECT
    repo: str  # e.g. "django__django"
    error_type: str = ""
    error_summary: str = ""
    files_changed: list[str] = field(default_factory=list)
    patch_snippet: str = ""
    iters_used: int = 0
    dense_reward: float = 0.0
    timestamp: float = 0.0

    @property
    def repo_family(self) -> str:
        parts = self.repo.split("-")
        if len(parts) > 1 and parts[-1].isdigit():
            return "-".join(parts[:-1])
        return self.repo


class OutcomeMemory:
    """SQLite-backed outcome store with retrieval for LLM prompting.

    Note: Previously this store used JSONL files.  It was migrated to SQLite
    for better query performance.  The constructor still accepts ``.jsonl``
    paths (converting them to ``.db``) so that existing call-sites are not
    broken, but the underlying storage is always SQLite.
    """

    def __init__(self, path: str) -> None:
        # Accept legacy .jsonl paths and silently convert to .db.
        if path.endswith(".jsonl"):
            path = path[:-6] + ".db"
        self._path = path
        self._conn: Optional[sqlite3.Connection] = None
        self._ensure_table()
        # Lazy-loaded outcome list (populated on first access via _outcomes).
        self._outcomes_cache: Optional[list] = None

    def _ensure_table(self) -> None:
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS outcomes (
                    task_id TEXT,
                    status TEXT,
                    repo TEXT,
                    error_type TEXT,
                    error_summary TEXT,
                    files_changed TEXT, -- JSON
                    patch_snippet TEXT,
                    iters_used INTEGER,
                    dense_reward REAL,
                    timestamp REAL
                )
            """
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_repo ON outcomes(repo)")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_status ON outcomes(status)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ts ON outcomes(timestamp)"
            )

    def record(
        self,
        task_id: str,
        status: str,
        repo: str,
        *,
        error_type: str = "",
        error_summary: str = "",
        files_changed: Optional[list[str]] = None,
        patch_snippet: str = "",
        iters_used: int = 0,
        dense_reward: float = 0.0,
    ) -> None:
        """Append an outcome to the store."""
        if not self._conn:
            self._ensure_table()

        with self._conn:
            self._conn.execute(
                """INSERT INTO outcomes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task_id,
                    status,
                    repo,
                    error_type,
                    error_summary,
                    json.dumps(files_changed or []),
                    patch_snippet[:500],
                    iters_used,
                    max(0.0, min(1.0, dense_reward)),
                    time.time(),
                ),
            )
        # Invalidate lazy cache after write.
        self._outcomes_cache = None

    def _row_to_outcome(self, row: sqlite3.Row) -> Outcome:
        return Outcome(
            task_id=row["task_id"],
            status=row["status"],
            repo=row["repo"],
            error_type=row["error_type"],
            error_summary=row["error_summary"],
            files_changed=json.loads(row["files_changed"]),
            patch_snippet=row["patch_snippet"],
            iters_used=row["iters_used"],
            dense_reward=row["dense_reward"],
            timestamp=row["timestamp"],
        )

    # ------------------------------------------------------------------
    # Convenience properties (mirrors the legacy JSONL-based interface)
    # ------------------------------------------------------------------

    @property
    def _outcomes(self) -> list:
        """Lazy-loaded list of all Outcome objects (for legacy test compatibility)."""
        if self._outcomes_cache is None:
            if not self._conn:
                self._ensure_table()
            cursor = self._conn.execute(
                "SELECT * FROM outcomes ORDER BY timestamp ASC"
            )
            self._outcomes_cache = [self._row_to_outcome(r) for r in cursor]
        return self._outcomes_cache

    @property
    def total_outcomes(self) -> int:
        """Total number of recorded outcomes."""
        if not self._conn:
            self._ensure_table()
        row = self._conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()
        return int(row[0])

    @property
    def pass_rate(self) -> float:
        """Fraction of outcomes with status == 'PASS'.  Returns 0.0 if empty."""
        total = self.total_outcomes
        if total == 0:
            return 0.0
        row = self._conn.execute(
            "SELECT COUNT(*) FROM outcomes WHERE status = 'PASS'"
        ).fetchone()
        return int(row[0]) / total

    def get_similar_tasks(
        self, query: str, *, max_results: int = 5
    ) -> list[Outcome]:
        """Return outcomes whose error_summary contains words from *query*.

        Results are ordered by number of matching keywords (most matches first).
        Simple keyword-overlap search — not semantic.  Returns an empty list
        for blank queries.
        """
        query = (query or "").strip()
        if not query:
            return []
        keywords = [w.lower() for w in query.split() if len(w) > 2]
        if not keywords:
            return []
        if not self._conn:
            self._ensure_table()
        cursor = self._conn.execute(
            "SELECT * FROM outcomes WHERE error_summary != '' ORDER BY timestamp DESC"
        )
        scored: list[tuple[int, Outcome]] = []
        for row in cursor:
            summary = (row["error_summary"] or "").lower()
            match_count = sum(1 for kw in keywords if kw in summary)
            if match_count > 0:
                scored.append((match_count, self._row_to_outcome(row)))
        # Sort by descending match count, then return top results.
        scored.sort(key=lambda t: t[0], reverse=True)
        return [outcome for _, outcome in scored[:max_results]]

    def get_repo_outcomes(self, repo: str, *, max_results: int = 5) -> list[Outcome]:
        if not self._conn:
            self._ensure_table()

        # Approximate repo family match via LIKE
        parts = repo.split("-")
        family = (
            "-".join(parts[:-1]) if len(parts) > 1 and parts[-1].isdigit() else repo
        )

        cursor = self._conn.execute(
            """SELECT * FROM outcomes WHERE repo LIKE ? OR repo = ? 
               ORDER BY timestamp DESC LIMIT ?""",
            (f"{family}%", repo, max_results),
        )
        return [self._row_to_outcome(row) for row in cursor]

    def get_successful_patterns(
        self, repo: str, *, max_results: int = 3
    ) -> list[Outcome]:
        if not self._conn:
            self._ensure_table()

        parts = repo.split("-")
        family = (
            "-".join(parts[:-1]) if len(parts) > 1 and parts[-1].isdigit() else repo
        )

        cursor = self._conn.execute(
            """SELECT * FROM outcomes 
               WHERE status = 'PASS' AND (repo LIKE ? OR repo = ?)
               ORDER BY timestamp DESC LIMIT ?""",
            (f"{family}%", repo, max_results),
        )
        return [self._row_to_outcome(row) for row in cursor]

    def get_common_mistakes(self, *, max_results: int = 5) -> list[str]:
        if not self._conn:
            self._ensure_table()

        cursor = self._conn.execute(
            """SELECT error_summary, COUNT(*) as cnt 
               FROM outcomes 
               WHERE status != 'PASS' AND error_summary != ''
               GROUP BY error_summary 
               ORDER BY cnt DESC LIMIT ?""",
            (max_results,),
        )
        return [row["error_summary"] for row in cursor]

    def get_failure_patterns(self, repo: str) -> Counter:
        # Re-implementing logic to use SQL for initial filtering
        outcomes = self.get_repo_outcomes(repo, max_results=20)
        failures = [o for o in outcomes if o.status != "PASS"]

        patterns: Counter = Counter()
        for f in failures:
            cat = self._classify_failure(f)
            patterns[cat] += 1
        return patterns

    @staticmethod
    def _classify_failure(outcome: Outcome) -> str:
        summary = (outcome.error_summary + " " + outcome.error_type).lower()
        if "import" in summary:
            return "import_error"
        if "syntax" in summary:
            return "syntax_error"
        if "not found" in summary or "no such file" in summary:
            return "wrong_file"
        if any(w in summary for w in ("assertion", "assert", "expected")):
            return "incomplete_fix"
        if "timeout" in summary:
            return "timeout"
        if "empty" in summary or "no patch" in summary:
            return "empty_patch"
        return "other"

    def format_learnings(self, repo: str) -> str:
        """Format relevant outcomes as a prompt section."""
        parts = []

        # 1. Successes
        successes = self.get_successful_patterns(repo, max_results=2)
        if successes:
            parts.append("## Successful Patterns from This Repo")
            for s in successes:
                files = ", ".join(s.files_changed[:3]) or "unknown"
                parts.append(f"- **{s.task_id}** (PASS): Modified {files}")
                if s.patch_snippet:
                    parts.append(f"  ```\n  {s.patch_snippet[:200]}\n  ```")

        # 2. Recent Failures
        failures = [
            o for o in self.get_repo_outcomes(repo, max_results=3) if o.status != "PASS"
        ]
        if failures:
            parts.append("\n## Recent Failures from This Repo")
            for f in failures:
                parts.append(f"- **{f.task_id}** ({f.status}): {f.error_summary}")

        # 3. Common Mistakes (Global)
        mistakes = self.get_common_mistakes(max_results=3)
        if mistakes:
            parts.append("\n## Common Mistakes to Avoid")
            for m in mistakes:
                parts.append(f"- {m}")

        return "\n".join(parts) + "\n" if parts else ""
