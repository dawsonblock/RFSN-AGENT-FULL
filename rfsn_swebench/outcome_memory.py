"""Outcome memory — persistent learning from every repair attempt.

Stores task outcomes in a lightweight JSONL file so that subsequent
tasks can learn from prior successes and failures.  Two retrieval
modes:

1. **Repo-family outcomes** — outcomes from tasks in the same repo
   (e.g. all ``django__django-*`` tasks share learnings).
2. **Common mistakes** — the most frequent failure patterns across
   all tasks, surfaced as "things to avoid".

Usage::

    mem = OutcomeMemory("data/results/outcome_memory.jsonl")
    mem.record(task_id="flask-4045", status="FAIL",
               repo="flask__flask", error_type="test_fail",
               error_summary="Dot check in wrong method",
               files_changed=["src/flask/blueprints.py"],
               patch_snippet="...")
    prompt_section = mem.format_learnings("flask__flask")
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Optional  # noqa: F401 — used by type annotations


@dataclass
class Outcome:
    """A single task attempt outcome."""

    task_id: str
    status: str  # PASS, FAIL, ABORT, GATE_REJECT
    repo: str  # e.g. "django__django"
    error_type: str = ""  # test_fail, gate_reject, apply_fail, empty_patch
    error_summary: str = ""  # one-line description of what went wrong
    files_changed: list[str] = field(default_factory=list)
    patch_snippet: str = ""  # first 500 chars of the patch
    iters_used: int = 0
    dense_reward: float = 0.0  # partial progress (0.0-1.0)
    timestamp: float = 0.0

    def __post_init__(self) -> None:
        # Precompute cached fields.
        self._repo_family: str = self._compute_repo_family()
        self._search_words: set[str] | None = None

    def _compute_repo_family(self) -> str:
        parts = self.repo.split("-")
        if len(parts) > 1 and parts[-1].isdigit():
            return "-".join(parts[:-1])
        return self.repo

    @property
    def repo_family(self) -> str:
        return self._repo_family

    @property
    def search_words(self) -> set[str]:
        """Lazily cached word set for similarity search."""
        if self._search_words is None:
            self._search_words = set(
                f"{self.task_id} {self.error_summary} {self.error_type}".lower().split()
            )
        return self._search_words


class OutcomeMemory:
    """JSONL-backed outcome store with retrieval for LLM prompting."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._outcomes: list[Outcome] = []
        self._loaded = False

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not os.path.isfile(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        self._outcomes.append(Outcome(**d))
                    except (json.JSONDecodeError, TypeError):
                        continue  # skip malformed lines
        except Exception:
            pass  # file inaccessible — start fresh

    def record(
        self,
        task_id: str,
        status: str,
        repo: str,
        *,
        error_type: str = "",
        error_summary: str = "",
        files_changed: list[str] | None = None,
        patch_snippet: str = "",
        iters_used: int = 0,
        dense_reward: float = 0.0,
    ) -> None:
        """Append an outcome to the store."""
        self._ensure_loaded()
        outcome = Outcome(
            task_id=task_id,
            status=status,
            repo=repo,
            error_type=error_type,
            error_summary=error_summary,
            files_changed=files_changed or [],
            patch_snippet=patch_snippet[:500],
            iters_used=iters_used,
            dense_reward=max(0.0, min(1.0, dense_reward)),
            timestamp=time.time(),
        )
        self._outcomes.append(outcome)

        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(outcome), ensure_ascii=False) + "\n")

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    def _repo_family(self, repo: str) -> str:
        """Extract the repo family (e.g. 'django__django' from
        'django__django-11049')."""
        # Repo is already the family in most cases, but normalize
        parts = repo.split("-")
        if len(parts) > 1 and parts[-1].isdigit():
            return "-".join(parts[:-1])
        return repo

    def get_repo_outcomes(self, repo: str, *, max_results: int = 5) -> list[Outcome]:
        """Get recent outcomes from the same repo family."""
        self._ensure_loaded()
        # Build family once for the query repo.
        parts = repo.split("-")
        family = (
            "-".join(parts[:-1]) if len(parts) > 1 and parts[-1].isdigit() else repo
        )
        matches = [o for o in self._outcomes if o.repo_family == family]
        # Most recent first
        matches.sort(key=lambda o: o.timestamp, reverse=True)
        return matches[:max_results]

    def get_common_mistakes(self, *, max_results: int = 5) -> list[str]:
        """Return the most common error summaries across all failures."""
        self._ensure_loaded()
        errors = [
            o.error_summary
            for o in self._outcomes
            if o.status != "PASS" and o.error_summary.strip()
        ]
        if not errors:
            return []
        counter = Counter(errors)
        return [msg for msg, _ in counter.most_common(max_results)]

    def get_successful_patterns(
        self, repo: str, *, max_results: int = 3
    ) -> list[Outcome]:
        """Get successful outcomes from the same repo family."""
        self._ensure_loaded()
        parts = repo.split("-")
        family = (
            "-".join(parts[:-1]) if len(parts) > 1 and parts[-1].isdigit() else repo
        )
        successes = [
            o for o in self._outcomes if o.status == "PASS" and o.repo_family == family
        ]
        successes.sort(key=lambda o: o.timestamp, reverse=True)
        return successes[:max_results]

    def get_similar_tasks(
        self,
        task_description: str,
        *,
        max_results: int = 5,
    ) -> list[Outcome]:
        """Find past tasks with similar error patterns using keyword overlap."""
        self._ensure_loaded()
        if not task_description.strip():
            return []

        query_words = set(task_description.lower().split())
        scored: list[tuple[float, Outcome]] = []

        for o in self._outcomes:
            # Use cached search_words.
            overlap = len(query_words & o.search_words)
            if overlap == 0:
                continue
            score = overlap / max(len(query_words), 1)
            # Boost outcomes with dense_reward data.
            if o.dense_reward > 0:
                score += 0.1 * o.dense_reward
            scored.append((score, o))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [o for _, o in scored[:max_results]]

    # ------------------------------------------------------------------
    # Prompt formatting
    # ------------------------------------------------------------------

    def get_failure_patterns(
        self,
        repo: str,
        *,
        max_results: int = 5,
    ) -> Counter:
        """Classify and count failure patterns for a repo family.

        Returns a Counter mapping failure categories → counts.
        """
        self._ensure_loaded()
        fam = self._repo_family(repo)
        failures = [
            o for o in self._outcomes if o.repo_family == fam and o.status != "PASS"
        ]
        patterns: Counter = Counter()
        for f in failures[-20:]:  # last 20 for recency
            cat = self._classify_failure(f)
            patterns[cat] += 1
        return patterns

    @staticmethod
    def _classify_failure(outcome: "Outcome") -> str:
        """Classify a failure into an actionable bucket."""
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
        if any(w in summary for w in ("attribute", "type", "name")):
            return "type_error"
        return "other"

    def format_learnings(self, repo: str) -> str:
        """Format relevant outcomes as a prompt section for the LLM.

        Returns an empty string if no relevant learnings exist.
        """
        self._ensure_loaded()
        parts: list[str] = []

        # Successes from same repo
        successes = self.get_successful_patterns(repo, max_results=2)
        if successes:
            parts.append("## Successful Patterns from This Repo")
            for s in successes:
                files_str = ", ".join(s.files_changed[:3]) or "unknown"
                parts.append(
                    f"- **{s.task_id}** (PASS in {s.iters_used} iters): "
                    f"Modified {files_str}"
                )
                if s.patch_snippet:
                    parts.append(f"  ```\n  {s.patch_snippet[:200]}\n  ```")

        # Failures from same repo
        repo_outcomes = self.get_repo_outcomes(repo, max_results=3)
        failures = [o for o in repo_outcomes if o.status != "PASS"]
        if failures:
            parts.append("\n## Recent Failures from This Repo (learn from these)")
            for f in failures:
                parts.append(f"- **{f.task_id}** ({f.status}): {f.error_summary}")

        # Failure pattern breakdown (shows WHERE things go wrong)
        patterns = self.get_failure_patterns(repo)
        if patterns:
            parts.append("\n## Failure Pattern Analysis")
            parts.append(
                "The following shows how past fixes failed — "
                "avoid repeating these mistakes:"
            )
            for cat, count in patterns.most_common(5):
                label = cat.replace("_", " ").title()
                parts.append(f"- {label}: {count} occurrence(s)")

        # Common mistakes across all repos
        mistakes = self.get_common_mistakes(max_results=3)
        if mistakes:
            parts.append("\n## Common Mistakes to Avoid")
            for m in mistakes:
                parts.append(f"- {m}")

        if not parts:
            return ""

        return "\n".join(parts) + "\n"

    @property
    def total_outcomes(self) -> int:
        self._ensure_loaded()
        return len(self._outcomes)

    @property
    def pass_rate(self) -> float:
        self._ensure_loaded()
        if not self._outcomes:
            return 0.0
        passes = sum(1 for o in self._outcomes if o.status == "PASS")
        return passes / len(self._outcomes)
