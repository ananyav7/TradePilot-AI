"""Chronological holdout and expanding-window walk-forward evaluation helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from backtesting.engine import BacktestResult, HistoricalBacktester, InsufficientBacktestData
from strategy.indicators import validate_ohlcv
from strategy.technical_analysis import MINIMUM_HISTORY


@dataclass(frozen=True)
class ChronologicalSplit:
    train: pd.DataFrame
    test_with_warmup: pd.DataFrame
    train_end: date
    test_start: date
    test_end: date


@dataclass(frozen=True)
class TrainTestResults:
    train: BacktestResult
    test: BacktestResult
    split: ChronologicalSplit


@dataclass(frozen=True)
class WalkForwardFold:
    fold: int
    training_end: date
    test_start: date
    test_end: date
    result: BacktestResult


def chronological_split(ohlcv: pd.DataFrame, train_fraction: float = 0.7) -> ChronologicalSplit:
    """Split by time; test includes only preceding rows needed for causal indicator warm-up."""
    if not 0.5 <= train_fraction < 1.0:
        raise ValueError("train_fraction must be at least 0.5 and less than 1")
    frame, _ = validate_ohlcv(ohlcv)
    cut = int(len(frame) * train_fraction)
    if cut < MINIMUM_HISTORY or len(frame) - cut < 2:
        raise InsufficientBacktestData("Chronological split needs indicator warm-up history and at least two test bars")
    train = frame.iloc[:cut].copy()
    test_with_warmup = frame.copy()  # Prior observations are context only; test trading starts at split boundary.
    return ChronologicalSplit(train, test_with_warmup, train.index[-1].date(),
                              frame.index[cut].date(), frame.index[-1].date())


def run_chronological_train_test(backtester: HistoricalBacktester, ohlcv: pd.DataFrame, *,
                                 symbol: str, source: str = "user_supplied_historical_data",
                                 train_fraction: float = 0.7) -> TrainTestResults:
    split = chronological_split(ohlcv, train_fraction)
    train = backtester.run(split.train, symbol=symbol, source=source)
    test = backtester.run(split.test_with_warmup, symbol=symbol, source=source,
                          start_date=split.test_start, end_date=split.test_end)
    return TrainTestResults(train, test, split)


def walk_forward_test(backtester: HistoricalBacktester, ohlcv: pd.DataFrame, *,
                      symbol: str, initial_train_bars: int, test_bars: int,
                      source: str = "user_supplied_historical_data") -> tuple[WalkForwardFold, ...]:
    """Run sequential held-out windows with fixed parameters and expanding history."""
    frame, _ = validate_ohlcv(ohlcv)
    if initial_train_bars < MINIMUM_HISTORY or test_bars < 2:
        raise ValueError(f"initial_train_bars must be >= {MINIMUM_HISTORY}; test_bars must be >= 2")
    if initial_train_bars + test_bars > len(frame):
        raise InsufficientBacktestData("Dataset is shorter than the requested initial train and test windows")
    folds = []
    train_end = initial_train_bars
    fold = 1
    while train_end < len(frame):
        test_end = min(train_end + test_bars, len(frame))
        if test_end - train_end < 2:
            break
        start_date = frame.index[train_end].date()
        end_date = frame.index[test_end - 1].date()
        result = backtester.run(frame.iloc[:test_end], symbol=symbol, source=source,
                                start_date=start_date, end_date=end_date)
        folds.append(WalkForwardFold(fold, frame.index[train_end - 1].date(),
                                      start_date, end_date, result))
        train_end = test_end
        fold += 1
    return tuple(folds)
