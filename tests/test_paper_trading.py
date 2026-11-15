from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from database.connection import Base
from database.models import (Approvals, PaperOrders, Positions, Stock,
                             TradeHistory, TradeSignals, TradeStatusChanges)
from execution.order_manager import PaperOrderStatus
from execution.paper_engine import (DailyMarketBar, ExecutionQuote,
                                    PaperExecutionError, PaperTradingEngine)
from execution.signal_version import signal_version
from risk.controls import KillSwitch
from risk.engine import RiskConfig


NOW = datetime(2026, 11, 15, 10, tzinfo=timezone.utc)


@pytest.fixture
def paper_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


def seed_approved_signal(session: Session):
    stock = Stock(symbol="PAPER", exchange="NSE", name="Paper Test Ltd")
    session.add(stock)
    session.flush()
    signal = TradeSignals(stock_id=stock.id, signal_key="paper-signal", signal_type="BUY",
                          status="pending", reference_price=Decimal("100"), stop_loss=Decimal("95"),
                          target_price=Decimal("110"), expires_at=NOW + timedelta(days=1))
    session.add(signal)
    session.flush()
    approval = Approvals(signal_id=signal.id, status="approved", decision_at=NOW,
                         approved_quantity=100, signal_version=signal_version(signal))
    session.add(approval)
    session.commit()
    return stock, signal, approval


def make_engine(session, **kwargs):
    return PaperTradingEngine(session, risk_config=RiskConfig(starting_capital="3000",
                                                              risk_per_trade="0.005"),
                              slippage_bps="10", clock=lambda: NOW, **kwargs)


def quote(price="100", *, bid=None, ask=None, at=NOW, source="test quote"):
    return ExecutionQuote(price, source, at, bid=bid, ask=ask)


def buy_position(engine, session, approval, quantity=2):
    order = engine.create_buy_order(approval.id, quantity, stop_loss="95", target_price="110")
    assert order.status == PaperOrderStatus.PENDING
    engine.approve_order(order.id, approved_by="tester")
    return order


def test_approved_buy_partial_fills_update_cash_average_cost_and_restore(paper_session):
    stock, _, approval = seed_approved_signal(paper_session)
    engine = make_engine(paper_session)
    order = buy_position(engine, paper_session, approval, 2)
    first = engine.fill_order(order.id, quote("100", bid="99.9", ask="100.1"), quantity=1)
    assert first.status == PaperOrderStatus.PARTIALLY_FILLED
    second = engine.fill_order(order.id, quote("101", bid="100.9", ask="101"), quantity=1)
    assert second.status == PaperOrderStatus.FILLED
    position = paper_session.get(Positions, first.position_id)
    assert position.quantity == 2
    expected_average = (first.fill_price + second.fill_price) / 2
    assert Decimal(position.average_entry_price) == pytest.approx(expected_average)
    snapshot = engine.portfolio_snapshot({stock.id: "105"})
    assert snapshot.initial_capital == Decimal("3000.00")
    assert snapshot.available_cash < snapshot.initial_capital
    assert snapshot.invested_capital == Decimal(position.average_entry_price) * 2
    assert snapshot.unrealized_pnl == pytest.approx((Decimal("105") - Decimal(position.average_entry_price)) * 2)
    assert snapshot.realized_pnl == Decimal("0.00")
    # Recreate the engine from persisted rows, as on an application restart.
    order_states = paper_session.scalars(select(TradeStatusChanges).where(
        TradeStatusChanges.paper_order_id == order.id).order_by(TradeStatusChanges.id)).all()
    assert [state.new_status for state in order_states] == ["PENDING", "APPROVED", "PARTIALLY_FILLED", "FILLED"]
    with Session(paper_session.get_bind(), expire_on_commit=False) as restarted_session:
        restarted = make_engine(restarted_session)
        restored = restarted.portfolio_snapshot({stock.id: "105"})
        assert restored.available_cash == snapshot.available_cash
        assert restored.positions[0].quantity == 2
        assert restored.positions[0].average_entry_price == position.average_entry_price


def test_partial_and_full_manual_exits_realize_pnl_and_preserve_cash_ledger(paper_session):
    stock, _, approval = seed_approved_signal(paper_session)
    engine = make_engine(paper_session)
    order = buy_position(engine, paper_session, approval, 2)
    buy = engine.fill_order(order.id, quote("100"))
    pos = paper_session.get(Positions, buy.position_id)
    partial = engine.manual_exit(pos.id, quote("110", bid="109.9", ask="110.1"), quantity=1)
    assert partial.status == PaperOrderStatus.FILLED
    still_open = paper_session.get(Positions, pos.id)
    assert still_open.status == "open" and still_open.quantity == 1
    mid = engine.portfolio_snapshot({stock.id: "108"})
    assert mid.realized_pnl > Decimal("0")
    assert mid.unrealized_pnl is not None
    final = engine.manual_exit(pos.id, quote("108", bid="107.9", ask="108.1"))
    assert final.status == PaperOrderStatus.FILLED
    closed = paper_session.get(Positions, pos.id)
    assert closed.status == "closed" and closed.quantity == 0
    snapshot = engine.portfolio_snapshot()
    assert snapshot.open_positions == 0
    assert snapshot.unrealized_pnl == Decimal("0.00")
    assert snapshot.available_cash == Decimal("3000.00") + snapshot.realized_pnl
    sells = paper_session.scalars(select(TradeHistory).where(TradeHistory.event_type == "SELL_FILL")).all()
    assert len(sells) == 2


