"""Descriptive market regime labels derived from completed technical-analysis rows."""

from dataclasses import dataclass
import math

from strategy.indicators import IndicatorValues


@dataclass(frozen=True)
class MarketConditions:
    """Non-executable regime summary for a single completed market candle."""

    trend: str
    volatility: str
    momentum: str
    volume_activity: str
    close_vs_support: str
    close_vs_resistance: str


def classify_market_conditions(values: IndicatorValues) -> MarketConditions:
    """Summarize trend, volatility, momentum and volume from one causal indicator snapshot."""
    numeric = (values.close, values.ema20, values.ema50, values.rsi14, values.atr14,
               values.relative_volume, values.support20, values.resistance20)
    if (not all(math.isfinite(value) for value in numeric) or values.close <= 0 or values.atr14 < 0
            or values.relative_volume < 0 or not 0 <= values.rsi14 <= 100):
        raise ValueError("Market-condition inputs contain invalid price/volatility/volume values")
    trend = "Bullish" if values.close > values.ema20 > values.ema50 else (
        "Bearish" if values.close < values.ema20 < values.ema50 else "Sideways / mixed")
    atr_percent = values.atr14 / values.close * 100
    volatility = "High" if atr_percent >= 4 else "Low" if atr_percent <= 1.5 else "Normal"
    momentum = "Overbought" if values.rsi14 >= 70 else "Oversold" if values.rsi14 <= 30 else "Neutral"
    volume_activity = "Strong" if values.relative_volume >= 1.5 else (
        "Quiet" if values.relative_volume < 0.75 else "Typical")
    return MarketConditions(
        trend=trend,
        volatility=volatility,
        momentum=momentum,
        volume_activity=volume_activity,
        close_vs_support="Above support" if values.close >= values.support20 else "Below support",
        close_vs_resistance="Above resistance" if values.close > values.resistance20 else "Below resistance",
    )
