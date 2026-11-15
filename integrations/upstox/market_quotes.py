"""Full market quote V3 client with validation and short-lived cache."""

import json

from integrations.upstox.common import MarketDataSnapshot, TTLCache, execute_read, finite_number, utc_retrieved_at
from integrations.upstox.errors import InvalidResponseError
from integrations.upstox.rate_limiter import SlidingWindowRateLimiter
from integrations.upstox.token_manager import TokenManager


class MarketQuoteClient:
    """Fetch full quote snapshots only; this module exposes no order methods."""

    def __init__(self, adapter, token_manager: TokenManager, *, limiter: SlidingWindowRateLimiter | None = None,
                 cache: TTLCache | None = None, max_retries: int = 3, cache_seconds: float = 5) -> None:
        self.adapter = adapter
        self.tokens = token_manager
        self.limiter = limiter or SlidingWindowRateLimiter()
        self.cache = cache or TTLCache()
        self.max_retries = max_retries
        self.cache_seconds = cache_seconds

    def full_quotes(self, instrument_keys: list[str] | tuple[str, ...]) -> MarketDataSnapshot[dict[str, dict]]:
        """Fetch up to 500 full quote snapshots using comma-separated instrument keys."""
        keys = list(dict.fromkeys(instrument_keys))
        if not keys or len(keys) > 500:
            raise ValueError("Provide between 1 and 500 instrument keys")
        if any(not isinstance(key, str) or "|" not in key for key in keys):
            raise ValueError("Every instrument_key must come from the Upstox instrument master")
        token = self.tokens.get_token()
        key = "quotes:" + json.dumps(keys)
        cached = self.cache.get(key, self.cache_seconds)
        if cached:
            return MarketDataSnapshot(cached[0], cached[1], "Upstox Full Market Quotes V3", "LIVE_QUOTE", True)
        response = execute_read(
            lambda: self.adapter.market_quotes(token, keys),
            token_manager=self.tokens, limiter=self.limiter, max_attempts=self.max_retries,
        )
        # Quote API returns the quote map directly under `data`, unlike candles' named array.
        if hasattr(response, "to_dict"):
            response = response.to_dict()
        if isinstance(response, tuple):
            response = response[0]
            if hasattr(response, "to_dict"):
                response = response.to_dict()
        if not isinstance(response, dict) or response.get("status") != "success" or not isinstance(response.get("data"), dict):
            raise InvalidResponseError("Upstox full quote response must contain status=success and an object data map")
        quotes = response["data"]
        if not quotes:
            raise InvalidResponseError("Upstox full quote response contains no quote data")
        for instrument, quote in quotes.items():
            if not isinstance(quote, dict) or "last_price" not in quote:
                raise InvalidResponseError(f"Quote for {instrument} is missing last_price")
            finite_number(quote["last_price"], f"{instrument}.last_price")
            ohlc = quote.get("ohlc")
            if ohlc is not None:
                if not isinstance(ohlc, dict):
                    raise InvalidResponseError(f"Quote for {instrument} has invalid ohlc")
                for field in ("open", "high", "low", "close"):
                    if field in ohlc:
                        finite_number(ohlc[field], f"{instrument}.ohlc.{field}")
        retrieved = utc_retrieved_at()
        self.cache.set(key, quotes, self.cache_seconds, retrieved)
        return MarketDataSnapshot(quotes, retrieved, "Upstox Full Market Quotes V3", "LIVE_QUOTE", False)
