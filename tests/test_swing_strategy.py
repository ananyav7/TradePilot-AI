"""Business-rule, exit, configuration, and database-idempotency tests."""

from dataclasses import replace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from database.connection import Base
from database.models import Stock, TradeSignals
from strategy.configuration import EntryConfig, MarketFilterConfig, StrategyConfig, load_strategy_config
from strategy.technical_analysis import TechnicalAnalysisEngine
from strategy.swing_strategy import IncompleteCandleError, OpenPositionContext, SwingStrategyEngine
from tests.test_technical_analysis import sample_ohlcv


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        yield db
    engine.dispose()


def qualifying_analysis(*, close_shift: float = -0.8):
    frame = sample_ohlcv(80)
    frame.iloc[-1, frame.columns.get_loc("volume")] = 3000
    frame.iloc[-1, frame.columns.get_loc("close")] += close_shift
    frame.iloc[-1, frame.columns.get_loc("high")] = max(frame.iloc[-1].open, frame.iloc[-1].close) + 1
    frame.iloc[-1, frame.columns.get_loc("low")] = min(frame.iloc[-1].open, frame.iloc[-1].close) - 0.8
    market_date = frame.index[-1].tz_convert("Asia/Kolkata").date()
    return TechnicalAnalysisEngine().analyze(frame, completed_through=market_date, source="test completed EOD")


def saved_stock(session: Session) -> Stock:
    stock = Stock(symbol="ALPHA", name="Alpha Ltd", exchange="NSE", active=True)
    session.add(stock)
    session.flush()
    return stock


def test_config_loads_user_toml_and_rejects_invalid_thresholds(tmp_path):
    config = load_strategy_config()
    assert config.entry.rsi_min == 50
    assert config.entry.rsi_max == 65
    assert config.entry.relative_volume_min_exclusive == 1.2
    invalid = tmp_path / "bad.toml"
    invalid.write_text("""[strategy]
name = "x"
version = "x"
signal_expiry_calendar_days = 1
[entry]
rsi_min = 70
rsi_max = 50
relative_volume_min_exclusive = 1.2
[market_filters]
allowed_trends = ["Bullish"]
allowed_volatility = ["Normal"]
allowed_momentum = ["Neutral"]
allowed_volume_activity = ["Typical"]
min_atr_percent = 0.0
max_atr_percent = 4.0
require_close_above_support = false
[exits]
stop_atr_multiple = 2.0
target_atr_multiple = 3.0
trailing_stop_enabled = true
trailing_atr_multiple = 2.0
invalidate_when_ema20_below_ema50 = true
invalidate_when_close_below_ema20 = true
invalidate_when_rsi_below = 45.0
""", encoding="utf-8")
    with pytest.raises(ValueError, match="RSI bounds"):
        StrategyConfig.from_file(invalid)


def test_qualifying_completed_candle_generates_buy_with_explanation_and_levels(session):
    stock = saved_stock(session)
    signal = SwingStrategyEngine().evaluate(stock, qualifying_analysis())
    assert signal.signal_type == "BUY"
    assert signal.recommended_action == "BUY"
    assert signal.symbol == "ALPHA"
    assert signal.signal_id
    assert signal.signal_date == qualifying_analysis().latest.timestamp.tz_convert("Asia/Kolkata").date()
    assert signal.entry_reference_price > signal.ema20 > signal.ema50
    assert signal.stop_loss < signal.entry_reference_price < signal.target
    assert signal.quantity_placeholder is None
    assert signal.rsi14 == pytest.approx(59.57, abs=.02)
    assert signal.volume_ratio > 1.2
    assert signal.risk_reward_ratio == pytest.approx(1.5)
    assert signal.signal_expiration > signal.candle_at
    assert "Entry conditions passed" in signal.strategy_explanation


def test_rejects_incomplete_candle_without_generating_signal(session):
    stock = saved_stock(session)
    analysis = TechnicalAnalysisEngine().analyze(sample_ohlcv(80), end_of_day=False)
    with pytest.raises(IncompleteCandleError):
        SwingStrategyEngine().evaluate(stock, analysis)


def test_no_trade_when_any_entry_or_market_filter_fails(session):
    stock = saved_stock(session)
    base = StrategyConfig()
    rsi_filtered = replace(base, entry=EntryConfig(70, 80, 1.2))
    result = SwingStrategyEngine(rsi_filtered).evaluate(stock, qualifying_analysis())
    assert result.signal_type == "NO TRADE"
    assert "RSI14 is outside" in result.strategy_explanation
    market_filtered = replace(base, market_filters=MarketFilterConfig(allowed_volatility=("Low",)))
    result = SwingStrategyEngine(market_filtered).evaluate(stock, qualifying_analysis())
    assert result.signal_type == "NO TRADE"
    assert "market volatility" in result.strategy_explanation


@pytest.mark.parametrize(("context", "manual_exit", "reason"), [
    (OpenPositionContext(118, 115, 130), False, "STOP_LOSS"),
    (OpenPositionContext(110, 100, 114), False, "TARGET_REACHED"),
    (OpenPositionContext(118, 1, 500), False, "STRATEGY_INVALIDATION"),
    (OpenPositionContext(118, 1, 500, trailing_stop=115), False, "TRAILING_STOP"),
    (OpenPositionContext(118, 1, 500), True, "MANUAL_EXIT"),
])
def test_open_position_exit_reasons(context, manual_exit, reason, session):
    stock = saved_stock(session)
    analysis = qualifying_analysis(close_shift=-8) if reason == "STRATEGY_INVALIDATION" else qualifying_analysis()
    signal = SwingStrategyEngine().evaluate(stock, analysis, position=context, manual_exit=manual_exit)
    assert signal.signal_type == "HOLD"
    assert signal.recommended_action == "EXIT"
    assert signal.exit_reason == reason


def test_same_bar_stop_has_priority_and_trailing_stop_advances_for_next_candle(session):
    stock = saved_stock(session)
    engine = SwingStrategyEngine()
    analysis = qualifying_analysis()
    ambiguous = engine.evaluate(stock, analysis, position=OpenPositionContext(118, 115, 114))
    assert ambiguous.exit_reason == "STOP_LOSS"  # Conservative assumption when both levels are crossed.
    trailing = engine.evaluate(stock, analysis, position=OpenPositionContext(110, 1, 500, high_watermark=110))
    assert trailing.recommended_action == "HOLD"
    assert trailing.next_trailing_stop is not None
    assert trailing.next_high_watermark >= 110


def test_signal_is_stored_once_per_stock_and_completed_candle(session):
    stock = saved_stock(session)
    engine = SwingStrategyEngine()
    analysis = qualifying_analysis()
    first = engine.generate_and_store(session, stock, analysis)
    repeated = engine.generate_and_store(session, stock, analysis)
    assert first.persisted and repeated.persisted
    assert first.signal_id == repeated.signal_id
    assert repeated.signal_type == "BUY"
    assert session.scalar(select(func.count(TradeSignals.id))) == 1
    stored = session.scalar(select(TradeSignals))
    assert stored.stock_id == stock.id
    assert stored.candle_at == analysis.latest.timestamp.to_pydatetime()
    assert stored.stop_loss is not None and stored.target_price is not None
    assert stored.quantity_placeholder is None
