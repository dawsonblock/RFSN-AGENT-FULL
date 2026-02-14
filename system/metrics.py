"""Prometheus-style counters and gauges for system metrics.

Lightweight metrics collection without requiring the prometheus_client
library. Exports to JSON or log format.
"""

import json
import os
import time
import threading
from typing import Any, Dict, Optional


class Counter:
    """A monotonically increasing counter."""

    def __init__(self, name: str, description: str = ""):
        self.name = name
        self.description = description
        self._value: float = 0
        self._lock = threading.Lock()

    def inc(self, amount: float = 1.0) -> None:
        with self._lock:
            self._value += amount

    @property
    def value(self) -> float:
        return self._value

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "type": "counter", "value": self._value}


class Gauge:
    """A value that can go up and down."""

    def __init__(self, name: str, description: str = ""):
        self.name = name
        self.description = description
        self._value: float = 0
        self._lock = threading.Lock()

    def set(self, value: float) -> None:
        with self._lock:
            self._value = value

    def inc(self, amount: float = 1.0) -> None:
        with self._lock:
            self._value += amount

    def dec(self, amount: float = 1.0) -> None:
        with self._lock:
            self._value -= amount

    @property
    def value(self) -> float:
        return self._value

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "type": "gauge", "value": self._value}


class MetricsRegistry:
    """Central registry for all metrics."""

    def __init__(self):
        self._metrics: Dict[str, Any] = {}
        self._lock = threading.Lock()

    def counter(self, name: str, description: str = "") -> Counter:
        with self._lock:
            if name not in self._metrics:
                self._metrics[name] = Counter(name, description)
            return self._metrics[name]

    def gauge(self, name: str, description: str = "") -> Gauge:
        with self._lock:
            if name not in self._metrics:
                self._metrics[name] = Gauge(name, description)
            return self._metrics[name]

    def snapshot(self) -> Dict[str, Any]:
        """Return a snapshot of all metrics."""
        with self._lock:
            return {
                "timestamp": time.time(),
                "metrics": {name: m.to_dict() for name, m in self._metrics.items()},
            }

    def export_json(self, path: Optional[str] = None) -> str:
        """Export metrics as JSON string, optionally to a file."""
        data = self.snapshot()
        text = json.dumps(data, indent=2)
        if path:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w") as f:
                f.write(text)
        return text

    def export_prometheus(self) -> str:
        """Export in Prometheus text format."""
        lines = []
        with self._lock:
            for name, m in sorted(self._metrics.items()):
                mtype = "counter" if isinstance(m, Counter) else "gauge"
                if m.description:
                    lines.append(f"# HELP {name} {m.description}")
                lines.append(f"# TYPE {name} {mtype}")
                lines.append(f"{name} {m.value}")
        return "\n".join(lines) + "\n"


# -- Global registry instance --
_global_registry = MetricsRegistry()


def get_registry() -> MetricsRegistry:
    return _global_registry


# Convenience shortcuts
def counter(name: str, description: str = "") -> Counter:
    return _global_registry.counter(name, description)


def gauge(name: str, description: str = "") -> Gauge:
    return _global_registry.gauge(name, description)
