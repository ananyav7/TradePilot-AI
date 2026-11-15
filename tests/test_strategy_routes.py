"""Mocked API route tests for strategy signal generation and persistence."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend import strategy_routes
from database.connection import Base
from database.models import Stock, TradeSignals
from integrations.upstox.common import MarketDataSnapshot
from integrations.upstox.errors import TokenExpiredError
from tests.test_technical_analysis import sample_ohlcv


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        yield db
    engine.dispose()


def test_generation_route_fetches_daily_candles_and_persists_no_trade(session, monkeypatch):
    stock = Stock(symbol="ALPHA", name="Alpha Ltd", exchange="NSE", active=True,
                  instrument_available=True, instrument_key="NSE_EQ|ALPHA")
    session.add(stock)
    session.commit()
    bars = sample_ohlcv(80)
    candles = [[timestamp.isoformat(), row.open, row.high, row.low, row.close, int(row.volume)]
               for timestamp, row in bars.iterrows()]
    calls = []
    monkeypatch.setattr(strategy_routes.upstox_token_manager, "get_token", lambda: "test-token")
    monkeypatch.setattr(strategy_routes.upstox_market_data, "historical_candles",
                        lambda *args: (calls.append(args) or MarketDataSnapshot(
                            candles, datetime.now(timezone.utc), "mock historical", "HISTORICAL", False)))

    response = strategy_routes.generate_strategy_signals(symbol="ALPHA", session=session)

    assert response["generated_count"] == 1
    assert response["signals"][0]["signal_type"] == "NO TRADE"
    assert response["signals"][0]["symbol"] == "ALPHA"
    assert calls[0][0:3] == ("NSE_EQ|ALPHA", "days", 1)
    assert session.scalar(select(func.count(TradeSignals.id))) == 1


def test_generation_route_requires_upstox_authorization(session, monkeypatch):
    monkeypatch.setattr(strategy_routes.upstox_token_manager, "get_token",
                        lambda: (_ for _ in ()).throw(TokenExpiredError("expired")))
    with pytest.raises(strategy_routes.HTTPException) as error:
        strategy_routes.generate_strategy_signals(session=session)
    assert error.value.status_code == 401
