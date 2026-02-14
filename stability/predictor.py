"""Exponential smoothing predictor for stability signals.

Takes signal time-series and predicts whether the system is trending
toward instability (rising error rate, dropping success rate, etc).
"""

from typing import Any, Dict, List, Optional, Tuple


class ExponentialSmoother:
    """Single exponential smoothing for a time series."""

    def __init__(self, alpha: float = 0.3):
        self.alpha = alpha
        self._smoothed: Optional[float] = None

    def update(self, value: float) -> float:
        """Update with a new observation and return the smoothed value."""
        if self._smoothed is None:
            self._smoothed = value
        else:
            self._smoothed = self.alpha * value + (1 - self.alpha) * self._smoothed
        return self._smoothed

    @property
    def value(self) -> Optional[float]:
        return self._smoothed

    def reset(self) -> None:
        self._smoothed = None


class StabilityPredictor:
    """Predict system instability from multiple signals.

    Maintains exponential smoothers for each signal and evaluates
    whether the system is trending toward instability.
    """

    def __init__(
        self,
        alpha: float = 0.3,
        thresholds: Optional[Dict[str, Dict[str, float]]] = None,
    ):
        self.alpha = alpha
        self._smoothers: Dict[str, ExponentialSmoother] = {}
        # Default thresholds: {signal_name: {warn: float, critical: float, direction: 1|-1}}
        # direction=1 means high values are bad, -1 means low values are bad
        self._thresholds = thresholds or {
            "error_count": {"warn": 3.0, "critical": 10.0, "direction": 1},
            "cpu_percent": {"warn": 80.0, "critical": 95.0, "direction": 1},
            "memory_percent": {"warn": 85.0, "critical": 95.0, "direction": 1},
            "patch_success_rate": {"warn": 0.3, "critical": 0.1, "direction": -1},
        }

    def update(self, signal_name: str, value: float) -> float:
        """Update a signal and return the smoothed value."""
        if signal_name not in self._smoothers:
            self._smoothers[signal_name] = ExponentialSmoother(self.alpha)
        return self._smoothers[signal_name].update(value)

    def update_batch(self, signals: Dict[str, float]) -> Dict[str, float]:
        """Update multiple signals at once."""
        return {k: self.update(k, v) for k, v in signals.items()}

    def predict(self) -> Dict[str, Any]:
        """Evaluate current stability.

        Returns:
            dict with keys: stable, warnings, criticals, signals
        """
        warnings = []
        criticals = []
        signal_status = {}

        for name, smoother in self._smoothers.items():
            val = smoother.value
            if val is None:
                continue

            thresh = self._thresholds.get(name)
            status = "ok"

            if thresh:
                direction = thresh.get("direction", 1)
                warn_val = thresh.get("warn", float("inf"))
                crit_val = thresh.get("critical", float("inf"))

                if direction == 1:
                    # High = bad
                    if val >= crit_val:
                        status = "critical"
                        criticals.append(f"{name}={val:.2f} (>={crit_val})")
                    elif val >= warn_val:
                        status = "warn"
                        warnings.append(f"{name}={val:.2f} (>={warn_val})")
                else:
                    # Low = bad
                    if val <= crit_val:
                        status = "critical"
                        criticals.append(f"{name}={val:.2f} (<={crit_val})")
                    elif val <= warn_val:
                        status = "warn"
                        warnings.append(f"{name}={val:.2f} (<={warn_val})")

            signal_status[name] = {
                "smoothed": round(val, 4),
                "status": status,
            }

        return {
            "stable": len(criticals) == 0,
            "warnings": warnings,
            "criticals": criticals,
            "signals": signal_status,
        }

    def is_stable(self) -> bool:
        """Quick check: is the system currently stable?"""
        return self.predict()["stable"]
