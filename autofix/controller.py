"""Autofix event loop: ingest → analyze → act → verify → learn.

Orchestrates the full self-healing pipeline.
"""

import json
import os
import time
from typing import Any, Dict, List, Optional

from autofix.ingest import ingest
from autofix.analyze import classify_batch, prioritize, summarize
from autofix.actions import plan_actions
from autofix.apply import apply_actions
from autofix.verify import verify_all


class AutofixController:
    """Orchestrates the autofix pipeline."""

    def __init__(
        self,
        log_dir: str = "/data/logs",
        history_path: str = "/data/autofix_history.jsonl",
        dry_run: bool = False,
        max_auto_actions: int = 5,
    ):
        self.log_dir = log_dir
        self.history_path = history_path
        self.dry_run = dry_run
        self.max_auto_actions = max_auto_actions
        self._history: List[Dict[str, Any]] = []

    def run_cycle(
        self,
        log_paths: Optional[List[str]] = None,
        raw_text: str = "",
    ) -> Dict[str, Any]:
        """Execute one full autofix cycle.

        Returns:
            Report dict with ingest/classify/action/verify results.
        """
        ts = time.time()

        # 1. Ingest
        if log_paths is None:
            log_paths = self._discover_logs()
        failures = ingest(log_paths=log_paths, raw_text=raw_text)

        if not failures:
            return {"cycle": "no_failures", "timestamp": ts}

        # 2. Analyze
        classified = classify_batch(failures)
        classified = prioritize(classified)
        summary = summarize(classified)

        # 3. Plan actions (only auto-fixable, up to max)
        auto_fixable = [f for f in classified if f.get("auto_fixable")]
        actions = plan_actions(auto_fixable[: self.max_auto_actions])

        # 4. Apply
        results = apply_actions(actions, dry_run=self.dry_run)

        # 5. Verify
        verification = verify_all(results)

        # 6. Record
        report = {
            "timestamp": ts,
            "summary": summary,
            "actions_planned": len(actions),
            "actions_applied": sum(1 for r in results if r.get("applied")),
            "verification": verification,
            "dry_run": self.dry_run,
        }
        self._record(report)

        return report

    def _discover_logs(self) -> List[str]:
        """Find log files in the log directory."""
        if not os.path.isdir(self.log_dir):
            return []
        logs = []
        for f in os.listdir(self.log_dir):
            if f.endswith((".log", ".jsonl", ".txt")):
                logs.append(os.path.join(self.log_dir, f))
        return sorted(logs)

    def _record(self, report: Dict[str, Any]) -> None:
        """Append report to history."""
        self._history.append(report)
        try:
            os.makedirs(os.path.dirname(self.history_path) or ".", exist_ok=True)
            with open(self.history_path, "a") as f:
                f.write(json.dumps(report) + "\n")
        except Exception:
            pass

    def history(self) -> List[Dict[str, Any]]:
        return list(self._history)


def run_once(
    log_paths: Optional[List[str]] = None,
    raw_text: str = "",
    dry_run: bool = True,
) -> Dict[str, Any]:
    """Convenience function: run one autofix cycle."""
    controller = AutofixController(dry_run=dry_run)
    return controller.run_cycle(log_paths=log_paths, raw_text=raw_text)


if __name__ == "__main__":
    import sys

    paths = sys.argv[1:] if len(sys.argv) > 1 else None
    result = run_once(log_paths=paths, dry_run=True)
    print(json.dumps(result, indent=2, default=str))
