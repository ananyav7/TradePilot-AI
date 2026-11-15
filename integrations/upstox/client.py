"""Convenience façade wiring tokens, the SDK, rate limiting, and read-only clients."""

from typing import Any

from config.settings import Settings, get_settings
from integrations.upstox.historical_data import HistoricalCandleClient
from integrations.upstox.market_quotes import MarketQuoteClient
from integrations.upstox.rate_limiter import SlidingWindowRateLimiter
from integrations.upstox.sdk_adapter import UpstoxSDKAdapter
from integrations.upstox.token_manager import TokenManager
from integrations.upstox.common import MarketDataSnapshot


class MarketDataClient:
    """Read-only facade for Upstox V3 candles and full market quotes."""

    def __init__(self, token_manager: TokenManager, *, settings: Settings | None = None,
                 adapter: Any | None = None, limiter: SlidingWindowRateLimiter | None = None) -> None:
        config = settings or get_settings()
        shared_limiter = limiter or SlidingWindowRateLimiter()
        read_adapter = adapter or UpstoxSDKAdapter(timeout=config.upstox_api_timeout_seconds)
        self.historical_data = HistoricalCandleClient(
            read_adapter, token_manager, limiter=shared_limiter,
            max_retries=config.upstox_max_read_retries,
        )
        self.market_quotes = MarketQuoteClient(
            read_adapter, token_manager, limiter=shared_limiter,
            max_retries=config.upstox_max_read_retries,
        )

    def historical_candles(self, instrument_key: str, unit: str, interval: int,
                           to_date: Any, from_date: Any = None) -> MarketDataSnapshot:
        return self.historical_data.historical(instrument_key, unit, interval, to_date, from_date)

    def intraday_candles(self, instrument_key: str, unit: str = "minutes", interval: int = 1) -> MarketDataSnapshot:
        return self.historical_data.intraday(instrument_key, unit, interval)

    def full_market_quotes(self, instrument_keys: list[str] | tuple[str, ...]) -> MarketDataSnapshot:
        return self.market_quotes.full_quotes(instrument_keys)
