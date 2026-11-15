"""Thread-safe sliding-window rate limiter matching Upstox standard API caps."""

from collections import deque
from threading import Lock
import time
from collections.abc import Callable


class SlidingWindowRateLimiter:
    """Limit standard read APIs to 50/s, 500/minute, 2000/30 minutes."""

    def __init__(
        self,
        limits: tuple[tuple[int, float], ...] = ((50, 1.0), (500, 60.0), (2000, 1800.0)),
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not limits or any(capacity < 1 or window <= 0 for capacity, window in limits):
            raise ValueError("Rate limits require positive capacities and window durations")
        self._limits = limits
        self._events = [deque[float]() for _ in limits]
        self._clock = clock
        self._sleep = sleep
        self._lock = Lock()

    def acquire(self) -> None:
        """Wait until a request fits within every configured sliding window."""
        while True:
            with self._lock:
                now = self._clock()
                waits: list[float] = []
                for events, (capacity, window) in zip(self._events, self._limits):
                    while events and now - events[0] >= window:
                        events.popleft()
                    if len(events) >= capacity:
                        waits.append(max(0.001, window - (now - events[0])))
                if not waits:
                    for events in self._events:
                        events.append(now)
                    return
                delay = max(waits)
            self._sleep(delay)
