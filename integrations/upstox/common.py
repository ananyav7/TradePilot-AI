"""Shared retry, cache, validation, and provenance helpers."""

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import math
import random
import time
from threading import RLock
from typing import Any, TypeVar

from integrations.upstox.errors import InvalidResponseError, NetworkError, TokenExpiredError, UpstoxAPIError
from integrations.upstox.rate_limiter import SlidingWindowRateLimiter
from integrations.upstox.token_manager import TokenManager

T = TypeVar("T")


@dataclass(frozen=True)
class MarketDataSnapshot[T]:
    """Response plus retrieval provenance; `retrieved_at` always means fetch time."""

    data: T
    retrieved_at: datetime
    source: str
    data_kind: str
    cache_hit: bool


class TTLCache:
    """Small thread-safe in-memory cache with no disk persistence."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._values: dict[str, tuple[float, datetime, Any]] = {}
        self._lock = RLock()

    def get(self, key: str, ttl_seconds: float) -> tuple[Any, datetime] | None:
        with self._lock:
            value = self._values.get(key)
            if value is None:
                return None
            expiry, retrieved_at, data = value
            if self._clock() >= expiry:
                self._values.pop(key, None)
                return None
            return deepcopy(data), retrieved_at

    def set(self, key: str, data: Any, ttl_seconds: float, retrieved_at: datetime) -> None:
        if ttl_seconds <= 0:
            return
        with self._lock:
            self._values[key] = (self._clock() + ttl_seconds, retrieved_at, deepcopy(data))


def execute_read(
    operation: Callable[[], T],
    *,
    token_manager: TokenManager,
    limiter: SlidingWindowRateLimiter,
    max_attempts: int = 3,
    base_delay: float = 0.25,
    max_delay: float = 4.0,
    sleep: Callable[[float], None] = time.sleep,
    jitter: Callable[[], float] = random.random,
) -> T:
    """Run a safe read with bounded retries for transient network/HTTP failures."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    for attempt in range(max_attempts):
        limiter.acquire()
        try:
            return operation()
        except TokenExpiredError:
            token_manager.invalidate()
            raise
        except UpstoxAPIError as exc:
            if exc.status_code == 401:
                token_manager.invalidate()
                raise TokenExpiredError("Upstox rejected the access token; reauthorize") from exc
            retryable = exc.status_code == 429 or (exc.status_code is not None and 500 <= exc.status_code < 600)
            if not retryable or attempt + 1 >= max_attempts:
                raise
            delay = exc.retry_after_seconds
        except NetworkError:
            if attempt + 1 >= max_attempts:
                raise
            delay = None
        except (TimeoutError, OSError) as exc:
            if attempt + 1 >= max_attempts:
                raise NetworkError("Network request failed") from exc
            delay = None
        delay = delay if delay is not None else min(max_delay, base_delay * (2 ** attempt)) * (0.75 + jitter() * 0.5)
        sleep(max(0.0, min(delay, max_delay)))
    raise AssertionError("unreachable retry loop")


def validate_envelope(response: Any, *, expected_field: str) -> Any:
    """Validate common Upstox `{status, data}` envelopes and endpoint payload shape."""
    if hasattr(response, "to_dict"):
        response = response.to_dict()
    if isinstance(response, tuple):
        response = response[0]
        if hasattr(response, "to_dict"):
            response = response.to_dict()
    if not isinstance(response, dict):
        raise InvalidResponseError("Upstox response must be a JSON object")
    if response.get("status") != "success":
        raise InvalidResponseError("Upstox response status was not success")
    data = response.get("data")
    if not isinstance(data, dict) or expected_field not in data:
        raise InvalidResponseError(f"Upstox response is missing data.{expected_field}")
    return data[expected_field]


def finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise InvalidResponseError(f"Upstox response field {field} must be a finite number")
    return float(value)


def utc_retrieved_at() -> datetime:
    return datetime.now(timezone.utc)
