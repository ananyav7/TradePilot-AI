"""Database model, constraint, relationship, repository, and transaction tests."""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from database.connection import Base
from database.models import (
    Approvals, Indicators, MarketData, NotificationLogs, PaperOrders, Portfolio,
    Positions, Stock, SystemLogs, TradeHistory, TradeSignals, TradeStatusChanges,
    Watchlists, WatchlistStocks,
)
from database.repositories import PaperOrderRepository


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as db:
        yield db
    engine.dispose()


def test_all_models_persist_and_relationships_work(session: Session) -> None:
    stock = Stock(symbol="TEST", exchange="NSE", name="Test Ltd")
    portfolio = Portfolio(name="Primary", starting_capital=Decimal("3000.00"))
    watchlist = Watchlists(name="Core")
    session.add_all([stock, portfolio, watchlist])
    session.flush()

    candle_time = datetime(2026, 11, 15, 4, 0, tzinfo=timezone.utc)
    candle = MarketData(stock=stock, interval="1d", market_date=date(2026, 11, 15), candle_at=candle_time,
                       open=100, high=105, low=99, close=103, volume=1000, source="test")
    indicator = Indicators(stock=stock, interval="1d", market_date=date(2026, 11, 15), candle_at=candle_time,
                           name="EMA20", value=Decimal("102.4"))
    signal = TradeSignals(stock=stock, signal_type="BUY", rationale="fixture")
    session.add_all([candle, indicator, signal, SystemLogs(level="INFO", event="test", message="ok"),
                     NotificationLogs(channel="telegram", status="queued", message="test")])
    watchlist.members.append(WatchlistStocks(stock=stock))
    session.flush()
    approval = Approvals(signal=signal, status="approved", decision_at=candle_time, decided_by="user")
    order = PaperOrders(approval=approval, side="BUY", quantity=1, status="created")
    position = Positions(portfolio=portfolio, stock=stock, quantity=1, average_entry_price=103)
    session.add_all([approval, order, position])
    session.flush()
    history = TradeHistory(paper_order=order, position=position, event_type="filled", quantity=1, price=103)
    session.add(history)
    session.commit()

    assert session.scalar(select(Stock).where(Stock.symbol == "TEST")).market_data[0].close == Decimal("103.0000")
    assert session.scalar(select(TradeSignals)).approval.paper_order.id == order.id
    assert session.scalar(select(Positions)).portfolio.name == "Primary"
    assert session.scalar(select(Watchlists)).members[0].stock.name == "Test Ltd"
    assert session.scalar(select(TradeHistory)).position.id == position.id
    assert session.scalar(select(Indicators)).candle_at.tzinfo == timezone.utc
    assert session.scalar(select(SystemLogs)).event == "test"
    assert session.scalar(select(NotificationLogs)).channel == "telegram"


def test_candle_approval_and_watchlist_duplicates_are_rejected(session: Session) -> None:
    stock = Stock(symbol="DUP", name="Duplicate Test")
    watchlist = Watchlists(name="Unique list")
    session.add_all([stock, watchlist])
    session.flush()
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    session.add(MarketData(stock=stock, interval="1d", market_date=date(2026, 1, 1), candle_at=at,
                           open=1, high=1, low=1, close=1, volume=1))
    session.commit()
    session.add(MarketData(stock_id=stock.id, interval="1d", market_date=date(2026, 1, 1), candle_at=at,
                           open=1, high=1, low=1, close=1, volume=1))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()

    signal = TradeSignals(stock=stock, signal_type="BUY")
    session.add(signal)
    session.flush()
    session.add(Approvals(signal_id=signal.id))
    session.commit()
    session.add(Approvals(signal_id=signal.id))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()

    session.add(WatchlistStocks(watchlist_id=watchlist.id, stock_id=stock.id))
    session.commit()
    session.add(WatchlistStocks(watchlist_id=watchlist.id, stock_id=stock.id))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_foreign_keys_are_enforced_and_order_status_changes_are_audited(session: Session) -> None:
    session.add(MarketData(stock_id=12345, interval="1d", market_date=date.today(),
                           candle_at=datetime.now(timezone.utc), open=1, high=1, low=1, close=1, volume=1))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()

    stock = Stock(symbol="AUDIT", name="Audit Test")
    session.add(stock)
    session.flush()
    signal = TradeSignals(stock=stock, signal_type="BUY")
    approval = Approvals(signal=signal, status="approved")
    order = PaperOrders(approval=approval, side="BUY", quantity=1, status="created")
    session.add(order)
    session.commit()
    PaperOrderRepository(session).set_status(order.id, "filled", "paper fill")
    changes = session.scalars(select(TradeStatusChanges).where(TradeStatusChanges.paper_order_id == order.id)).all()
    assert len(changes) == 1
    assert (changes[0].previous_status, changes[0].new_status, changes[0].reason) == ("created", "filled", "paper fill")


def test_duplicate_paper_order_for_one_approval_is_rejected(session: Session) -> None:
    stock = Stock(symbol="ORDERDUP", name="Duplicate Order Test")
    signal = TradeSignals(stock=stock, signal_type="BUY")
    approval = Approvals(signal=signal, status="approved")
    session.add(PaperOrders(approval=approval, side="BUY", quantity=1, status="APPROVED"))
    session.commit()
    session.add(PaperOrders(approval_id=approval.id, side="BUY", quantity=1, status="APPROVED"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
