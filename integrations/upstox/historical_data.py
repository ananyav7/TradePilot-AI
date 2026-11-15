"""Historical and current-day intraday candle clients for Upstox V3."""

from datetime import date, datetime
import json

from integrations.upstox.common import MarketDataSnapshot, TTLCache, execute_read, utc_retrieved_at, validate_envelope, finite_number
from integrations.upstox.errors import InvalidResponseError
from integrations.upstox.rate_limiter import SlidingWindowRateLimiter
from integrations.upstox.token_manager import TokenManager


def _validate_instrument_key(instrument_key: str) -> None:
    if not instrument_key or not isinstance(instrument_key, str) or "|" not in instrument_key:
        raise ValueError("instrument_key must be a key from the Upstox instrument master")


def _validate_interval(unit: str, interval: int, *, intraday: bool) -> None:
    valid_units = {"minutes": range(1, 301), "hours": range(1, 6), "days": range(1, 2)}
    if not intraday:
        valid_units.update({"weeks": range(1, 2), "months": range(1, 2)})
    if unit not in valid_units or isinstance(interval, bool) or interval not in valid_units[unit]:
        raise ValueError(f"Invalid unit/interval for Upstox V3 candles: {unit}/{interval}")


def _parse_candles(response: object) -> list[list[object]]:
    rows = validate_envelope(response, expected_field="candles")
    if not isinstance(rows, list):
        raise InvalidResponseError("Upstox data.candles must be an array")
    validated: list[list[object]] = []
    for idx, candle in enumerate(rows):
        if not isinstance(candle, list) or len(candle) not in (6, 7):
            raise InvalidResponseError(f"Candle row {idx} must contain 6 or 7 documented values")
        if not isinstance(candle[0], str):
            raise InvalidResponseError(f"Candle row {idx} timestamp must be an ISO-8601 string")
        try:
            timestamp = datetime.fromisoformat(candle[0].replace("Z", "+00:00"))
        except ValueError as exc:
            raise InvalidResponseError(f"Candle row {idx} has invalid timestamp") from exc
        if timestamp.tzinfo is None:
            raise InvalidResponseError(f"Candle row {idx} timestamp must include an offset")
        for position, field in zip(range(1, 5), ("open", "high", "low", "close")):
            finite_number(candle[position], f"candles[{idx}].{field}")
        if isinstance(candle[5], bool) or not isinstance(candle[5], (int, float)) or candle[5] < 0:
            raise InvalidResponseError(f"Candle row {idx} volume must be nonnegative numeric")
        if len(candle) == 7 and (isinstance(candle[6], bool) or not isinstance(candle[6], (int, float))):
            raise InvalidResponseError(f"Candle row {idx} open interest must be numeric")
        validated.append(candle)
    return validated


class HistoricalCandleClient:
    """Fetches V3 historical and current-day intraday candles with TTL caching."""

    def __init__(self, adapter, token_manager: TokenManager, *, limiter: SlidingWindowRateLimiter | None = None,
                 cache: TTLCache | None = None, max_retries: int = 3,
                 historical_cache_seconds: float = 21600, intraday_cache_seconds: float = 30) -> None:
        self.adapter = adapter
        self.tokens = token_manager
        self.limiter = limiter or SlidingWindowRateLimiter()
        self.cache = cache or TTLCache()
        self.max_retries = max_retries
        self.historical_cache_seconds = historical_cache_seconds
        self.intraday_cache_seconds = intraday_cache_seconds

    def historical(self, instrument_key: str, unit: str, interval: int, to_date: date | str,
                   from_date: date | str | None = None) -> MarketDataSnapshot[list[list[object]]]:
        """Call V3 historical-candle path with the documented date parameters."""
        _validate_instrument_key(instrument_key)
        _validate_interval(unit, interval, intraday=False)
        to_text = to_date.isoformat() if isinstance(to_date, date) else to_date
        from_text = from_date.isoformat() if isinstance(from_date, date) else from_date
        end = date.fromisoformat(to_text)
        if from_text is not None and date.fromisoformat(from_text) > end:
            raise ValueError("from_date must be on or before to_date")
        token = self.tokens.get_token()
        key = "historical:" + json.dumps([instrument_key, unit, interval, to_text, from_text])
        cached = self.cache.get(key, self.historical_cache_seconds)
        if cached:
            return MarketDataSnapshot(cached[0], cached[1], "Upstox Historical Candle Data V3", "HISTORICAL", True)
        response = execute_read(
            lambda: self.adapter.historical_candles(token, instrument_key, unit, interval, to_text, from_text),
            token_manager=self.tokens, limiter=self.limiter, max_attempts=self.max_retries,
        )
        candles = _parse_candles(response)
        retrieved = utc_retrieved_at()
        self.cache.set(key, candles, self.historical_cache_seconds, retrieved)
        return MarketDataSnapshot(candles, retrieved, "Upstox Historical Candle Data V3", "HISTORICAL", False)

    def intraday(self, instrument_key: str, unit: str = "minutes", interval: int = 1) -> MarketDataSnapshot[list[list[object]]]:
        """Fetch V3 current-trading-day candles (the endpoint has no date path parameters)."""
        _validate_instrument_key(instrument_key)
        _validate_interval(unit, interval, intraday=True)
        token = self.tokens.get_token()
        key = "intraday:" + json.dumps([instrument_key, unit, interval])
        cached = self.cache.get(key, self.intraday_cache_seconds)
        if cached:
            return MarketDataSnapshot(cached[0], cached[1], "Upstox Intraday Candle Data V3", "LIVE_INTRADAY", True)
        response = execute_read(
            lambda: self.adapter.intraday_candles(token, instrument_key, unit, interval),
            token_manager=self.tokens, limiter=self.limiter, max_attempts=self.max_retries,
        )
        candles = _parse_candles(response)
        retrieved = utc_retrieved_at()
        self.cache.set(key, candles, self.intraday_cache_seconds, retrieved)
        return MarketDataSnapshot(candles, retrieved, "Upstox Intraday Candle Data V3", "LIVE_INTRADAY", False)
