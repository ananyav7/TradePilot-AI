"""Causal OHLCV indicator calculations with no forward filling or future data."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

try:  # The ta package supplies standard implementations; formulas below are equivalent fallbacks.
    from ta.momentum import RSIIndicator
    from ta.trend import EMAIndicator
    from ta.volatility import AverageTrueRange
except ImportError:  # Allows the core calculations to remain usable before optional dependency installation.
    RSIIndicator = EMAIndicator = AverageTrueRange = None


OHLCV_COLUMNS = ("open", "high", "low", "close", "volume")


@dataclass(frozen=True)
class IndicatorValues:
    """Typed snapshot of the most recent fully warmed-up indicator row."""

    timestamp: pd.Timestamp
    close: float
    ema20: float
    ema50: float
    rsi14: float
    atr14: float
    volume_sma20: float
    relative_volume: float
    daily_change_pct: float
    support20: float
    resistance20: float


def validate_ohlcv(frame: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Validate columns and prices, sort timestamps, and drop incomplete rows without interpolation."""
    if not isinstance(frame, pd.DataFrame):
        raise TypeError("OHLCV input must be a pandas DataFrame")
    if frame.empty:
        raise ValueError("OHLCV input contains no candles")
    normalized = frame.copy()
    normalized.columns = [str(column).strip().lower() for column in normalized.columns]
    missing_columns = set(OHLCV_COLUMNS) - set(normalized.columns)
    if missing_columns:
        raise ValueError(f"OHLCV input is missing columns: {', '.join(sorted(missing_columns))}")
    if "timestamp" in normalized.columns:
        timestamps = pd.to_datetime(normalized.pop("timestamp"), utc=True, errors="coerce")
        normalized.index = pd.DatetimeIndex(timestamps, name="timestamp")
    elif "date" in normalized.columns and not isinstance(normalized.index, pd.DatetimeIndex):
        timestamps = pd.to_datetime(normalized.pop("date"), utc=True, errors="coerce")
        normalized.index = pd.DatetimeIndex(timestamps, name="timestamp")
    elif not isinstance(normalized.index, pd.DatetimeIndex):
        raise ValueError("OHLCV input must have a DatetimeIndex or timestamp/date column")
    else:
        timestamps = pd.to_datetime(normalized.index, utc=True, errors="coerce")
        normalized.index = pd.DatetimeIndex(timestamps, name="timestamp")
    if normalized.index.hasnans:
        raise ValueError("OHLCV timestamps must be valid dates")
    if normalized.index.has_duplicates:
        raise ValueError("OHLCV timestamps must be unique")
    normalized = normalized.loc[:, list(OHLCV_COLUMNS)].apply(pd.to_numeric, errors="coerce")
    complete = normalized.notna().all(axis=1)
    missing_candles = int((~complete).sum())
    normalized = normalized.loc[complete].astype(float).sort_index()
    if normalized.empty:
        raise ValueError("OHLCV input contains no complete candles")
    if not np.isfinite(normalized.to_numpy()).all():
        raise ValueError("OHLCV values must be finite")
    if (normalized["volume"] < 0).any():
        raise ValueError("Volume cannot be negative")
    if (normalized[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError("OHLC prices must be greater than zero")
    invalid = ((normalized["high"] < normalized[["open", "close", "low"]].max(axis=1)) |
               (normalized["low"] > normalized[["open", "close", "high"]].min(axis=1)))
    if invalid.any():
        raise ValueError("OHLC candle values are inconsistent")
    return normalized, missing_candles


def add_indicators(frame: pd.DataFrame, *, ema_fast: int = 20, ema_slow: int = 50,
                   rsi_window: int = 14, atr_window: int = 14,
                   volume_window: int = 20, level_window: int = 20) -> pd.DataFrame:
    """Append indicators; every value at row t uses only rows at or before t.

    Relative volume uses the current candle's volume divided by the prior 20
    completed sessions' mean, so today's volume does not affect its baseline.
    Support and resistance use the preceding ``level_window`` sessions and are
    shifted one row so the current candle cannot define its own breakout level.
    """
    if min(ema_fast, ema_slow, rsi_window, atr_window, volume_window, level_window) < 1:
        raise ValueError("Indicator windows must be positive")
    if ema_fast >= ema_slow:
        raise ValueError("ema_fast must be less than ema_slow")
    output, _ = validate_ohlcv(frame)
    close, high, low, volume = output["close"], output["high"], output["low"], output["volume"]
    if EMAIndicator is not None:
        output["ema20"] = EMAIndicator(close, window=ema_fast, fillna=False).ema_indicator()
        output["ema50"] = EMAIndicator(close, window=ema_slow, fillna=False).ema_indicator()
        output["rsi14"] = RSIIndicator(close, window=rsi_window, fillna=False).rsi()
    else:
        output["ema20"] = close.ewm(span=ema_fast, adjust=False, min_periods=ema_fast).mean()
        output["ema50"] = close.ewm(span=ema_slow, adjust=False, min_periods=ema_slow).mean()
        difference = close.diff()
        gains = difference.where(difference > 0, 0.0)
        losses = -difference.where(difference < 0, 0.0)
        average_gain = gains.ewm(alpha=1 / rsi_window, min_periods=rsi_window, adjust=False).mean()
        average_loss = losses.ewm(alpha=1 / rsi_window, min_periods=rsi_window, adjust=False).mean()
        relative_strength = average_gain / average_loss
        output["rsi14"] = pd.Series(np.where(average_loss == 0, 100,
                                             100 - (100 / (1 + relative_strength))), index=output.index)
    previous_close = close.shift(1)
    true_range = pd.concat([(high - low), (high - previous_close).abs(),
                             (low - previous_close).abs()], axis=1).max(axis=1)
    if AverageTrueRange is not None and len(output) >= atr_window:
        atr = AverageTrueRange(high, low, close, window=atr_window, fillna=False).average_true_range()
        output["atr14"] = atr.where(pd.Series(np.arange(len(atr)) >= atr_window - 1, index=atr.index))
    else:
        output["atr14"] = np.nan
        if len(output) >= atr_window:
            output.iloc[atr_window - 1, output.columns.get_loc("atr14")] = true_range.iloc[:atr_window].mean()
            for position in range(atr_window, len(output)):
                output.iloc[position, output.columns.get_loc("atr14")] = (
                    output["atr14"].iloc[position - 1] * (atr_window - 1) + true_range.iloc[position]
                ) / atr_window
    output["volume_sma20"] = volume.rolling(volume_window, min_periods=volume_window).mean()
    prior_average = output["volume_sma20"].shift(1)
    output["relative_volume"] = volume / prior_average.where(prior_average != 0)
    output["daily_change_pct"] = close.pct_change(fill_method=None) * 100
    output["support20"] = low.rolling(level_window, min_periods=level_window).min().shift(1)
    output["resistance20"] = high.rolling(level_window, min_periods=level_window).max().shift(1)
    return output


def latest_indicator_values(frame: pd.DataFrame) -> IndicatorValues:
    """Return the last row after requiring every requested indicator to be available."""
    columns = ["ema20", "ema50", "rsi14", "atr14", "volume_sma20", "relative_volume",
               "daily_change_pct", "support20", "resistance20"]
    if frame.empty or any(column not in frame for column in columns):
        raise ValueError("Indicator frame is empty or missing calculated fields")
    latest = frame.iloc[-1]
    if latest[columns].isna().any():
        raise ValueError("Insufficient historical data to calculate all latest indicators")
    return IndicatorValues(
        timestamp=pd.Timestamp(frame.index[-1]), close=float(latest["close"]),
        ema20=float(latest["ema20"]), ema50=float(latest["ema50"]),
        rsi14=float(latest["rsi14"]), atr14=float(latest["atr14"]),
        volume_sma20=float(latest["volume_sma20"]), relative_volume=float(latest["relative_volume"]),
        daily_change_pct=float(latest["daily_change_pct"]), support20=float(latest["support20"]),
        resistance20=float(latest["resistance20"]),
    )
