"""Circuit Breaker for fault tolerance.

Prevents cascading failures by stopping requests to a failing service
for a period of time.

States:
- CLOSED: Requests go through. Fails count towards threshold.
- OPEN: Requests fail immediately. Waits for recovery_timeout.
- HALF-OPEN: Single probe request allowed. Success -> CLOSED, Fail -> OPEN.
"""

import time
import threading
from typing import Callable, Any, Type
from enum import Enum


class State(Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreakerOpen(Exception):
    """Raised when the circuit is open."""

    pass


class CircuitBreaker:
    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        exclude_exceptions: tuple[Type[Exception], ...] = (),
    ):
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._exclude_exceptions = exclude_exceptions

        self._state = State.CLOSED
        self._failures = 0
        self._last_failure_time = 0.0
        self._lock = threading.RLock()

    @property
    def state(self) -> str:
        return self._state.value

    def call(self, func: Callable, *args, **kwargs) -> Any:
        """Call the function with circuit breaker protection."""
        with self._lock:
            if self._state == State.OPEN:
                if time.time() - self._last_failure_time > self._recovery_timeout:
                    self._state = State.HALF_OPEN
                else:
                    raise CircuitBreakerOpen(f"Circuit Open (fails={self._failures})")

            # In HALF_OPEN, we allow 1 request (the current one)
            # If concurrent requests come, they might fail or pass depending on lock
            # but for this simple implementation, we just proceed.

        try:
            result = func(*args, **kwargs)
        except Exception as e:
            if isinstance(e, self._exclude_exceptions):
                raise e
            self._handle_failure()
            raise e

        self._handle_success()
        return result

    def _handle_failure(self):
        with self._lock:
            self._failures += 1
            self._last_failure_time = time.time()
            if (
                self._state == State.HALF_OPEN
                or self._failures >= self._failure_threshold
            ):
                self._state = State.OPEN

    def _handle_success(self):
        with self._lock:
            if self._state == State.HALF_OPEN:
                self._state = State.CLOSED
                self._failures = 0
            elif self._state == State.CLOSED:
                self._failures = 0
