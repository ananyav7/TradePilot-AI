"""Reporting tests for provenance, period metrics, and export metadata."""

from datetime import datetime, timezone
from decimal import Decimal
import io
import zipfile
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from database.connection import Base
from database.models import PaperOrders, Portfolio, Positions, Stock, TradeHistory
from reports.service import BACKTEST_RESULT, PAPER_RESULT, ReportingService


def make_session() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    session._test_engine = engine
    portfolio = Portfolio(name="Paper Portfolio", starting_capital=Decimal("3000.00"))
    stock = Stock(symbol="TST", name="Test Ltd")
    session.add_all([portfolio, stock])
    session.flush()
    position = Positions(portfolio=portfolio, stock=stock, quantity=0, average_entry_price=Decimal("100"),
                         stop_loss=Decimal("90"), status="closed",
                         opened_at=datetime(2026, 11, 1, 4, tzinfo=timezone.utc),
                         closed_at=datetime(2026, 11, 2, 10, tzinfo=timezone.utc))
    session.add(position)
    session.flush()
    order = PaperOrders(position=position, stock=stock, side="SELL", quantity=2, status="FILLED")
    session.add(order)
    session.flush()
    session.add_all([
        TradeHistory(position=position, paper_order=order, event_type="BUY_FILL", quantity=2,
            price=Decimal("100"), created_at=datetime(2026, 11, 1, 5, tzinfo=timezone.utc),
            details='{"cash_delta":"-201","buy_fee":"1","source":"simulated_quote"}'),
        TradeHistory(position=position, paper_order=order, event_type="SELL_FILL", quantity=2,
            price=Decimal("110"), created_at=datetime(2026, 11, 2, 10, tzinfo=timezone.utc),
            details='{"cash_delta":"218","sell_fee":"2","allocated_buy_fee":"1","gross_pnl":"20","realized_net_pnl":"17","slippage_cost":"1","source":"simulated_quote","reason":"TARGET"}'),
    ])
    session.commit()
    return session


def test_paper_analytics_and_period_reports_use_only_paper_fill_ledger():
    session = make_session()
    try:
        service = ReportingService(session)
        metrics = service.paper_analytics(today=datetime(2026, 11, 2).date())
        assert metrics["daily_pnl_inr"] == 17
        assert metrics["weekly_pnl_inr"] == 17
        assert metrics["monthly_pnl_inr"] == 17
        assert metrics["total_return_pct"] == 17 / 3000 * 100
        assert metrics["available_capital_inr"] == 3017
        assert metrics["invested_capital_inr"] == 0
        assert metrics["win_rate_pct"] == 100
        assert metrics["average_reward_risk"] == 0.85
        assert metrics["trading_costs_inr"] == 4
        assert metrics["paper_market_data_classes"] == ["SIMULATED_MARKET_DATA"]
        assert metrics["unrealized_pnl_inr"] is None
        daily = service.daily_report(datetime(2026, 11, 2).date())
        assert daily.result_type == PAPER_RESULT
        assert len(daily.data) == 1
        assert service.weekly_report(datetime(2026, 11, 2).date()).data.iloc[0]["trade_id"].startswith("PAPER-")
        assert service.monthly_report(datetime(2026, 11, 2).date()).data.iloc[0]["net_pnl_inr"] == 17
        assert service.strategy_report().result_type == PAPER_RESULT
        assert service.risk_exposure_report().data.empty
    finally:
        session.close()
        session._test_engine.dispose()


def test_trade_detail_and_missing_id_handling():
    session = make_session()
    try:
        service = ReportingService(session)
        trade_id = service.paper_trades().iloc[0]["trade_id"]
        artifact = service.individual_trade_report(trade_id)
        assert artifact.data.iloc[0]["exit_reason"] == "TARGET"
        try:
            service.individual_trade_report("PAPER-404")
        except LookupError:
            pass
        else:
            raise AssertionError("unknown paper fill should fail clearly")
    finally:
        session.close()
        session._test_engine.dispose()


def test_exports_embed_source_result_type_and_utc_generation_time():
    session = make_session()
    try:
        service = ReportingService(session)
        artifact = service.daily_report(datetime(2026, 11, 2).date())
        csv_data = service.export_csv(artifact).decode("utf-8-sig")
        assert "PAPER_TRADING_RESULT" in csv_data
        assert "SQLite paper fill ledger" in csv_data
        assert artifact.generated_at.tzinfo == timezone.utc
        workbook = service.export_excel(artifact)
        assert zipfile.is_zipfile(io.BytesIO(workbook))
    finally:
        session.close()
        session._test_engine.dispose()


def test_backtest_report_stays_separate_from_paper_results():
    session = make_session()
    try:
        service = ReportingService(session)
        class Result:
            symbol = "TST"
            source = "user_supplied_historical_data"
            trades = __import__("pandas").DataFrame([{"net_pnl": 100}])
            metrics = SimpleNamespace(net_profit_loss=100.0, number_of_trades=1)
            strategy_version = "swing-v1"
            completed_through = datetime(2026, 11, 2).date()
            slippage_bps = 10.0
        artifact = service.backtest_report(Result())
        assert artifact.result_type == BACKTEST_RESULT
        assert artifact.data.iloc[0]["result_type"] == BACKTEST_RESULT
        assert artifact.data.iloc[0]["market_data_classification"] == "HISTORICAL_MARKET_DATA"
        assert service.paper_analytics(today=datetime(2026, 11, 2).date())["total_realized_pnl_inr"] == 17
    finally:
        session.close()
        session._test_engine.dispose()
