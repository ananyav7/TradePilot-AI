"""Independent reference-value and input-safety tests for technical analysis."""

from datetime import date
import math

import numpy as np
import pandas as pd
import pytest

from strategy.indicators import add_indicators, latest_indicator_values, validate_ohlcv
from strategy.market_conditions import classify_market_conditions
from strategy.technical_analysis import TechnicalAnalysisEngine


def sample_ohlcv(size: int = 80) -> pd.DataFrame:
    index = pd.date_range("2025-01-01", periods=size, freq="D", tz="Asia/Kolkata")
    steps = np.array([0.7, -0.2, 1.1, -0.6, 0.4, -0.1, 0.8, -0.5] * (size // 8 + 1))[:size]
    close = 100 + np.cumsum(steps)
    return pd.DataFrame({
        "open": close - 0.25,
        "high": close + np.linspace(0.8, 1.2, size),
        "low": close - np.linspace(0.7, 1.0, size),
        "close": close,
        "volume": 1000 + (np.arange(size) * 37 % 550),
    }, index=index)


def reference_ema(values: np.ndarray, window: int) -> np.ndarray:
    result = np.full(len(values), np.nan)
    alpha = 2 / (window + 1)
    result[0] = values[0]
    for i in range(1, len(values)):
        result[i] = alpha * values[i] + (1 - alpha) * result[i - 1]
    result[:window - 1] = np.nan
    return result


def reference_rsi(close: np.ndarray, window: int = 14) -> np.ndarray:
    change = np.diff(close, prepend=close[0])
    gain, loss = np.maximum(change, 0), np.maximum(-change, 0)
    avg_gain, avg_loss = gain[0], loss[0]
    result = np.full(len(close), np.nan)
    for i in range(1, len(close)):
        avg_gain = ((window - 1) * avg_gain + gain[i]) / window
        avg_loss = ((window - 1) * avg_loss + loss[i]) / window
        if i >= window - 1:
            result[i] = 100 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    return result


def reference_atr(frame: pd.DataFrame, window: int = 14) -> np.ndarray:
    high, low, close = (frame[column].to_numpy() for column in ("high", "low", "close"))
    previous = np.r_[np.nan, close[:-1]]
    tr = np.fmax.reduce([high - low, np.abs(high - previous), np.abs(low - previous)])
    result = np.full(len(frame), np.nan)
    result[window - 1] = tr[:window].mean()
    for i in range(window, len(frame)):
        result[i] = ((window - 1) * result[i - 1] + tr[i]) / window
    return result


def test_indicators_match_independent_calculations():
    frame = sample_ohlcv()
    result = add_indicators(frame)
    close = frame["close"].to_numpy()
    volume = frame["volume"].to_numpy()
    high, low = frame["high"].to_numpy(), frame["low"].to_numpy()
    i = len(frame) - 1

    assert result["ema20"].iloc[i] == pytest.approx(reference_ema(close, 20)[i])
    assert result["ema50"].iloc[i] == pytest.approx(reference_ema(close, 50)[i])
    assert result["rsi14"].iloc[i] == pytest.approx(reference_rsi(close)[i])
    assert result["atr14"].iloc[i] == pytest.approx(reference_atr(frame)[i])
    assert result["volume_sma20"].iloc[i] == pytest.approx(volume[i - 19:i + 1].mean())
    assert result["relative_volume"].iloc[i] == pytest.approx(volume[i] / volume[i - 20:i].mean())
    assert result["daily_change_pct"].iloc[i] == pytest.approx((close[i] / close[i - 1] - 1) * 100)
    assert result["support20"].iloc[i] == pytest.approx(low[i - 20:i].min())
    assert result["resistance20"].iloc[i] == pytest.approx(high[i - 20:i].max())


def test_no_lookahead_future_rows_do_not_change_prior_indicator_values():
    frame = sample_ohlcv()
    prefix_result = add_indicators(frame.iloc[:65])
    full_result = add_indicators(frame)
    columns = ["ema20", "ema50", "rsi14", "atr14", "volume_sma20", "relative_volume",
               "daily_change_pct", "support20", "resistance20"]
    pd.testing.assert_frame_equal(prefix_result[columns], full_result.iloc[:65][columns])


def test_validation_drops_incomplete_candles_without_filling_them():
    frame = sample_ohlcv(62)
    frame.iloc[20, frame.columns.get_loc("close")] = np.nan
    cleaned, dropped = validate_ohlcv(frame)
    assert dropped == 1
    assert len(cleaned) == 61
    assert cleaned["close"].isna().sum() == 0
    assert cleaned.index.is_monotonic_increasing


@pytest.mark.parametrize("mutate, message", [
    (lambda df: df.drop(columns=["volume"]), "missing columns"),
    (lambda df: df.assign(high=df["low"] - 1), "inconsistent"),
    (lambda df: df.assign(volume=-1), "negative"),
    (lambda df: df.set_axis([df.index[0]] * len(df)), "unique"),
])
def test_invalid_ohlcv_is_rejected(mutate, message):
    with pytest.raises(ValueError, match=message):
        validate_ohlcv(mutate(sample_ohlcv(60)))


def test_insufficient_history_and_zero_volume_reference_are_explicit():
    with pytest.raises(ValueError, match="Insufficient completed history"):
        TechnicalAnalysisEngine().analyze(sample_ohlcv(50), end_of_day=False)
    frame = sample_ohlcv(60)
    frame["volume"] = 0
    result = add_indicators(frame)
    assert math.isnan(result["relative_volume"].iloc[-1])
    with pytest.raises(ValueError, match="Insufficient historical data"):
        latest_indicator_values(result)


def test_end_of_day_excludes_as_of_market_date_unless_marked_completed():
    frame = sample_ohlcv(62)
    as_of = date(2025, 3, 3)
    result = TechnicalAnalysisEngine().analyze(frame, as_of=as_of, source="test daily feed")
    assert result.excluded_uncompleted_candles == 1
    assert result.frame.index[-1].date() < as_of
    explicitly_completed = TechnicalAnalysisEngine().analyze(frame, as_of=as_of, completed_through=as_of)
    assert explicitly_completed.frame.index[-1].tz_convert("Asia/Kolkata").date() == as_of
    assert result.source == "test daily feed"


def test_result_is_plotly_ready_and_market_conditions_are_descriptive():
    frame = sample_ohlcv(70)
    result = TechnicalAnalysisEngine().analyze(frame, end_of_day=False)
    assert "timestamp" in result.plotly_frame.columns
    assert {"ema20", "ema50", "rsi14", "atr14", "volume_sma20", "relative_volume",
            "daily_change_pct", "support20", "resistance20"}.issubset(result.plotly_frame.columns)
    conditions = classify_market_conditions(result.latest)
    assert conditions.trend in {"Bullish", "Bearish", "Sideways / mixed"}
    assert conditions.volatility in {"Low", "Normal", "High"}
