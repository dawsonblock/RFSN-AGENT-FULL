"""Collect system and agent health signals.

Tracks CPU, memory, error rate, patch success rate, and other
numeric signals for trend analysis.
"""

import os
import time
from collections import deque
from typing import Any, Dict, List, Optional


class SignalCollector:
    """Collect and query time-series health signals."""

    def __init__(self, window_size: int = 100):
        self._window_size = window_size
        self._series: Dict[str, deque] = {}
        self._counts: Dict[str, int] = {}

    def record(self, name: str, value: float, ts: Optional[float] = None) -> None:
        """Record a signal sample."""
        ts = ts or time.time()
        if name not in self._series:
            self._series[name] = deque(maxlen=self._window_size)
            self._counts[name] = 0
        self._series[name].append((ts, value))
        self._counts[name] += 1

    def latest(self, name: str) -> Optional[float]:
        """Get the latest value of a signal."""
        if name not in self._series or not self._series[name]:
            return None
        return self._series[name][-1][1]

    def mean(self, name: str, n: Optional[int] = None) -> Optional[float]:
        """Compute the mean of the last N samples."""
        if name not in self._series or not self._series[name]:
            return None
        vals = list(self._series[name])
        if n is not None:
            vals = vals[-n:]
        return sum(v for _, v in vals) / len(vals)

    def trend(self, name: str, n: int = 10) -> Optional[float]:
        """Compute a simple linear trend (slope) over last N samples.

        Returns positive if increasing, negative if decreasing.
        """
        if name not in self._series or len(self._series[name]) < 2:
            return None
        vals = list(self._series[name])[-n:]
        if len(vals) < 2:
            return 0.0
        # Simple slope: (last - first) / time_span
        t0, v0 = vals[0]
        t1, v1 = vals[-1]
        dt = t1 - t0
        if dt == 0:
            return 0.0
        return (v1 - v0) / dt

    def all_signals(self) -> Dict[str, Dict[str, Any]]:
        """Snapshot of all signals with latest value and trend."""
        result = {}
        for name in self._series:
            result[name] = {
                "latest": self.latest(name),
                "mean": self.mean(name),
                "trend": self.trend(name),
                "count": self._counts.get(name, 0),
            }
        return result

    def names(self) -> List[str]:
        return list(self._series.keys())


# -- Convenience functions for common signals --


def collect_system_signals(collector: SignalCollector) -> None:
    """Collect CPU and memory signals (best-effort)."""
    try:
        import psutil  # type: ignore[import-untyped]

        collector.record("cpu_percent", psutil.cpu_percent())
        mem = psutil.virtual_memory()
        collector.record("memory_percent", mem.percent)
        collector.record("memory_available_mb", mem.available / (1024 * 1024))
    except ImportError:
        # psutil not available — collect from /proc if Linux
        try:
            with open("/proc/loadavg") as f:
                load1 = float(f.read().split()[0])
                collector.record("load_avg_1m", load1)
        except Exception:
            pass


def collect_agent_signals(
    collector: SignalCollector,
    *,
    patches_attempted: int = 0,
    patches_succeeded: int = 0,
    errors: int = 0,
    run_duration_s: float = 0.0,
) -> None:
    """Record agent-level signals."""
    if patches_attempted > 0:
        rate = patches_succeeded / patches_attempted
        collector.record("patch_success_rate", rate)
    collector.record("error_count", float(errors))
    if run_duration_s > 0:
        collector.record("run_duration_s", run_duration_s)
