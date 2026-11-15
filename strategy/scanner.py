"""Daily-candle completeness checks, technical indicators, and research scanner results."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import math
from typing import Any, Iterable, Protocol

import pandas as pd
from integrations.upstox.errors import UpstoxError
from strategy.indicators import add_indicators


class DailyCandleProvider(Protocol):
    def historical_candles(self, instrument_key: str, unit: str, interval: int,
                           to_date: Any, from_date: Any = None) -> Any: ...


@dataclass(frozen=True)
class ScannerResult:
    symbol: str
    company_name: str
    status: str
    data_source: str | None
    retrieved_at: datetime | None
    candle_count: int
    data_complete: bool
    close: float | None = None
    ema20: float | None = None
    ema50: float | None = None
    rsi14: float | None = None
    atr14: float | None = None
    volume_ratio: float | None = None
    ema_trend: str = "Unknown"
    direction: str = "Neutral"
    strong_volume: bool = False
    risk_reward: float | None = None
    planned_stop: float | None = None
    planned_target: float | None = None
    explanation: str = ""


def candles_to_frame(candles: list[list[object]]) -> pd.DataFrame:
    """Convert documented Upstox V3 candle rows into a chronologically ordered frame."""
    rows = []
    for row in candles:
        if len(row) < 6:
            raise ValueError("Daily candle row must include timestamp, OHLC, and volume")
        timestamp = pd.to_datetime(row[0], utc=True, errors="coerce")
        if pd.isna(timestamp):
            raise ValueError("Daily candle contains an invalid timestamp")
        values = [float(value) for value in row[1:6]]
        if not all(math.isfinite(value) for value in values) or values[4] < 0:
            raise ValueError("Daily candle contains non-finite prices or invalid volume")
        rows.append((timestamp, *values))
    frame = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    if frame.empty:
        return frame
    if frame["timestamp"].duplicated().any():
        raise ValueError("Daily candle data contains duplicate timestamps")
    frame = frame.sort_values("timestamp").reset_index(drop=True)
    if frame["timestamp"].diff().dropna().gt(pd.Timedelta(days=10)).any():
        raise ValueError("Daily candle history contains a gap greater than 10 calendar days")
    if ((frame["high"] < frame[["open", "close", "low"]].max(axis=1)) |
            (frame["low"] > frame[["open", "close", "high"]].min(axis=1))).any():
        raise ValueError("Daily candle OHLC values are inconsistent")
    return frame


def calculate_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    """Calculate the shared technical indicators and preserve legacy scanner field names."""
    if len(frame) < 60:
        raise ValueError("At least 60 complete daily candles are required to warm up indicators")
    source = frame.set_index("timestamp") if "timestamp" in frame.columns else frame
    output = add_indicators(source)
    output["average_volume20"] = output["volume_sma20"]
    output["volume_ratio"] = output["relative_volume"]
    return output


class StockScanner:
    """Scan enabled, mapped shares using real provider candles; failures stay per-stock and explicit."""

    minimum_candles = 60
    strong_volume_threshold = 1.5

    def __init__(self, market_data: DailyCandleProvider) -> None:
        self.market_data = market_data

    def scan_stock(self, stock: Any, *, to_date: date | None = None, lookback_days: int = 180) -> ScannerResult:
        symbol, name = stock.symbol, stock.name
        if lookback_days < 1:
            raise ValueError("lookback_days must be positive")
        if not getattr(stock, "active", False):
            return ScannerResult(symbol, name, "Disabled", None, None, 0, False, explanation="Stock is disabled.")
        instrument_key = getattr(stock, "instrument_key", None)
        if not instrument_key or not getattr(stock, "instrument_available", False):
            return ScannerResult(symbol, name, "Unavailable instrument", None, None, 0, False,
                                 explanation="No validated Upstox NSE equity mapping is available.")
        end_date = to_date or datetime.now(timezone.utc).date()
        start_date = end_date - timedelta(days=lookback_days)
        try:
            snapshot = self.market_data.historical_candles(instrument_key, "days", 1, end_date, start_date)
            frame = candles_to_frame(snapshot.data)
            if len(frame) < self.minimum_candles:
                return ScannerResult(symbol, name, "Insufficient data", snapshot.source, snapshot.retrieved_at,
                                     len(frame), False, explanation=f"Need at least {self.minimum_candles} valid daily candles; received {len(frame)}.")
            indicators = calculate_indicators(frame)
            last = indicators.iloc[-1]
            values = [last[key] for key in ("ema20", "ema50", "rsi14", "atr14", "volume_ratio")]
            if any(pd.isna(value) or not math.isfinite(float(value)) for value in values):
                return ScannerResult(symbol, name, "Incomplete indicators", snapshot.source, snapshot.retrieved_at,
                                     len(frame), False, explanation="Latest candle does not have all required warmed-up indicators.")
            close, ema20, ema50, rsi, atr, volume_ratio = (float(last[key]) for key in
                                                           ("close", "ema20", "ema50", "rsi14", "atr14", "volume_ratio"))
            ema_trend = "Bullish" if close > ema20 > ema50 else "Bearish" if close < ema20 < ema50 else "Mixed"
            strong_volume = volume_ratio >= self.strong_volume_threshold
            direction = "Bullish" if ema_trend == "Bullish" and 50 <= rsi <= 70 and strong_volume else (
                "Bearish" if ema_trend == "Bearish" and rsi < 50 else "Neutral")
            if direction == "Bullish" and atr > 0:
                planned_stop, planned_target = close - 2 * atr, close + 3 * atr
            elif direction == "Bearish" and atr > 0:
                planned_stop, planned_target = close + 2 * atr, close - 3 * atr
            else:
                planned_stop = planned_target = None
            risk_reward = (abs(planned_target - close) / abs(close - planned_stop)
                           if planned_stop is not None and planned_target is not None and close != planned_stop else None)
            explanation = (f"Close {close:.2f}; EMA trend {ema_trend}; RSI14 {rsi:.1f}; volume {volume_ratio:.2f}x 20-session average. "
                           "Bullish setup requires close > EMA20 > EMA50, RSI 50–70, and volume ≥1.5x; "
                           "bearish classification requires close < EMA20 < EMA50 and RSI <50. Research classification only.")
            return ScannerResult(symbol, name, "Ready", snapshot.source, snapshot.retrieved_at, len(frame), True,
                                 close, ema20, ema50, rsi, atr, volume_ratio, ema_trend, direction, strong_volume,
                                 risk_reward, planned_stop, planned_target, explanation)
        except (UpstoxError, ValueError, TypeError, KeyError, OSError, TimeoutError) as exc:
            return ScannerResult(symbol, name, f"Data error: {type(exc).__name__}", None, None, 0, False,
                                 explanation="Daily candles could not be retrieved or validated; no values were inferred.")

    def scan(self, stocks: Iterable[Any], *, to_date: date | None = None, lookback_days: int = 180) -> list[ScannerResult]:
        return [self.scan_stock(stock, to_date=to_date, lookback_days=lookback_days) for stock in stocks]


def filter_scan_results(results: Iterable[ScannerResult], *, direction: str = "All", strong_volume_only: bool = False,
                        rsi_min: float = 0, rsi_max: float = 100, ema_trend: str = "All",
                        minimum_risk_reward: float = 0, sort_by: str = "symbol", descending: bool = False
                        ) -> list[ScannerResult]:
    """Apply dashboard filters and stable sorting without silently dropping scan failures."""
    if direction not in {"All", "Bullish", "Bearish", "Neutral"}:
        raise ValueError("direction filter must be All, Bullish, Bearish, or Neutral")
    if not 0 <= rsi_min <= rsi_max <= 100:
        raise ValueError("RSI range must satisfy 0 <= minimum <= maximum <= 100")
    if ema_trend not in {"All", "Bullish", "Bearish", "Mixed", "Unknown"}:
        raise ValueError("Invalid EMA trend filter")
    allowed_sort = {"symbol", "close", "rsi14", "volume_ratio", "risk_reward"}
    if sort_by not in allowed_sort:
        raise ValueError(f"sort_by must be one of {sorted(allowed_sort)}")
    selected = [row for row in results
                if (direction == "All" or row.direction == direction)
                and (not strong_volume_only or row.strong_volume)
                and (row.rsi14 is None or rsi_min <= row.rsi14 <= rsi_max)
                and (ema_trend == "All" or row.ema_trend == ema_trend)
                and (row.risk_reward is None or row.risk_reward >= minimum_risk_reward)]
    return sorted(selected, key=lambda row: (getattr(row, sort_by) is None, getattr(row, sort_by) or 0), reverse=descending)
