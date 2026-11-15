"""Deterministic mock adapter for Upstox read-only endpoints and token exchange."""

from collections import defaultdict, deque
from typing import Any


class MockUpstoxAdapter:
    """Queue mock results/exceptions and retain calls for request-schema assertions."""

    def __init__(self, **responses: Any) -> None:
        self.responses: dict[str, deque[Any]] = defaultdict(deque)
        for name, response in responses.items():
            if isinstance(response, (list, tuple)):
                self.responses[name].extend(response)
            else:
                self.responses[name].append(response)
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def _take(self, name: str, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((name, args, kwargs))
        if not self.responses[name]:
            raise AssertionError(f"No mock response queued for {name}")
        value = self.responses[name].popleft()
        if isinstance(value, Exception):
            raise value
        return value

    def historical_candles(self, token: str, instrument_key: str, unit: str, interval: int,
                           to_date: str, from_date: str | None) -> Any:
        return self._take("historical_candles", token, instrument_key, unit, interval, to_date, from_date)

    def intraday_candles(self, token: str, instrument_key: str, unit: str, interval: int) -> Any:
        return self._take("intraday_candles", token, instrument_key, unit, interval)

    def market_quotes(self, token: str, instrument_keys: list[str]) -> Any:
        return self._take("market_quotes", token, instrument_keys)

    def exchange_code(self, **kwargs: Any) -> Any:
        return self._take("exchange_code", **kwargs)

    def download_instruments(self, url: str, timeout: float) -> bytes:
        return self._take("download_instruments", url=url, timeout=timeout)
