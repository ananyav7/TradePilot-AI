from datetime import date, datetime, timezone
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.connection import Base
from scheduler.calendar import (INDIA_TZ, NSE_HOLIDAY_PAGE, NSETradingCalendar,
                                import_official_calendar_csv, seed_official_2026_calendar)
from scheduler.service import DailyAutomationScheduler
from scheduler.workflow import DailyWorkflow


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


class CountingWorkflow(DailyWorkflow):
    def __init__(self, session_factory, *, fail_once=False, owner_id="test-host|987654321|test"):
        super().__init__(session_factory=session_factory, market_data=object(),
                         token_manager=object(), owner_id=owner_id)
        self.count = 0
        self.fail_once = fail_once

    def _execute(self, session, job_name, market_date, now):
        self.count += 1
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("synthetic test failure")
        return {"completed": True}


def test_official_calendar_handles_weekends_holidays_and_special_sessions(session_factory):
    with session_factory() as session:
        seed_official_2026_calendar(session, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
        calendar = NSETradingCalendar(session)
        assert calendar.is_trading_day(date(2026, 11, 9)) is True
        assert calendar.is_trading_day(date(2026, 11, 10)) is False
        assert calendar.is_trading_day(date(2026, 11, 14)) is False
        assert calendar.is_trading_day(date(2026, 11, 8)) is True
        assert calendar.is_standard_session_day(date(2026, 11, 8)) is False
        assert calendar.session_bounds(date(2026, 11, 8)) is None  # timing not yet specified in source
        assert calendar.is_trading_day(date(2027, 1, 4)) is False  # unknown years fail closed
        metadata = calendar.status(2026)
        assert metadata["verified"] is True
        assert metadata["source_url"] == NSE_HOLIDAY_PAGE
        assert metadata["special_sessions"] == 1


def test_official_calendar_import_validates_source_and_special_session_times(session_factory):
    csv_text = "date,kind,description,opens_at,closes_at\n2027-01-26,HOLIDAY,Republic Day,,\n2027-02-20,SPECIAL_SESSION,Special session,18:00,20:00\n"
    with session_factory() as session:
        year = import_official_calendar_csv(session, csv_text,
            source_url="https://www.nseindia.com/resources/exchange-communication-holidays",
            source_name="NSE 2027 equity calendar",
            refreshed_at=datetime(2026, 12, 1, tzinfo=timezone.utc))
        assert year == 2027
        calendar = NSETradingCalendar(session)
        assert not calendar.is_trading_day(date(2027, 1, 26))
        assert calendar.is_trading_day(date(2027, 2, 20))
        assert not calendar.is_standard_session_day(date(2027, 2, 20))
        opening, closing = calendar.session_bounds(date(2027, 2, 20))
        assert opening == datetime(2027, 2, 20, 18, 0, tzinfo=INDIA_TZ)
        assert closing == datetime(2027, 2, 20, 20, 0, tzinfo=INDIA_TZ)
        assert calendar.is_open_at(datetime(2027, 2, 20, 12, 30, tzinfo=timezone.utc))
        with pytest.raises(ValueError, match="NSE India URL"):
            import_official_calendar_csv(session, csv_text, source_url="https://example.com/calendar.csv",
                                        source_name="unverified")


def test_daily_job_is_deduplicated_and_holiday_is_skipped(session_factory):
    workflow = CountingWorkflow(session_factory)
    trading_time = datetime(2026, 11, 11, 3, 30, tzinfo=timezone.utc)  # 09:00 IST
    first = workflow.run("market_data_availability", at=trading_time)
    duplicate = workflow.run("market_data_availability", at=datetime(2026, 11, 11, 4, 0, tzinfo=timezone.utc))
    assert first.status == "succeeded"
    assert duplicate is None
    assert workflow.count == 1

    holiday = workflow.run("market_data_availability", at=datetime(2026, 11, 10, 3, 30, tzinfo=timezone.utc))
    assert holiday.status == "skipped"
    assert "holiday" in json.loads(holiday.details_json)["reason"]
    assert workflow.count == 1


def test_failed_job_can_retry_after_restart_without_duplicate_success(session_factory):
    workflow = CountingWorkflow(session_factory, fail_once=True)
    at = datetime(2026, 11, 11, 3, 30, tzinfo=timezone.utc)
    failed = workflow.run("market_data_availability", at=at)
    assert failed.status == "failed"
    retry = workflow.run("market_data_availability", at=at + __import__("datetime").timedelta(minutes=1))
    assert retry.status == "succeeded"
    assert retry.attempts == 2
    assert workflow.run("market_data_availability", at=at + __import__("datetime").timedelta(minutes=2)) is None
    assert workflow.count == 2


def test_sqlite_lease_blocks_another_process_pipeline_until_released(session_factory):
    first = CountingWorkflow(session_factory)
    second = CountingWorkflow(session_factory, owner_id="test-host|987654322|test")
    now = datetime(2026, 11, 11, 3, 30, tzinfo=timezone.utc)
    with session_factory() as session:
        assert first._acquire_pipeline_lease(session, now)
    with session_factory() as session:
        assert not second._acquire_pipeline_lease(session, now)
    with session_factory() as session:
        first._release_pipeline_lease(session, now)
    with session_factory() as session:
        assert second._acquire_pipeline_lease(session, now)
        second._release_pipeline_lease(session, now)


def test_apscheduler_registers_india_timezone_jobs_and_shuts_down():
    fixed = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)  # Sunday: catch-up is a no-op.
    workflow = CountingWorkflow(sessionmaker(), owner_id="test-host|987654321|service")
    service = DailyAutomationScheduler(workflow, enabled=True, clock=lambda: fixed)
    assert service.start() is True
    try:
        jobs = {job.id: job for job in service.scheduler.get_jobs()}
        assert {"connectivity_check", "instrument_master_refresh", "market_data_availability",
                "fetch_daily_candles", "calculate_indicators", "nifty50_scanner",
                "generate_validate_signals", "dashboard_update", "daily_summary",
                "monitor_positions"} <= jobs.keys()
        assert jobs["daily_summary"].trigger.timezone.key == "Asia/Kolkata"
    finally:
        service.shutdown(wait=True)
    assert not service.running
