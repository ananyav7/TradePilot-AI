"""End-to-end simulated research-to-paper-trade workflow on an isolated SQLite file."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from dataclasses import replace
from decimal import Decimal
import json

import pandas as pd
import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from database.connection import Base
from database.models import (
    Approvals, Indicators, MarketData, NotificationLogs, PaperOrders, Positions,
    Stock, TradeHistory, TradeSignals, TradeStatusChanges,
)
from config.settings import Settings
from execution.approval_manager import ApprovalManager
from execution.paper_engine import ExecutionQuote, PaperExecutionError, PaperTradingEngine
from integrations.upstox.common import MarketDataSnapshot
from notifications.telegram import TelegramNotifier
from portfolio.portfolio_manager import PortfolioManager
from reports.service import ReportingService
from risk.engine import RiskConfig, RiskEngine, RiskRequest, estimate_sell_cost
from strategy.scanner import StockScanner
from strategy.configuration import load_strategy_config
from strategy.swing_strategy import SwingStrategyEngine
from strategy.technical_analysis import TechnicalAnalysisEngine
from tests.test_technical_analysis import sample_ohlcv


NOW = datetime(2026, 11, 15, 10, tzinfo=timezone.utc)
SIM_SOURCE = "simulated deterministic E2E candles"


class SimulatedCandleProvider:
    """Deterministic historical-data adapter; it has no network or broker client."""

    def __init__(self, rows: list[list[object]]) -> None:
        self.rows = rows
        self.calls: list[str] = []

    def historical_candles(self, instrument_key, unit, interval, to_date, from_date=None):
        self.calls.append(instrument_key)
        rows = [] if instrument_key.endswith("50") else self.rows
        return MarketDataSnapshot(rows, NOW, SIM_SOURCE, "historical", False)


class NoNetworkPoster:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def post(self, url: str, *, json: dict, timeout: float):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        raise AssertionError("Telegram network must remain disabled during this simulation")


def simulated_frame() -> pd.DataFrame:
    frame = sample_ohlcv(80)
    frame.index = pd.date_range(end="2026-11-14", periods=80, freq="D", tz="Asia/Kolkata")
    frame.iloc[-1, frame.columns.get_loc("volume")] = 3000
    frame.iloc[-1, frame.columns.get_loc("close")] -= 0.8
    frame.iloc[-1, frame.columns.get_loc("high")] = max(frame.iloc[-1].open, frame.iloc[-1].close) + 1
    frame.iloc[-1, frame.columns.get_loc("low")] = min(frame.iloc[-1].open, frame.iloc[-1].close) - 0.8
    return frame


def as_upstox_rows(frame: pd.DataFrame) -> list[list[object]]:
    return [[stamp.isoformat(), row.open, row.high, row.low, row.close, int(row.volume)]
            for stamp, row in frame.iterrows()]


@pytest.fixture
def sqlite_store(tmp_path):
    """A real file-backed SQLite database with FK checks, removed with tmp_path."""
    db_path = tmp_path / "tradepilot_e2e.sqlite3"
    test_engine = create_engine(f"sqlite:///{db_path.as_posix()}", connect_args={"check_same_thread": False})

    @event.listens_for(test_engine, "connect")
    def enforce_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(test_engine)
    factory = sessionmaker(test_engine, expire_on_commit=False)
    yield db_path, test_engine, factory
    test_engine.dispose()


def test_complete_simulated_nifty_to_paper_trade_restart_workflow(sqlite_store):
    db_path, db_engine, factory = sqlite_store
    no_network = NoNetworkPoster()
    notifier = TelegramNotifier(session=None, settings=Settings(_env_file=None), http=no_network)
    risk_config = RiskConfig(
        starting_capital=Decimal("3000"), risk_per_trade=Decimal("0.005"),
        max_open_positions=1, max_market_data_age=timedelta(days=1),
    )
    engine = PaperTradingEngine(factory(), risk_config=risk_config, slippage_bps="10",
                                clock=lambda: NOW, notifier=notifier)
    session: Session = engine.session
    notifier.session = session

    # 1–2: Application dependencies are instantiated and the simulated NIFTY universe is loaded.
    stocks = [Stock(symbol=f"SIM{i:02d}", exchange="NSE", name=f"Simulated Constituent {i:02d}",
                    instrument_key=f"NSE_EQ|SIM{i:02d}", instrument_available=True,
                    in_nifty50=True, membership_source="E2E fixture", active=True)
              for i in range(1, 51)]
    session.add_all(stocks)
    session.commit()
    assert session.scalar(select(func.count(Stock.id)).where(Stock.in_nifty50.is_(True))) == 50

    # 3–5: Retrieve simulated OHLCV, persist candles, calculate indicators, scan every eligible stock.
    frame = simulated_frame()
    rows = as_upstox_rows(frame)
    provider = SimulatedCandleProvider(rows)
    results = StockScanner(provider).scan(stocks, to_date=date(2026, 11, 14))
    assert len(results) == 50
    assert len(provider.calls) == 50
    assert sum(result.data_complete for result in results) == 49
    assert results[-1].status == "Insufficient data"
    assert all(result.data_source == SIM_SOURCE for result in results[:-1])
    assert all(result.direction == "Bullish" for result in results[:-1])
    assert results[0].ema20 > results[0].ema50
    assert 50 <= results[0].rsi14 <= 65
    assert results[0].volume_ratio > 1.2

    chosen = stocks[:3]
    for stock in chosen[:2]:
        for stamp, row in frame.iterrows():
            session.add(MarketData(
                stock_id=stock.id, interval="1d", market_date=stamp.date(), candle_at=stamp.to_pydatetime(),
                open=row.open, high=row.high, low=row.low, close=row.close, volume=int(row.volume),
                source="simulated",
            ))
    analysis = TechnicalAnalysisEngine().analyze(
        frame, completed_through=date(2026, 11, 14), source=SIM_SOURCE)
    latest = analysis.frame.iloc[-1]
    for name in ("ema20", "ema50", "rsi14", "atr14", "volume_sma20", "relative_volume"):
        session.add(Indicators(stock_id=chosen[0].id, interval="1d",
                               market_date=date(2026, 11, 14), candle_at=analysis.latest.timestamp.to_pydatetime(),
                               name=name, value=Decimal(str(latest[name]))))
    strategy_config = load_strategy_config()
    strategy_config = replace(
        strategy_config, version="e2e-v1",
        exits=replace(strategy_config.exits, target_atr_multiple=4.0),
    )
    strategy = SwingStrategyEngine(strategy_config)
    signals = [strategy.generate_and_store(session, stock, analysis) for stock in chosen]
    session.commit()
    assert len(signals) == 3 and all(signal.signal_type == "BUY" for signal in signals)
    assert session.scalar(select(func.count(MarketData.id))) == 160
    assert session.scalar(select(func.count(Indicators.id))) == 6
    assert session.scalar(select(func.count(TradeSignals.id))) == 3

    # 6–10: Deterministic risk check and opportunity detail review; malformed and stale proposals fail closed.
    quote = ExecutionQuote(str(signals[0].entry_reference_price), SIM_SOURCE, NOW)
    review_manager = ApprovalManager(session, engine=engine, notifier=notifier, clock=lambda: NOW)
    first_review = review_manager.review(signals[0].signal_id, quote)
    second_review = review_manager.review(signals[1].signal_id, quote)
    assert first_review.quantity > 0 and first_review.risk_amount <= Decimal("15.00")
    assert first_review.risk_reward >= Decimal("1.5")
    assert first_review.explanation and first_review.signal_version.startswith("e2e-v1:")
    with pytest.raises(ValueError, match="already has"):
        review_manager.review(signals[0].signal_id, quote)
    with pytest.raises(ValueError, match="stale"):
        review_manager.review(signals[2].signal_id,
                              ExecutionQuote(str(signals[2].entry_reference_price), SIM_SOURCE,
                                             NOW - timedelta(minutes=2)))

    invalid = TradeSignals(stock_id=chosen[2].id, signal_key="invalid-no-stop", signal_type="BUY",
                           status="pending", reference_price=Decimal("100"), target_price=Decimal("120"),
                           expires_at=NOW + timedelta(days=1))
    session.add(invalid)
    session.commit()
    with pytest.raises(PaperExecutionError, match="stop-loss or target"):
        review_manager.review(invalid.signal_key, quote)
    insufficient_cash = RiskEngine(risk_config).assess(RiskRequest(
        entry_price="100", stop_loss="95", target_price="110", available_capital="3000",
        available_cash="0", market_data_at=NOW, as_of=NOW))
    assert not insufficient_cash.accepted
    assert {"required_capital_exceeds_available_cash", "quantity_is_zero"} <= set(insufficient_cash.reasons)

    # 11–12: Explicitly approve one signal and reject another; rejection creates no order.
    approval, order_id = review_manager.decide(
        first_review.approval_id, decision="approve", expected_signal_version=first_review.signal_version,
        confirm_text=f"APPROVE {signals[0].signal_id}", quote=quote,
        quantity=first_review.quantity, actor="e2e-user")
    rejected, rejected_order_id = review_manager.decide(
        second_review.approval_id, decision="reject", expected_signal_version=second_review.signal_version,
        confirm_text=f"REJECT {signals[1].signal_id}", quote=None,
        actor="e2e-user", comment="Rejected during E2E scenario")
    assert approval.status == "approved" and order_id is not None
    assert rejected.status == "rejected" and rejected_order_id is None
    assert session.scalar(select(func.count(PaperOrders.id))) == 1
    order = session.get(PaperOrders, order_id)
    assert order is not None and order.status == "APPROVED"
    with pytest.raises(IntegrityError):
        engine.create_buy_order(approval.id, approval.approved_quantity,
                                stop_loss=approval.decision_stop_loss,
                                target_price=approval.decision_target_price)
    session.rollback()
    assert session.scalar(select(func.count(PaperOrders.id))) == 1
    with pytest.raises(ValueError, match="risk-qualified approved quantity"):
        engine.create_buy_order(approval.id, approval.approved_quantity + 1,
                                stop_loss=approval.decision_stop_loss,
                                target_price=approval.decision_target_price)

    # 13–18: Simulate fill, cash/position/mark updates, and an explicit simulated manual exit.
    buy_fill = engine.fill_order(order_id, quote, quantity=approval.approved_quantity)
    buy_history = session.scalar(select(TradeHistory).where(
        TradeHistory.paper_order_id == order_id, TradeHistory.event_type == "BUY_FILL"))
    buy_details = json.loads(buy_history.details)
    position = session.get(Positions, buy_fill.position_id)
    assert buy_fill.status == "FILLED"
    assert buy_fill.quantity == 3
    assert buy_fill.fill_price == Decimal("115.3152")
    assert position.quantity == approval.approved_quantity
    snapshot_after_buy = engine.portfolio_snapshot()
    assert snapshot_after_buy.available_cash == Decimal("3000.00") + Decimal(buy_details["cash_delta"]).quantize(Decimal("0.01"))
    assert snapshot_after_buy.available_cash < Decimal("3000.00")
    assert snapshot_after_buy.available_cash == Decimal("2653.86")
    assert snapshot_after_buy.positions[0].quantity == approval.approved_quantity

    mark = buy_fill.fill_price + Decimal("1.00")
    marked = PortfolioManager(session, risk_config).snapshot({chosen[0].id: mark})
    assert marked.unrealized_pnl == (mark - Decimal(position.average_entry_price)) * position.quantity

    exit_quote = ExecutionQuote(str(signals[0].target + 2), SIM_SOURCE, NOW,
                                bid=str(signals[0].target + 1), ask=str(signals[0].target + 2))
    sell_fill = engine.manual_exit(position.id, exit_quote, approved_by="e2e-user")
    assert sell_fill.status == "FILLED" and sell_fill.quantity == approval.approved_quantity
    assert session.get(Positions, position.id).status == "closed"
    sell_history = session.scalar(select(TradeHistory).where(
        TradeHistory.paper_order_id == sell_fill.order_id, TradeHistory.event_type == "SELL_FILL"))
    sell_details = json.loads(sell_history.details)
    expected_sell_fee = estimate_sell_cost(sell_fill.fill_price, sell_fill.quantity, engine.fees)
    expected_net = (sell_fill.fill_price - buy_fill.fill_price) * sell_fill.quantity \
        - expected_sell_fee - Decimal(buy_details["buy_fee"])
    assert Decimal(sell_details["sell_fee"]) == expected_sell_fee
    assert expected_sell_fee == Decimal("0.52")
    assert sell_fill.fill_price == Decimal("124.5288")
    assert Decimal(sell_details["realized_net_pnl"]) == expected_net
    assert expected_net == Decimal("26.9308")
    final_snapshot = engine.portfolio_snapshot()
    assert final_snapshot.unrealized_pnl == Decimal("0.00")
    assert final_snapshot.realized_pnl == expected_net.quantize(Decimal("0.01"))
    assert final_snapshot.available_cash == Decimal("3000.00") + final_snapshot.realized_pnl
    assert final_snapshot.available_cash == Decimal("3026.93")

    # 19–20: Trade journal and daily paper report are derived from persisted fill rows.
    report_service = ReportingService(session)
    journal = report_service.paper_trades()
    daily = report_service.daily_report(date(2026, 11, 15))
    assert len(journal) == 1 and journal.iloc[0]["symbol"] == chosen[0].symbol
    assert journal.iloc[0]["market_data_classification"] == "SIMULATED_MARKET_DATA"
    assert daily.result_type == "PAPER_TRADING_RESULT" and len(daily.data) == 1
    assert daily.data.iloc[0]["net_pnl_inr"] == pytest.approx(float(expected_net))

    # 21–22: Close all sessions and reopen the file DB to model application restart.
    order_states_before = session.scalars(select(TradeStatusChanges).where(
        TradeStatusChanges.paper_order_id.in_([order_id, sell_fill.order_id])).order_by(
            TradeStatusChanges.paper_order_id, TradeStatusChanges.id)).all()
    assert [row.new_status for row in order_states_before if row.paper_order_id == order_id] == ["PENDING", "APPROVED", "FILLED"]
    assert [row.new_status for row in order_states_before if row.paper_order_id == sell_fill.order_id] == ["PENDING", "APPROVED", "FILLED"]
    assert session.scalar(select(func.count(TradeHistory.id))) == 2
    assert session.scalar(select(func.count(Approvals.id))) == 2
    session.close()
    with factory() as restarted:
        restored = PaperTradingEngine(restarted, risk_config=risk_config, slippage_bps="10",
                                      clock=lambda: NOW, notifier=notifier)
        restored_snapshot = restored.portfolio_snapshot()
        assert restored_snapshot.available_cash == final_snapshot.available_cash
        assert restored_snapshot.realized_pnl == final_snapshot.realized_pnl
        assert restored_snapshot.open_positions == 0
        assert restarted.scalar(select(func.count(TradeSignals.id))) == 4
        assert restarted.scalar(select(func.count(Approvals.id))) == 2
        assert restarted.scalar(select(func.count(PaperOrders.id))) == 2
        assert restarted.scalar(select(func.count(TradeHistory.id))) == 2
        assert restarted.scalar(select(func.count(TradeStatusChanges.id))) == 6
        # Approval rows, rejection decision, fills, and lifecycle transitions are the audit ledger.
        assert restarted.scalar(select(Approvals).where(Approvals.id == second_review.approval_id)).status == "rejected"
        notice_rows = restarted.scalars(select(NotificationLogs).order_by(NotificationLogs.id)).all()
        assert len(notice_rows) == 6
        notice_events = {row.message.split("]", 1)[0].lstrip("[") for row in notice_rows}
        assert notice_events >= {"new_opportunity", "approval_received", "trade_rejected", "paper_order_completed"}
        assert all(row.status == "disabled" for row in notice_rows)
        assert no_network.calls == []
        assert {row.side for row in restarted.scalars(select(PaperOrders)).all()} == {"BUY", "SELL"}

    # Every generated order belongs to the local paper ledger; no broker adapter is present in this flow.
    assert not hasattr(engine, "client")
    assert db_path.exists() and db_engine.dialect.name == "sqlite"
