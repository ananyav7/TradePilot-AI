"""Durable orchestration for scheduled NSE data, indicators, scans, and signals."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import json
import logging
import os
import socket
from threading import Lock
from typing import Any

import pandas as pd
from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from config.settings import get_settings
from database.connection import SessionLocal, transaction
from database.models import (Indicators, MarketData, NSECalendarYear, Positions,
                             SchedulerLeases, SchedulerRuns, Stock, SystemLogs, TradeSignals)
from integrations.upstox.client import MarketDataClient
from integrations.upstox.instrument_master import InstrumentMasterClient
from integrations.upstox.routes import upstox_market_data, upstox_token_manager
from market_data.universe import StockUniverseService
from notifications.telegram import TelegramNotifier
from portfolio.portfolio_manager import PortfolioManager
from risk.controls import KillSwitch
from risk.engine import RiskEngine, RiskRequest
from scheduler.calendar import INDIA_TZ, NSETradingCalendar, seed_official_2026_calendar
from strategy.scanner import StockScanner, calculate_indicators, candles_to_frame
from strategy.swing_strategy import OpenPositionContext, SwingStrategyEngine
from strategy.technical_analysis import TechnicalAnalysisEngine

logger = logging.getLogger(__name__)
DAILY_TIMES = {
    "connectivity_check": time(8, 30),
    "instrument_master_refresh": time(8, 45),
    "market_data_availability": time(9, 0),
    "fetch_daily_candles": time(15, 45),
    "calculate_indicators": time(16, 0),
    "nifty50_scanner": time(16, 5),
    "generate_validate_signals": time(16, 10),
    "dashboard_update": time(16, 15),
    "daily_summary": time(16, 20),
}
PIPELINE_JOBS = {"fetch_daily_candles", "calculate_indicators", "nifty50_scanner",
                 "generate_validate_signals"}
SCAN_LOCK = Lock()
_active_workflow: "DailyWorkflow | None" = None


def dispatch_job(job_name: str) -> None:
    """Module-level APScheduler target, safe to register in persistent job stores."""
    if _active_workflow is None:
        logger.error("Scheduled operation %s skipped: workflow service is not active", job_name)
        return
    _active_workflow.run(job_name)


class DailyWorkflow:
    """Run one scheduled operation, with durable deduplication and audit logging."""

    def __init__(self, *, session_factory: sessionmaker[Session] = SessionLocal,
                 market_data: MarketDataClient = upstox_market_data,
                 token_manager=upstox_token_manager,
                 instrument_master: InstrumentMasterClient | None = None,
                 notifier_factory=None,
                 clock=None,
                 owner_id: str | None = None) -> None:
        self.session_factory = session_factory
        self.market_data = market_data
        self.token_manager = token_manager
        self.instrument_master = instrument_master or InstrumentMasterClient()
        self.notifier_factory = notifier_factory or (lambda session: TelegramNotifier(session))
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.owner_id = owner_id or f"{socket.gethostname()}|{os.getpid()}|{id(self)}"
        self.settings = get_settings()

    def run(self, job_name: str, *, at: datetime | None = None,
            scheduled_for: datetime | None = None) -> SchedulerRuns | None:
        """Execute a scheduled job at most once for its durable daily/minute key."""
        now = at or self.clock()
        if now.tzinfo is None:
            raise ValueError("Scheduler clock must return a timezone-aware datetime")
        now = now.astimezone(timezone.utc)
        local_now = now.astimezone(INDIA_TZ)
        scheduled_local = (scheduled_for.astimezone(INDIA_TZ) if scheduled_for else local_now)
        market_date = scheduled_local.date()
        scheduled_at = self._scheduled_for(job_name, scheduled_local) if scheduled_for is None else scheduled_for.astimezone(timezone.utc)
        run_key = self._run_key(job_name, scheduled_local)
        with self.session_factory() as session:
            seed_official_2026_calendar(session)
            run_record = self._claim(session, run_key, job_name, market_date, scheduled_at, now)
            if run_record is None:
                logger.info("Scheduler duplicate suppressed: %s (%s)", job_name, run_key)
                return None
            logger.info("Scheduled operation started: %s for %s", job_name, market_date)
            try:
                calendar = NSETradingCalendar(session)
                if job_name == "monitor_positions":
                    if not calendar.is_open_at(local_now):
                        self._finish(session, run_record, "skipped", {"reason": "outside official market session"}, now)
                        return run_record
                else:
                    if session.get(NSECalendarYear, market_date.year) is None:
                        self._finish(session, run_record, "skipped", {"reason": "official NSE calendar year is not verified"}, now)
                        return run_record
                    if not calendar.is_trading_day(market_date):
                        self._finish(session, run_record, "skipped", {"reason": "NSE holiday or non-trading weekend"}, now)
                        return run_record
                    if job_name in DAILY_TIMES and not calendar.is_standard_session_day(market_date):
                        self._finish(session, run_record, "skipped", {"reason": "special session; standard 08:30–16:20 workflow is not applicable"}, now)
                        return run_record
                if job_name in PIPELINE_JOBS:
                    if not SCAN_LOCK.acquire(blocking=False):
                        self._finish(session, run_record, "skipped", {"reason": "another market scan job is still running"}, now)
                        return run_record
                    if not self._acquire_pipeline_lease(session, now):
                        SCAN_LOCK.release()
                        self._finish(session, run_record, "skipped", {"reason": "another process holds the market pipeline lease"}, now)
                        return run_record
                    try:
                        details = self._execute(session, job_name, market_date, now)
                    finally:
                        self._release_pipeline_lease(session, now)
                        SCAN_LOCK.release()
                else:
                    details = self._execute(session, job_name, market_date, now)
                status = "partial" if details.get("failures") else "succeeded"
                self._finish(session, run_record, status, details, now)
                logger.info("Scheduled operation finished: %s status=%s", job_name, status)
                return run_record
            except Exception as exc:
                self._finish(session, run_record, "failed", {"reason": "operation raised an exception"}, now,
                             error_type=type(exc).__name__)
                logger.error("Scheduled operation failed: %s (%s)", job_name, type(exc).__name__)
                return run_record

    def expire_and_monitor(self, now: datetime) -> dict[str, Any]:
        """Public helper for tests and monitoring integrations; quote alerts never place orders."""
        return self._monitor_positions(now.astimezone(timezone.utc).astimezone(INDIA_TZ).date(), now)

    def _execute(self, session: Session, job_name: str, market_date: date,
                 now: datetime) -> dict[str, Any]:
        handlers = {
            "connectivity_check": self._connectivity_check,
            "instrument_master_refresh": self._refresh_instruments,
            "market_data_availability": self._check_market_data,
            "monitor_positions": lambda s, d, n: self._monitor_positions(d, n),
            "fetch_daily_candles": self._fetch_daily_candles,
            "calculate_indicators": self._calculate_indicators,
            "nifty50_scanner": self._run_scanner,
            "generate_validate_signals": self._generate_signals,
            "dashboard_update": self._update_dashboard,
            "daily_summary": self._send_daily_summary,
        }
        handler = handlers.get(job_name)
        if handler is None:
            raise ValueError(f"Unknown scheduled operation: {job_name}")
        return handler(session, market_date, now)

    def _connectivity_check(self, session: Session, _: date, now: datetime) -> dict[str, Any]:
        stocks = self._eligible_stocks(session)
        if not stocks:
            raise RuntimeError("No enabled NIFTY 50 stocks have validated instrument keys")
        token = self.token_manager.get_token()
        quote = self.market_data.full_market_quotes([stocks[0].instrument_key])
        if not quote.data:
            raise RuntimeError("Upstox quote connectivity returned no market data")
        return {"database": "reachable", "upstox": "reachable",
                "quote_count": len(quote.data), "quote_source": quote.source,
                "retrieved_at": quote.retrieved_at.isoformat(), "token_present": bool(token)}

    def _refresh_instruments(self, session: Session, _: date, now: datetime) -> dict[str, Any]:
        result = StockUniverseService(session, instrument_master=self.instrument_master).refresh_official(
            force_master_refresh=False)
        snapshot = self.instrument_master.get_nse_instruments()
        return {"source": result.source, "refreshed_at": result.updated_at.isoformat(),
                "instrument_master_source": snapshot.source, "instrument_cache_hit": snapshot.cache_hit,
                "added": len(result.added), "removed": len(result.removed),
                "unavailable": list(result.unavailable)}

    def _check_market_data(self, session: Session, _: date, now: datetime) -> dict[str, Any]:
        stocks = self._eligible_stocks(session)
        self.token_manager.get_token()
        keys = [stock.instrument_key for stock in stocks]
        if not keys:
            raise RuntimeError("No enabled mapped NIFTY 50 instruments are available")
        received: set[str] = set()
        retrieved: list[str] = []
        for start in range(0, len(keys), 500):
            snapshot = self.market_data.full_market_quotes(keys[start:start + 500])
            received.update(snapshot.data)
            retrieved.append(snapshot.retrieved_at.isoformat())
        missing = [stock.symbol for stock in stocks if stock.instrument_key not in received]
        return {"universe_count": len(keys), "quote_count": len(received),
                "missing_instruments": missing,
                "failures": [{"symbol": symbol, "reason": "quote unavailable"} for symbol in missing],
                "retrieved_at": retrieved, "data_kind": "LIVE_QUOTE"}

    def _monitor_positions(self, market_date: date, now: datetime) -> dict[str, Any]:
        with self.session_factory() as session:
            portfolio = PortfolioManager(session).get_or_create()
            positions = list(session.scalars(select(Positions).where(
                Positions.portfolio_id == portfolio.id, Positions.status == "open")))
            if not positions:
                return {"open_positions": 0, "alerts": 0, "execution": "monitor only"}
            keys = [position.stock.instrument_key for position in positions
                    if position.stock.instrument_available and position.stock.instrument_key]
            if not keys:
                raise RuntimeError("Open paper position has no validated Upstox instrument key")
            self.token_manager.get_token()
            snapshot = self.market_data.full_market_quotes(keys)
            alerts = []
            for position in positions:
                key = position.stock.instrument_key
                row = snapshot.data.get(key) if key else None
                if not row:
                    continue
                last = Decimal(str(row["last_price"]))
                trigger = ("stop_loss_triggered" if position.stop_loss is not None and last <= Decimal(position.stop_loss)
                           else "target_reached" if position.target_price is not None and
                           last >= Decimal(position.target_price) else None)
                if trigger:
                    alert_key = f"position-alert:{position.id}:{trigger}"
                    if self._claim(session, alert_key, trigger, market_date, now, now) is None:
                        continue
                    self.notifier_factory(session).notify(
                        trigger, f"{position.stock.symbol}: paper position price alert; observed quote ₹{last}. "
                        "Monitoring alert only; no order was placed.")
                    alert_run = session.scalar(select(SchedulerRuns).where(SchedulerRuns.run_key == alert_key))
                    if alert_run is not None:
                        self._finish(session, alert_run, "succeeded",
                                     {"symbol": position.stock.symbol, "last_price": str(last),
                                      "source": snapshot.source}, now)
                    alerts.append({"symbol": position.stock.symbol, "event": trigger})
            return {"open_positions": len(positions), "alerts": alerts,
                    "retrieved_at": snapshot.retrieved_at.isoformat(), "data_kind": "LIVE_QUOTE",
                    "execution": "monitor only"}

    def _fetch_daily_candles(self, session: Session, market_date: date, now: datetime) -> dict[str, Any]:
        self._require_previous(session, "market_data_availability", market_date)
        self.token_manager.get_token()
        stocks = self._eligible_stocks(session)
        start_date = market_date - timedelta(days=180)
        saved = 0
        failures: list[dict[str, str]] = []
        for stock in stocks:
            try:
                snapshot = self.market_data.historical_candles(
                    stock.instrument_key, "days", 1, market_date, start_date)
                frame = candles_to_frame(snapshot.data)
                if frame.empty or frame.index[-1].tz_convert(INDIA_TZ).date() != market_date:
                    raise ValueError("latest completed daily candle is missing")
                if len(frame) < 60:
                    raise ValueError(f"insufficient daily history ({len(frame)} candles)")
                rows = []
                for timestamp, candle in frame.iterrows():
                    candle_at = timestamp.to_pydatetime().astimezone(timezone.utc)
                    rows.append({
                        "stock_id": stock.id, "interval": "1d",
                        "market_date": timestamp.tz_convert(INDIA_TZ).date(),
                        "candle_at": candle_at, "open": Decimal(str(candle["open"])),
                        "high": Decimal(str(candle["high"])), "low": Decimal(str(candle["low"])),
                        "close": Decimal(str(candle["close"])), "volume": int(candle["volume"]),
                        "source": "UPSTOX_HIST_V3", "created_at": snapshot.retrieved_at,
                        "updated_at": snapshot.retrieved_at,
                    })
                with transaction(session):
                    session.execute(sqlite_insert(MarketData).values(rows).on_conflict_do_nothing(
                        index_elements=["stock_id", "interval", "candle_at"]))
                saved += 1
            except Exception as exc:
                failures.append({"symbol": stock.symbol, "reason": type(exc).__name__})
        return {"eligible_count": len(stocks), "stocks_with_completed_candles": saved,
                "failures": failures, "market_date": market_date.isoformat(),
                "source": "Upstox Historical Candle Data V3"}

    def _calculate_indicators(self, session: Session, market_date: date, now: datetime) -> dict[str, Any]:
        self._require_previous(session, "fetch_daily_candles", market_date)
        stocks = self._eligible_stocks(session)
        complete = 0
        failures = []
        for stock in stocks:
            rows = self._stored_candles(session, stock.id, market_date)
            if not rows or rows[-1].market_date != market_date:
                failures.append({"symbol": stock.symbol, "reason": "current completed candle missing"})
                continue
            try:
                frame = self._rows_to_frame(rows)
                indicators = calculate_indicators(frame)
                latest = indicators.iloc[-1]
                required = ("ema20", "ema50", "rsi14", "atr14", "volume_sma20",
                            "relative_volume", "daily_change_pct", "support20", "resistance20")
                if any(pd.isna(latest[name]) for name in required):
                    raise ValueError("incomplete indicator values")
                timestamp = indicators.index[-1].to_pydatetime().astimezone(timezone.utc)
                with transaction(session):
                    for name in required:
                        existing = session.scalar(select(Indicators).where(
                            Indicators.stock_id == stock.id, Indicators.interval == "1d",
                            Indicators.candle_at == timestamp, Indicators.name == name))
                        if existing is None:
                            session.add(Indicators(stock_id=stock.id, interval="1d",
                                market_date=market_date, candle_at=timestamp, name=name,
                                value=Decimal(str(latest[name]))))
                complete += 1
            except (ValueError, TypeError, KeyError) as exc:
                failures.append({"symbol": stock.symbol, "reason": type(exc).__name__})
        return {"eligible_count": len(stocks), "stocks_with_indicators": complete,
                "failures": failures, "market_date": market_date.isoformat(),
                "warmup": "at least 60 complete daily candles"}

    def _run_scanner(self, session: Session, market_date: date, now: datetime) -> dict[str, Any]:
        self._require_previous(session, "calculate_indicators", market_date)
        stocks = [stock for stock in self._eligible_stocks(session)
                  if self._has_current_candle(session, stock.id, market_date)]
        provider = _StoredCandleProvider(session)
        results = StockScanner(provider).scan(stocks, to_date=market_date)
        data = [asdict(result) for result in results]
        ready = sum(item.data_complete for item in results)
        failures = [{"symbol": item.symbol, "reason": item.status}
                    for item in results if not item.data_complete]
        return {"eligible_count": len(stocks), "ready_count": ready, "results": data,
                "failures": failures, "market_date": market_date.isoformat()}

    def _generate_signals(self, session: Session, market_date: date, now: datetime) -> dict[str, Any]:
        self._require_previous(session, "nifty50_scanner", market_date)
        stocks = self._eligible_stocks(session)
        engine = SwingStrategyEngine()
        analysis_engine = TechnicalAnalysisEngine()
        portfolio = PortfolioManager(session).get_or_create()
        positions = list(session.scalars(select(Positions).where(
            Positions.portfolio_id == portfolio.id, Positions.status == "open")))
        position_by_stock = {position.stock_id: position for position in positions}
        generated, qualified, failures = [], [], []
        notifier = self.notifier_factory(session)
        risk = RiskEngine(kill_switch=KillSwitch())
        snapshot = PortfolioManager(session).snapshot()
        for stock in stocks:
            rows = self._stored_candles(session, stock.id, market_date)
            if not rows or rows[-1].market_date != market_date:
                continue
            try:
                frame = self._rows_to_frame(rows)
                analysis = analysis_engine.analyze(frame.set_index("timestamp"),
                    end_of_day=True, completed_through=market_date, source="UPSTOX_HIST_V3")
                open_position = position_by_stock.get(stock.id)
                context = None
                if open_position is not None:
                    context = OpenPositionContext(float(open_position.average_entry_price),
                        float(open_position.stop_loss) if open_position.stop_loss else None,
                        float(open_position.target_price) if open_position.target_price else None)
                signal = engine.generate_and_store(session, stock, analysis, position=context)
                stored = session.scalar(select(TradeSignals).where(TradeSignals.signal_key == signal.signal_id))
                generated.append({"signal_key": signal.signal_id, "symbol": stock.symbol,
                                  "signal_type": signal.signal_type})
                if signal.signal_type != "BUY" or stored is None:
                    continue
                candle_time = analysis.latest.timestamp.to_pydatetime().astimezone(timezone.utc)
                assessment = risk.assess(RiskRequest(
                    entry_price=analysis.latest.close, stop_loss=signal.stop_loss,
                    target_price=signal.target, available_capital=get_settings().risk_starting_capital,
                    available_cash=snapshot.available_cash, current_exposure=snapshot.invested_capital,
                    open_positions=snapshot.open_positions, daily_pnl=PortfolioManager(session)._realized_balance(portfolio),
                    market_data_at=candle_time, as_of=now))
                stored.status = "risk_qualified" if assessment.accepted else "risk_rejected"
                session.flush()
                if assessment.accepted:
                    qualified.append({"symbol": stock.symbol, "signal_key": signal.signal_id,
                                      "quantity": assessment.quantity,
                                      "risk_amount": str(assessment.risk_amount),
                                      "estimated_charges": str(assessment.round_trip_transaction_cost)})
                    notifier.notify("new_opportunity",
                        f"{stock.symbol}: NIFTY 50 strategy and risk checks passed. "
                        f"Suggested paper size {assessment.quantity}; open Trade Approval for fresh quote review. "
                        "This is not an approval or order.")
            except Exception as exc:
                failures.append({"symbol": stock.symbol, "reason": type(exc).__name__})
        return {"generated_count": len(generated), "signals": generated,
                "risk_qualified_count": len(qualified), "qualified": qualified,
                "failures": failures, "market_date": market_date.isoformat()}

    def _update_dashboard(self, session: Session, market_date: date, now: datetime) -> dict[str, Any]:
        runs = list(session.scalars(select(SchedulerRuns).where(
            SchedulerRuns.market_date == market_date, SchedulerRuns.job_name != "dashboard_update")
            .order_by(SchedulerRuns.started_at)))
        signals = list(session.scalars(select(TradeSignals).where(TradeSignals.signal_date == market_date)))
        return {"latest_operation_count": len(runs), "signal_count": len(signals),
                "risk_qualified_count": sum(item.status == "risk_qualified" for item in signals),
                "market_date": market_date.isoformat(), "dashboard_reads": "SQLite scheduler and signal ledgers"}

    def _send_daily_summary(self, session: Session, market_date: date, now: datetime) -> dict[str, Any]:
        runs = list(session.scalars(select(SchedulerRuns).where(
            SchedulerRuns.market_date == market_date, SchedulerRuns.job_name != "daily_summary")
            .order_by(SchedulerRuns.started_at)))
        signal_count = session.query(TradeSignals).filter(TradeSignals.signal_date == market_date).count()
        qualified = session.query(TradeSignals).filter(
            TradeSignals.signal_date == market_date, TradeSignals.status == "risk_qualified").count()
        failed = [run.job_name for run in runs if run.status in {"failed", "partial"}]
        message = (f"TradePilot daily report · {market_date.isoformat()} IST. "
                   f"Scheduled operations {len(runs)}; data/signals requiring review {len(failed)}; "
                   f"signals {signal_count}; risk-qualified {qualified}. "
                   f"Operation issues: {', '.join(failed) if failed else 'none'}. "
                   "Paper/research only; no live orders.")
        notice = self.notifier_factory(session).daily_report(message)
        return {"signal_count": signal_count, "risk_qualified_count": qualified,
                "telegram_status": notice.status, "operation_issues": failed}

    def _eligible_stocks(self, session: Session) -> list[Stock]:
        return list(session.scalars(select(Stock).where(
            Stock.exchange == "NSE", Stock.in_nifty50.is_(True), Stock.active.is_(True),
            Stock.instrument_available.is_(True), Stock.instrument_key.is_not(None))
            .order_by(Stock.symbol)))

    def _stored_candles(self, session: Session, stock_id: int, through: date) -> list[MarketData]:
        return list(session.scalars(select(MarketData).where(
            MarketData.stock_id == stock_id, MarketData.interval == "1d",
            MarketData.market_date <= through).order_by(MarketData.candle_at).limit(300)))

    @staticmethod
    def _rows_to_frame(rows: list[MarketData]) -> pd.DataFrame:
        return pd.DataFrame([{"timestamp": row.candle_at, "open": float(row.open),
                              "high": float(row.high), "low": float(row.low),
                              "close": float(row.close), "volume": row.volume}
                             for row in rows]).set_index("timestamp")

    def _has_current_candle(self, session: Session, stock_id: int, market_date: date) -> bool:
        latest = session.scalar(select(MarketData.market_date).where(
            MarketData.stock_id == stock_id, MarketData.interval == "1d")
            .order_by(MarketData.market_date.desc()).limit(1))
        return latest == market_date

    def _require_previous(self, session: Session, job_name: str, market_date: date) -> SchedulerRuns:
        run = session.scalar(select(SchedulerRuns).where(
            SchedulerRuns.job_name == job_name, SchedulerRuns.market_date == market_date,
            SchedulerRuns.status.in_(("succeeded", "partial")))
            .order_by(SchedulerRuns.started_at.desc()).limit(1))
        if run is None:
            raise RuntimeError(f"Required prior operation {job_name} did not complete successfully")
        return run

    def _scheduled_for(self, job_name: str, local_now: datetime) -> datetime:
        scheduled_time = DAILY_TIMES.get(job_name)
        if scheduled_time is None:
            rounded = local_now.replace(second=0, microsecond=0)
            return rounded.astimezone(timezone.utc)
        return datetime.combine(local_now.date(), scheduled_time, INDIA_TZ).astimezone(timezone.utc)

    def _run_key(self, job_name: str, local_now: datetime) -> str:
        if job_name == "monitor_positions":
            return f"{job_name}:{local_now.strftime('%Y-%m-%dT%H:%M')}"
        return f"{job_name}:{local_now.date().isoformat()}"

    def _claim(self, session: Session, run_key: str, job_name: str, market_date: date,
               scheduled_for: datetime, now: datetime) -> SchedulerRuns | None:
        try:
            with transaction(session):
                record = session.scalar(select(SchedulerRuns).where(SchedulerRuns.run_key == run_key))
                if record is None:
                    record = SchedulerRuns(run_key=run_key, job_name=job_name, market_date=market_date,
                        scheduled_for=scheduled_for, status="running", attempts=1, owner_id=self.owner_id,
                        started_at=now)
                    session.add(record)
                    session.flush()
                    return record
                if record.status in {"succeeded", "partial", "skipped"}:
                    return None
                if record.status == "running" and not self._owner_is_stale(record.owner_id, record.started_at, now):
                    return None
                record.status = "running"
                record.attempts += 1
                record.owner_id = self.owner_id
                record.started_at = now
                record.finished_at = None
                record.error_type = None
                return record
        except IntegrityError:
            session.rollback()
            return None

    @staticmethod
    def _owner_is_stale(owner: str | None, started_at: datetime, now: datetime) -> bool:
        if owner and owner.split("|", 1)[0] == socket.gethostname():
            try:
                os.kill(int(owner.split("|")[1]), 0)
                return False
            except (ProcessLookupError, ValueError):
                return True
            except PermissionError:
                return False
        return now - started_at.astimezone(timezone.utc) > timedelta(hours=4)

    def _acquire_pipeline_lease(self, session: Session, now: datetime) -> bool:
        expires = now + timedelta(hours=4)
        with transaction(session):
            session.execute(sqlite_insert(SchedulerLeases).values(
                resource="market_pipeline", owner_id=self.owner_id,
                acquired_at=now, lease_until=expires).on_conflict_do_nothing(
                    index_elements=["resource"]))
            lease = session.get(SchedulerLeases, "market_pipeline")
            if lease is None:
                return False
            if lease.owner_id == self.owner_id:
                return True
            if lease.lease_until > now and not self._owner_is_stale(lease.owner_id, lease.acquired_at, now):
                return False
            old_owner, old_until = lease.owner_id, lease.lease_until
            claimed = session.execute(update(SchedulerLeases).where(
                SchedulerLeases.resource == "market_pipeline",
                SchedulerLeases.owner_id == old_owner,
                SchedulerLeases.lease_until == old_until).values(
                    owner_id=self.owner_id, acquired_at=now, lease_until=expires))
            return claimed.rowcount == 1

    def _release_pipeline_lease(self, session: Session, now: datetime) -> None:
        with transaction(session):
            session.execute(update(SchedulerLeases).where(
                SchedulerLeases.resource == "market_pipeline",
                SchedulerLeases.owner_id == self.owner_id).values(lease_until=now))

    @staticmethod
    def _finish(session: Session, record: SchedulerRuns, status: str, details: dict[str, Any],
                now: datetime, *, error_type: str | None = None) -> None:
        record.status = status
        record.finished_at = now
        record.details_json = json.dumps(details, default=str, separators=(",", ":"))
        record.error_type = error_type
        with transaction(session):
            session.flush()
            session.add(SystemLogs(level="ERROR" if status == "failed" else "INFO",
                event=f"scheduler:{record.job_name}",
                message=f"Scheduled operation {record.job_name} finished with status {status}",
                context_json=json.dumps({"run_key": record.run_key, "status": status,
                                         "error_type": error_type, "details": details},
                                        default=str, separators=(",", ":"))))


class _StoredCandleProvider:
    """Daily candle provider backed only by validated, persisted scheduler downloads."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def historical_candles(self, instrument_key: str, unit: str, interval: int,
                           to_date: Any, from_date: Any = None) -> Any:
        from integrations.upstox.common import MarketDataSnapshot
        stock = self.session.scalar(select(Stock).where(Stock.instrument_key == instrument_key))
        if stock is None:
            raise ValueError("Stored candle mapping does not exist")
        rows = self.session.scalars(select(MarketData).where(
            MarketData.stock_id == stock.id, MarketData.interval == "1d",
            MarketData.market_date <= to_date).order_by(MarketData.candle_at)).all()
        candles = [[row.candle_at.isoformat(), float(row.open), float(row.high), float(row.low),
                    float(row.close), row.volume] for row in rows]
        retrieved = max((row.created_at for row in rows), default=datetime.now(timezone.utc))
        return MarketDataSnapshot(candles, retrieved, "Persisted Upstox Historical Candle Data V3",
                                  "HISTORICAL", True)