def test_buy_requires_approval_and_invalid_states_are_rejected(paper_session):
    stock, signal, approval = seed_approved_signal(paper_session)
    engine = make_engine(paper_session)
    order = engine.create_buy_order(approval.id, 1, stop_loss=95, target_price=110)
    with pytest.raises(ValueError, match="approved"):
        engine.fill_order(order.id, quote())
    engine.approve_order(order.id, approved_by="tester")
    with pytest.raises(ValueError, match="Invalid paper-order transition"):
        engine.approve_order(order.id, approved_by="tester")
    engine.reject_order(order.id, reason="manual rejection")
    assert paper_session.get(PaperOrders, order.id).status == PaperOrderStatus.REJECTED
    with pytest.raises(LookupError):
        engine.create_buy_order(99999, 1, stop_loss=95, target_price=110)


def test_cash_and_risk_limits_reject_without_mutating_order_or_portfolio(paper_session):
    _, _, approval = seed_approved_signal(paper_session)
    engine = make_engine(paper_session)
    order = buy_position(engine, paper_session, approval, 100)
    with pytest.raises(PaperExecutionError):
        engine.fill_order(order.id, quote("100"))
    assert paper_session.get(PaperOrders, order.id).filled_quantity == 0
    snapshot = engine.portfolio_snapshot()
    assert snapshot.available_cash == Decimal("3000.00")
    assert snapshot.open_positions == 0


def test_only_one_distinct_open_position_is_allowed(paper_session):
    stock, _, approval = seed_approved_signal(paper_session)
    engine = make_engine(paper_session)
    first = buy_position(engine, paper_session, approval, 1)
    first_fill = engine.fill_order(first.id, quote("100"))
    other_stock = Stock(symbol="OTHER", exchange="NSE", name="Other Ltd")
    paper_session.add(other_stock)
    paper_session.flush()
    other_signal = TradeSignals(stock_id=other_stock.id, signal_key="other-signal", signal_type="BUY",
                                expires_at=NOW + timedelta(days=1))
    paper_session.add(other_signal)
    paper_session.flush()
    other_approval = Approvals(signal_id=other_signal.id, status="approved", decision_at=NOW,
                               approved_quantity=100, signal_version=signal_version(other_signal))
    paper_session.add(other_approval)
    paper_session.commit()
    second = engine.create_buy_order(other_approval.id, 1, stop_loss=95, target_price=110)
    engine.approve_order(second.id, approved_by="tester")
    with pytest.raises(PaperExecutionError, match="Maximum one open position"):
        engine.fill_order(second.id, quote("100"))
    assert paper_session.get(Positions, first_fill.position_id).status == "open"
    assert paper_session.get(PaperOrders, second.id).filled_quantity == 0


def test_stop_monitor_handles_gap_through_and_records_filled_exit(paper_session):
    stock, _, approval = seed_approved_signal(paper_session)
    engine = make_engine(paper_session)
    order = buy_position(engine, paper_session, approval, 1)
    buy = engine.fill_order(order.id, quote("100"))
    bar = DailyMarketBar(stock.id, open="90", high="92", low="88", close="91",
                         source="completed daily test bar", observed_at=NOW)
    stopped = engine.monitor_stops(bar)
    assert stopped is not None
    assert stopped.fill_price < Decimal("90")  # Gap-open and adverse slippage, not trigger-price guarantee.
    assert stopped.status == PaperOrderStatus.FILLED
    position = paper_session.get(Positions, buy.position_id)
    assert position.status == "closed"
    order_record = paper_session.get(PaperOrders, stopped.order_id)
    assert order_record.reason == "STOP_LOSS"


def test_protective_exit_cancels_unfilled_buy_remainder(paper_session):
    stock, _, approval = seed_approved_signal(paper_session)
    engine = make_engine(paper_session)
    buy = buy_position(engine, paper_session, approval, 2)
    fill = engine.fill_order(buy.id, quote("100"), quantity=1)
    assert fill.status == PaperOrderStatus.PARTIALLY_FILLED
    engine.monitor_stops(DailyMarketBar(stock.id, 90, 92, 88, 91,
                                        "completed daily feed", NOW))
    assert paper_session.get(PaperOrders, buy.id).status == PaperOrderStatus.CANCELLED


