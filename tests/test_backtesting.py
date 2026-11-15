
import numpy as np
import pandas as pd
import pytest

from backtesting.engine import (BacktestConfig, HistoricalBacktester,
                                InsufficientBacktestData)
from backtesting.engine import _Position
from backtesting.validation import (chronological_split,
                                    run_chronological_train_test,
                                    walk_forward_test)
from risk.engine import RiskConfig


def candles(count=360):
    index = pd.bdate_range("2023-01-02", periods=count, tz="UTC")
    steps = np.arange(count)
    close = 100 + steps * 0.07 + 1.2 * np.sin(steps / 5.0) + 0.4 * np.sin(steps / 2.1)
    open_ = close - 0.15 * np.sin(steps)
    high = np.maximum(open_, close) + 0.8
    low = np.minimum(open_, close) - 0.8
    volume = np.where(steps % 8 == 0, 2400, 1200)
    return pd.DataFrame({"open": open_, "high": high, "low": low,
                         "close": close, "volume": volume}, index=index)


def backtester():
    risk = RiskConfig(starting_capital="3000", risk_per_trade="0.005")
    cfg = BacktestConfig(initial_capital="3000", slippage_bps="10",
                         meaningful_minimum_bars=100, meaningful_minimum_trades=3,
                         risk_config=risk)
    return HistoricalBacktester(backtest_config=cfg)


def test_backtest_returns_metrics_records_and_costs():
    result = backtester().run(candles(), symbol="TEST", source="unit-test-candles")
    assert result.symbol == "TEST"
    assert result.source == "unit-test-candles"
    assert result.metrics.initial_capital == 3000
    assert result.metrics.final_capital == pytest.approx(
        result.metrics.initial_capital + result.metrics.net_profit_loss)
    assert result.metrics.total_transaction_costs >= 0
    assert result.metrics.bars_tested > 300
    assert {"equity", "drawdown_pct"}.issubset(result.equity_curve.columns)
    assert {"month", "return_pct"}.issubset(result.monthly_returns.columns)
    if not result.trades.empty:
        assert (result.trades["quantity"] > 0).all()
        assert (result.trades["total_costs"] > 0).all()
        assert result.trades["symbol"].eq("TEST").all()
        assert (result.trades["holding_period_sessions"] >= 1).all()


def test_signal_is_not_filled_until_next_session_open():
    frame = candles()
    result = backtester().run(frame, symbol="TEST")
    if result.trades.empty:
        pytest.skip("This fixed synthetic series did not trigger the configured strategy")
    trade = result.trades.iloc[0]
    assert pd.Timestamp(trade.entry_date) > pd.Timestamp(trade.signal_date)


def test_chronological_holdout_has_no_test_trading_before_split():
    frame = candles()
    split = chronological_split(frame, 0.7)
    assert split.train.index[-1].date() < split.test_start
    result = run_chronological_train_test(backtester(), frame, symbol="TEST", train_fraction=0.7)
    assert result.test.equity_curve.index[0].date() >= result.split.test_start
    assert result.test.metrics.bars_tested == len(frame) - int(len(frame) * 0.7)


def test_walk_forward_uses_fixed_configuration_on_sequential_holdouts():
    result = walk_forward_test(backtester(), candles(340), symbol="TEST",
                               initial_train_bars=180, test_bars=60)
    assert len(result) == 3
    assert result[0].test_start > result[0].training_end
    assert result[1].test_start > result[0].test_end
    assert all(fold.result.strategy_version == "swing-v1" for fold in result)


def test_reports_insufficient_history_and_invalid_splits():
    with pytest.raises(InsufficientBacktestData):
        backtester().run(candles(50), symbol="TEST")
    with pytest.raises(ValueError):
        chronological_split(candles(), 1.0)
    with pytest.raises(ValueError):
        walk_forward_test(backtester(), candles(), symbol="TEST",
                          initial_train_bars=50, test_bars=10)


def test_future_data_does_not_change_prior_trade_records():
    frame = candles(300)
    first = backtester().run(frame, symbol="TEST", end_date=frame.index[230].date())
    extended = pd.concat([frame, candles(40).set_axis(pd.bdate_range(
        frame.index[-1] + pd.Timedelta(days=1), periods=40, tz="UTC"))])
    second = backtester().run(extended, symbol="TEST", end_date=frame.index[230].date())
    pd.testing.assert_frame_equal(first.trades.reset_index(drop=True), second.trades.reset_index(drop=True))


def test_daily_stop_and_target_fills_are_conservative_and_slippage_is_adverse():
    engine = backtester()
    position = _Position(signal_date=pd.Timestamp("2024-01-02", tz="UTC"),
                         entry_date=pd.Timestamp("2024-01-03", tz="UTC"),
                         entry_reference_price=100, entry_price=100.1, quantity=1,
                         stop=95, target=105, buy_fee=0.1, entry_slippage_cost=0.1,
                         high_watermark=100.1, trailing_stop=None, entry_regime="Bull")
    reason, fill = engine._intraday_exit(position, pd.Series({"open": 100, "high": 106, "low": 94}))
    assert reason == "STOP_LOSS" and fill == 95
    reason, fill = engine._intraday_exit(position, pd.Series({"open": 107, "high": 109, "low": 106}))
    assert reason == "TARGET_REACHED" and fill == 107
    assert engine._slipped(100, "buy") > 100
    assert engine._slipped(100, "sell") < 100

