"""Corrective actions based on stability predictions.

Throttles, pauses, or restarts the system based on predictor output.
"""

import os
import time
from typing import Any, Dict, Optional

from stability.predictor import StabilityPredictor


class Corrector:
    """Apply corrective actions based on stability predictions."""

    def __init__(
        self,
        predictor: StabilityPredictor,
        cooldown_s: float = 30.0,
    ):
        self.predictor = predictor
        self.cooldown_s = cooldown_s
        self._last_action_time: float = 0
        self._actions_taken: list = []

    def evaluate_and_correct(self) -> Dict[str, Any]:
        """Evaluate stability and take corrective action if needed.

        Returns:
            dict with keys: action, reason, prediction
        """
        prediction = self.predictor.predict()

        # Check cooldown
        now = time.time()
        if now - self._last_action_time < self.cooldown_s:
            return {
                "action": "cooldown",
                "reason": f"Last action was {now - self._last_action_time:.0f}s ago",
                "prediction": prediction,
            }

        if not prediction["stable"]:
            action = self._handle_critical(prediction)
        elif prediction["warnings"]:
            action = self._handle_warning(prediction)
        else:
            return {
                "action": "none",
                "reason": "system stable",
                "prediction": prediction,
            }

        self._last_action_time = now
        self._actions_taken.append(action)
        return action

    def _handle_critical(self, prediction: Dict[str, Any]) -> Dict[str, Any]:
        """Handle critical instability."""
        criticals = prediction.get("criticals", [])

        # Check for OOM
        for c in criticals:
            if "memory_percent" in c:
                return {
                    "action": "reduce_workload",
                    "reason": f"Critical memory: {c}",
                    "prediction": prediction,
                    "applied": self._apply_reduce_workload(),
                }

        # Check for high error rate
        for c in criticals:
            if "error_count" in c:
                return {
                    "action": "pause",
                    "reason": f"Critical error rate: {c}",
                    "prediction": prediction,
                    "applied": self._apply_pause(),
                }

        # Generic critical
        return {
            "action": "throttle",
            "reason": f"Critical: {criticals}",
            "prediction": prediction,
            "applied": self._apply_throttle(),
        }

    def _handle_warning(self, prediction: Dict[str, Any]) -> Dict[str, Any]:
        """Handle warning-level instability."""
        return {
            "action": "throttle",
            "reason": f"Warnings: {prediction['warnings']}",
            "prediction": prediction,
            "applied": self._apply_throttle(),
        }

    def _apply_throttle(self) -> bool:
        """Reduce processing speed."""
        current = int(os.getenv("RFSN_PARALLEL_WORKERS", "4"))
        if current > 1:
            os.environ["RFSN_PARALLEL_WORKERS"] = str(max(1, current - 1))
        return True

    def _apply_pause(self) -> bool:
        """Signal pause by setting env flag."""
        os.environ["RFSN_PAUSED"] = "1"
        return True

    def _apply_reduce_workload(self) -> bool:
        """Reduce batch sizes."""
        current = int(os.getenv("RFSN_BATCH_SIZE", "10"))
        os.environ["RFSN_BATCH_SIZE"] = str(max(1, current // 2))
        return True

    def history(self) -> list:
        return list(self._actions_taken)
