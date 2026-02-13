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
    timestamp: float = 0.0


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
        family = self._repo_family(repo)
        matches = [o for o in self._outcomes if self._repo_family(o.repo) == family]
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
        family = self._repo_family(repo)
        successes = [
            o
            for o in self._outcomes
            if o.status == "PASS" and self._repo_family(o.repo) == family
        ]
        successes.sort(key=lambda o: o.timestamp, reverse=True)
        return successes[:max_results]

    # ------------------------------------------------------------------
    # Prompt formatting
    # ------------------------------------------------------------------

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
