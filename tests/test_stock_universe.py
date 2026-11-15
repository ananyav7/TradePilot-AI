"""Stock universe mapping, change detection, and scanner business-rule tests."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from database.connection import Base
from database.models import Stock, WatchlistStocks
from integrations.upstox.common import MarketDataSnapshot
from integrations.upstox.errors import InvalidResponseError
from integrations.upstox.instrument_master import InstrumentMasterSnapshot, InstrumentRecord
from market_data.universe import (
    NIFTY50_CSV_URL, StockUniverseService, download_official_constituents, parse_constituents_csv,
)
from strategy.scanner import ScannerResult, StockScanner, filter_scan_results


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        yield db
    engine.dispose()


def instrument(symbol: str, key: str | None = None, *, segment: str = "NSE_EQ", kind: str = "EQ") -> InstrumentRecord:
    value = {"instrument_key": key or f"NSE_EQ|{symbol}", "trading_symbol": symbol,
             "name": f"{symbol} Limited", "exchange": "NSE", "segment": segment, "instrument_type": kind}
    return InstrumentRecord(**value, raw=value)


def master(*records: InstrumentRecord) -> InstrumentMasterSnapshot:
    return InstrumentMasterSnapshot(tuple(records), datetime.now(timezone.utc), "mock master", False)


def test_constituent_csv_parses_nse_columns_and_rejects_duplicate_symbols():
    rows = parse_constituents_csv('Company Name,SYMBOL,Industry\nAlpha Limited,ALPHA,IT\nBeta Ltd,BETA,Banking\n')
    assert [(row.symbol, row.name) for row in rows] == [("ALPHA", "Alpha Limited"), ("BETA", "Beta Ltd")]
    with pytest.raises(ValueError, match="Duplicate"):
        parse_constituents_csv("SYMBOL\nALPHA\nalPhA\n")


def test_official_constituent_refresh_requires_fifty_from_verified_nse_csv():
    csv_text = "Company Name,SYMBOL\n" + "".join(f"Company {i},SYM{i}\n" for i in range(50))

    class Response:
        content = csv_text.encode()

        @staticmethod
        def raise_for_status():
            return None

    downloaded, source = download_official_constituents(getter=lambda *args, **kwargs: Response())
    assert len(parse_constituents_csv(downloaded)) == 50
    assert source == NIFTY50_CSV_URL

    class InvalidResponse(Response):
        content = b"SYMBOL\nONLY_ONE\n"

    with pytest.raises(InvalidResponseError, match="exactly 50"):
        download_official_constituents(getter=lambda *args, **kwargs: InvalidResponse())


def test_universe_sync_resolves_only_equity_mapping_and_detects_changes(session):
    manager = StockUniverseService(session)
    current_master = master(instrument("ALPHA"), instrument("BETA", segment="NSE_FO"))
    first = manager.import_csv_text("SYMBOL,Company Name\nALPHA,Alpha Co\nBETA,Beta Co\n",
                                    source="test source", require_fifty=False, master_snapshot=current_master)
    assert first.added == ("ALPHA", "BETA")
    assert first.unavailable == ("BETA",)
    alpha = session.scalar(select(Stock).where(Stock.symbol == "ALPHA"))
    beta = session.scalar(select(Stock).where(Stock.symbol == "BETA"))
    assert alpha.instrument_key == "NSE_EQ|ALPHA" and alpha.instrument_available
    assert beta.instrument_key is None and not beta.instrument_available

    second = manager.import_csv_text("SYMBOL,Company Name\nALPHA,Alpha Co\nGAMMA,Gamma Co\n",
                                     require_fifty=False, master_snapshot=master(instrument("ALPHA"), instrument("GAMMA")))
    assert second.added == ("GAMMA",)
    assert second.removed == ("BETA",)
    assert second.unchanged == ("ALPHA",)
    assert not beta.in_nifty50


def test_duplicate_import_is_idempotent_and_manual_watchlist_is_unique(session):
    manager = StockUniverseService(session)
    data = master(instrument("ALPHA"))
    manager.import_csv_text("SYMBOL\nALPHA\n", require_fifty=False, master_snapshot=data)
    manager.import_csv_text("SYMBOL\nALPHA\n", require_fifty=False, master_snapshot=data)
    manual = manager.add_manual("ALPHA", master_snapshot=data)
    manual_again = manager.add_manual("alpha", master_snapshot=data)
    assert manual.id == manual_again.id
    assert session.scalar(select(Stock).where(Stock.symbol == "ALPHA")) is not None
    assert len(list(session.scalars(select(WatchlistStocks)))) == 1
    assert manual.in_nifty50  # Manual membership does not erase genuine index membership.


def test_manual_addition_and_enable_disable_require_valid_mapping(session):
    manager = StockUniverseService(session)
    with pytest.raises(ValueError, match="unavailable or ambiguous"):
        manager.add_manual("MISSING", master_snapshot=master())
    stock = manager.add_manual("ALPHA", master_snapshot=master(instrument("ALPHA")))
    manager.set_enabled("ALPHA", False)
    assert not stock.active
    manager.set_enabled("ALPHA", True)
    assert stock.active and len(manager.list_stocks(eligible_only=True)) == 1


def test_ambiguous_master_equity_symbol_is_rejected_as_manual_mapping(session):
    records = master(instrument("ALPHA", "NSE_EQ|ALPHA"), instrument("ALPHA", "NSE_EQ|ALPHA_DUP"))
    with pytest.raises(ValueError, match="unavailable or ambiguous"):
        StockUniverseService(session).add_manual("ALPHA", master_snapshot=records)


class CandleProvider:
    def __init__(self, candles):
        self.candles = candles
        self.calls = []

    def historical_candles(self, instrument_key, unit, interval, to_date, from_date=None):
        self.calls.append((instrument_key, unit, interval, to_date, from_date))
        return MarketDataSnapshot(self.candles, datetime.now(timezone.utc), "mock historical source", "HISTORICAL", False)


def uptrend_candles(count: int = 80):
    import math
    rows = []
    for index in range(count):
        close = 100 + index * 0.12 + math.sin(index * 0.7) * 1.2
        open_ = close - 0.15
        high, low = close + 0.8, close - 0.8
        volume = 200 if index == count - 1 else 100
        timestamp = (datetime(2025, 1, 1, 10, tzinfo=timezone(timedelta(hours=5, minutes=30))) +
                     timedelta(days=index)).isoformat()
        rows.append([timestamp, open_, high, low, close, volume])
    return rows


def test_scanner_fetches_daily_data_calculates_indicators_and_returns_structured_result():
    provider = CandleProvider(uptrend_candles())
    stock = Stock(symbol="ALPHA", name="Alpha Limited", exchange="NSE", instrument_key="NSE_EQ|ALPHA",
                  instrument_available=True, active=True)
    result = StockScanner(provider).scan_stock(stock)
    assert result.status == "Ready"
    assert result.data_complete and result.candle_count == 80
    assert result.close is not None and result.ema20 is not None and result.ema50 is not None
    assert result.rsi14 is not None and result.atr14 is not None and result.volume_ratio is not None
    assert result.data_source == "mock historical source"
    assert provider.calls[0][1:3] == ("days", 1)


def test_scanner_marks_short_history_incomplete_and_unmapped_stock_unavailable():
    provider = CandleProvider(uptrend_candles(20))
    mapped = Stock(symbol="SHORT", name="Short", exchange="NSE", instrument_key="NSE_EQ|SHORT",
                   instrument_available=True, active=True)
    short = StockScanner(provider).scan_stock(mapped)
    assert short.status == "Insufficient data" and not short.data_complete
    missing = Stock(symbol="NONE", name="None", exchange="NSE", instrument_available=False, active=True)
    unavailable = StockScanner(provider).scan_stock(missing)
    assert unavailable.status == "Unavailable instrument"


def test_scanner_filters_direction_volume_rsi_ema_risk_reward_and_sort():
    rows = [
        ScannerResult("AAA", "A", "Ready", "test", None, 80, True, close=10, rsi14=60,
                      volume_ratio=1.8, ema_trend="Bullish", direction="Bullish", strong_volume=True, risk_reward=2.0),
        ScannerResult("BBB", "B", "Ready", "test", None, 80, True, close=20, rsi14=45,
                      volume_ratio=1.0, ema_trend="Bearish", direction="Bearish", strong_volume=False, risk_reward=1.0),
    ]
    selected = filter_scan_results(rows, direction="Bullish", strong_volume_only=True, rsi_min=50,
                                   rsi_max=70, ema_trend="Bullish", minimum_risk_reward=1.5,
                                   sort_by="close", descending=True)
    assert [row.symbol for row in selected] == ["AAA"]
    assert filter_scan_results(rows, direction="Bearish")[0].symbol == "BBB"