def test_target_monitor_fills_at_observed_gap_open_and_uses_sell_slippage(paper_session):
    stock, _, approval = seed_approved_signal(paper_session)
    engine = make_engine(paper_session)
    order = buy_position(engine, paper_session, approval, 1)
    buy = engine.fill_order(order.id, quote("100"))
    bar = DailyMarketBar(stock.id, open="112", high="114", low="111", close="113",
                         source="completed daily test bar", observed_at=NOW)
    exited = engine.monitor_stops(bar)
    assert exited is not None and exited.fill_price < Decimal("112")
    assert paper_session.get(PaperOrders, exited.order_id).reason == "TARGET_REACHED"
    assert paper_session.get(Positions, buy.position_id).status == "closed"


def test_stale_quotes_incomplete_bars_and_bad_spreads_are_rejected(paper_session):
    stock, _, approval = seed_approved_signal(paper_session)
    engine = make_engine(paper_session)
    order = buy_position(engine, paper_session, approval, 1)
    with pytest.raises(ValueError, match="stale"):
        engine.fill_order(order.id, quote(at=NOW - timedelta(minutes=1)))
    with pytest.raises(ValueError, match="bid cannot exceed ask"):
        engine.fill_order(order.id, quote(bid=101, ask=100))
    with pytest.raises(ValueError, match="completed"):
        engine.monitor_stops(DailyMarketBar(stock.id, 100, 101, 99, 100,
                                            "feed", NOW, completed=False))
    assert paper_session.get(PaperOrders, order.id).status == PaperOrderStatus.APPROVED


def test_kill_switch_blocks_buys_but_does_not_disable_protective_sells(paper_session, tmp_path):
    stock, _, approval = seed_approved_signal(paper_session)
    switch = KillSwitch(tmp_path / "kill.json")
    engine = make_engine(paper_session, kill_switch=switch)
    order = buy_position(engine, paper_session, approval, 1)
    engine.fill_order(order.id, quote("100"))
    other_stock = Stock(symbol="KILLTEST", exchange="NSE", name="Kill Test Ltd")
    paper_session.add(other_stock)
    paper_session.flush()
    other_signal = TradeSignals(stock_id=other_stock.id, signal_key="kill-signal", signal_type="BUY",
                                expires_at=NOW + timedelta(days=1))
    paper_session.add(other_signal)
    paper_session.flush()
    other_approval = Approvals(signal_id=other_signal.id, status="approved", decision_at=NOW,
                               approved_quantity=100, signal_version=signal_version(other_signal))
    paper_session.add(other_approval)
    paper_session.commit()
    blocked_order = buy_position(engine, paper_session, other_approval, 1)
    switch.activate("manual risk stop")
    with pytest.raises(PaperExecutionError, match="kill switch"):
        engine.fill_order(blocked_order.id, quote("100"))
    protective = engine.monitor_stops(DailyMarketBar(stock.id, 90, 92, 88, 91,
                                                       "completed daily feed", NOW))
    assert protective is not None
    assert paper_session.get(Positions, protective.position_id).status == "closed"
    assert paper_session.get(PaperOrders, blocked_order.id).filled_quantity == 0


def test_cancel_expire_and_audit_transitions_are_persisted(paper_session):
    stock, signal, approval = seed_approved_signal(paper_session)
    engine = make_engine(paper_session)
    cancelled = engine.create_buy_order(approval.id, 1, stop_loss=95, target_price=110)
    engine.cancel_order(cancelled.id, reason="user cancelled")
    later_signal = TradeSignals(stock_id=stock.id, signal_key="paper-signal-2", signal_type="BUY",
                                 status="pending", expires_at=NOW + timedelta(days=1))
    paper_session.add(later_signal)
    paper_session.flush()
    later_approval = Approvals(signal_id=later_signal.id, status="approved", decision_at=NOW,
                               approved_quantity=100, signal_version=signal_version(later_signal))
    paper_session.add(later_approval)
    paper_session.commit()
    expired = engine.create_buy_order(later_approval.id, 1, stop_loss=95, target_price=110)
    engine.expire_order(expired.id, reason="time in force elapsed")
    assert paper_session.get(PaperOrders, cancelled.id).status == PaperOrderStatus.CANCELLED
    assert paper_session.get(PaperOrders, expired.id).status == PaperOrderStatus.EXPIRED
    states = paper_session.scalars(select(TradeStatusChanges).where(
        TradeStatusChanges.paper_order_id == cancelled.id).order_by(TradeStatusChanges.id)).all()
    assert [state.new_status for state in states] == ["PENDING", "CANCELLED"]
    assert states[-1].reason == "user cancelled"
