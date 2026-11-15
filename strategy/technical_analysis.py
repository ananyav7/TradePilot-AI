"""Validated end-of-day OHLCV analysis and Plotly-ready technical-series output."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd

from strategy.indicators import IndicatorValues, add_indicators, latest_indicator_values, validate_ohlcv

INDIA_TZ = ZoneInfo("Asia/Kolkata")
MINIMUM_HISTORY = 51  # EMA50 warm-up plus one completed prior candle for rolling levels/change.


@dataclass(frozen=True)
class TechnicalAnalysisResult:
    """Analysis series and metadata; `frame` is ready to pass into Plotly traces."""

    frame: pd.DataFrame
    latest: IndicatorValues
    dropped_incomplete_candles: int
    excluded_uncompleted_candles: int
    end_of_day: bool
    source: str

    @property
    def plotly_frame(self) -> pd.DataFrame:
        """Return timestamp as a regular column for Plotly Express/Graph Objects."""
        return self.frame.reset_index(names="timestamp").copy()


def _market_dates(index: pd.DatetimeIndex) -> pd.Index:
    if index.tz is None:
        return pd.Index(index.date)
    return pd.Index(index.tz_convert(INDIA_TZ).date)


class TechnicalAnalysisEngine:
    """Calculate causal indicators from valid candles and enforce completed EOD input."""

    def analyze(self, ohlcv: pd.DataFrame, *, end_of_day: bool = True,
                as_of: date | datetime | None = None,
                completed_through: date | None = None,
                source: str = "unspecified") -> TechnicalAnalysisResult:
        """Analyze candles up to the last completed date.

        End-of-day mode excludes the as-of market date (assumed potentially partial).
        Callers that have authoritative completion metadata may supply
        ``completed_through`` to include candles through that market date. No future
        row is used to backfill or define an indicator at an earlier timestamp.
        """
        if not isinstance(end_of_day, bool):
            raise ValueError("end_of_day must be a boolean")
        normalized, dropped = validate_ohlcv(ohlcv)
        excluded = 0
        if end_of_day:
            if completed_through is not None:
                cutoff = completed_through
                keep = _market_dates(normalized.index) <= cutoff
            else:
                now = as_of or datetime.now(INDIA_TZ)
                if isinstance(now, datetime):
                    cutoff = now.astimezone(INDIA_TZ).date() if now.tzinfo else now.date()
                else:
                    cutoff = now
                keep = _market_dates(normalized.index) < cutoff
            excluded = int((~keep).sum())
            normalized = normalized.loc[keep]
        if len(normalized) < MINIMUM_HISTORY:
            raise ValueError(f"Insufficient completed history: need at least {MINIMUM_HISTORY} candles, got {len(normalized)}")
        output = add_indicators(normalized)
        latest = latest_indicator_values(output)
        return TechnicalAnalysisResult(output, latest, dropped, excluded, end_of_day, source)


def analyze_ohlcv(ohlcv: pd.DataFrame, *, end_of_day: bool = True,
                  as_of: date | datetime | None = None, completed_through: date | None = None,
                  source: str = "unspecified") -> TechnicalAnalysisResult:
    """Convenience function for callers that do not need to retain an engine instance."""
    return TechnicalAnalysisEngine().analyze(ohlcv, end_of_day=end_of_day, as_of=as_of,
                                             completed_through=completed_through, source=source)
