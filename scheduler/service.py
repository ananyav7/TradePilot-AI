"""APScheduler 3.x lifecycle, timezone-aware jobs, and restart catch-up."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from threading import RLock
from typing import Any
from sqlalchemy import select

from config.settings import get_settings
from database.connection import SessionLocal
from database.models import NSECalendarException, SchedulerRuns, SchedulerWorkerHeartbeat
from scheduler.calendar import INDIA_TZ, NSETradingCalendar, seed_official_2026_calendar
from scheduler.workflow import DAILY_TIMES, DailyWorkflow, dispatch_job

logger = logging.getLogger(__name__)
_active_service: "DailyAutomationScheduler | None" = None
_lifecycle_lock = RLock()


class DailyAutomationScheduler:
    """Background market scheduler; startup failure does not stop FastAPI."""

    def __init__(self, workflow: DailyWorkflow | None = None, *, enabled: bool | None = None,
                 clock=None) -> None:
        self.workflow = workflow or DailyWorkflow()
        self.enabled = get_settings().daily_scheduler_enabled if enabled is None else enabled
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.scheduler: Any | None = None
        self.start_error: str | None = None

    @property
    def running(self) -> bool:
        return bool(self.scheduler and self.scheduler.running)

    def start(self) -> bool:
        global _active_service
        if not self.enabled:
            _active_service = self
            return False
        with _lifecycle_lock:
            if self.running:
                return True
            try:
                from apscheduler.schedulers.background import BackgroundScheduler
                from apscheduler.triggers.cron import CronTrigger
                from apscheduler.triggers.date import DateTrigger
                _active_service = self
                from scheduler import workflow as workflow_module
                workflow_module._active_workflow = self.workflow
                scheduler = BackgroundScheduler(
                    timezone=INDIA_TZ,
                    job_defaults={"coalesce": True, "max_instances": 1,
                                  "misfire_grace_time": 7200},
                )
                for name, clock_time in DAILY_TIMES.items():
                    scheduler.add_job(
                        dispatch_job,
                        CronTrigger(day_of_week="mon-fri", hour=clock_time.hour,
                                    minute=clock_time.minute, timezone=INDIA_TZ),
                        id=name, args=[name], replace_existing=True, max_instances=1,
                        coalesce=True, misfire_grace_time=7200,
                    )
                scheduler.add_job(
                    dispatch_job,
                    CronTrigger(day_of_week="mon-fri", hour="9-15",
                                minute=f"*/{get_settings().scheduler_monitor_interval_minutes}",
                                timezone=INDIA_TZ),
                    id="monitor_positions", args=["monitor_positions"],
                    replace_existing=True, max_instances=1, coalesce=True,
                    misfire_grace_time=120,
                )
                self._add_special_session_monitor_jobs(scheduler)
                scheduler.start()
                self.scheduler = scheduler
                self.start_error = None
                catchup_at = self.clock().astimezone(timezone.utc) + timedelta(seconds=1)
                scheduler.add_job(self._catch_up_today, DateTrigger(run_date=catchup_at),
                                  id=f"daily-catch-up-{catchup_at.strftime('%Y%m%d%H%M%S')}",
                                  replace_existing=False, max_instances=1)
                logger.info("Daily automation scheduler started in %s", INDIA_TZ.key)
                return True
            except Exception as exc:
                self.start_error = type(exc).__name__
                logger.error("Daily automation scheduler failed to start (%s); manual app remains available",
                             self.start_error)
                if self.scheduler and self.scheduler.running:
                    self.scheduler.shutdown(wait=False)
                self.scheduler = None
                return False

    def shutdown(self, *, wait: bool = True) -> None:
        global _active_service
        with _lifecycle_lock:
            if self.scheduler and self.scheduler.running:
                self.scheduler.shutdown(wait=wait)
            self.scheduler = None
            if _active_service is self:
                _active_service = None
                from scheduler import workflow as workflow_module
                workflow_module._active_workflow = None
            logger.info("Daily automation scheduler stopped")

    def status(self) -> dict:
        with SessionLocal() as session:
            now = self.clock().astimezone(INDIA_TZ)
            seed_official_2026_calendar(session)
            calendar = NSETradingCalendar(session)
            calendar_status = calendar.status(now.year)
            jobs = []
            if self.scheduler:
                for job in self.scheduler.get_jobs():
                    next_run = job.next_run_time
                    jobs.append({"id": job.id,
                                 "next_run_at": next_run.astimezone(timezone.utc).isoformat()
                                 if next_run else None})
            recent = list(session.scalars(select(SchedulerRuns).order_by(
                SchedulerRuns.started_at.desc()).limit(20)))
            return {
                "enabled": self.enabled, "running": self.running,
                "state": "running" if self.running else "disabled" if not self.enabled else
                         "failed" if self.start_error else "stopped",
                "start_error": self.start_error, "timezone": INDIA_TZ.key,
                "local_time": now.isoformat(), "calendar": calendar_status, "jobs": jobs,
                "recent_runs": [{"job_name": row.job_name, "market_date": row.market_date.isoformat(),
                                 "scheduled_for": row.scheduled_for.isoformat(), "status": row.status,
                                 "attempts": row.attempts, "started_at": row.started_at.isoformat(),
                                 "finished_at": row.finished_at.isoformat() if row.finished_at else None,
                                 "details": row.details_json, "error_type": row.error_type}
                                for row in recent],
            }

    def _add_special_session_monitor_jobs(self, scheduler: Any) -> None:
        """Add polling only for explicitly timed special sessions in reviewed calendars."""
        from apscheduler.triggers.cron import CronTrigger

        with SessionLocal() as session:
            seed_official_2026_calendar(session)
            rows = list(session.query(NSECalendarException).filter(
                NSECalendarException.kind == "SPECIAL_SESSION",
                NSECalendarException.opens_at.is_not(None),
                NSECalendarException.closes_at.is_not(None)))
        for row in rows:
            hours = f"{row.opens_at.hour}-{row.closes_at.hour}"
            scheduler.add_job(
                dispatch_job,
                CronTrigger(year=row.market_date.year, month=row.market_date.month,
                            day=row.market_date.day, hour=hours,
                            minute=f"*/{get_settings().scheduler_monitor_interval_minutes}", timezone=INDIA_TZ),
                id=f"monitor_special_{row.market_date.isoformat()}",
                args=["monitor_positions"], replace_existing=True, max_instances=1,
                coalesce=True, misfire_grace_time=120,
            )

    def _catch_up_today(self) -> None:
        """Replay today's missed stages in their documented order after an app restart."""
        now = self.clock().astimezone(timezone.utc)
        local_now = now.astimezone(INDIA_TZ)
        with SessionLocal() as session:
            calendar = NSETradingCalendar(session)
            if not calendar.is_standard_session_day(local_now.date()):
                return
        for job_name, clock_time in DAILY_TIMES.items():
            scheduled_local = datetime.combine(local_now.date(), clock_time, INDIA_TZ)
            if scheduled_local <= local_now:
                try:
                    self.workflow.run(job_name, at=now,
                                      scheduled_for=scheduled_local.astimezone(timezone.utc))
                except Exception as exc:
                    logger.error("Scheduler catch-up failed for %s (%s)", job_name, type(exc).__name__)


def scheduler_status() -> dict:
    """Return current backend scheduler process state, if one was started."""
    if _active_service is None:
        mode = get_settings().scheduler_mode
        response = {"enabled": mode != "disabled", "running": None,
                    "state": "external_worker" if mode == "external" else "not_started",
                    "worker_mode": mode, "timezone": INDIA_TZ.key,
                    "jobs": [], "recent_runs": [], "calendar": {}}
        if mode == "external":
            # Durable history is useful across the separate API/worker processes.
            try:
                with SessionLocal() as session:
                    now = datetime.now(timezone.utc)
                    recent = list(session.scalars(select(SchedulerRuns).order_by(
                        SchedulerRuns.started_at.desc()).limit(20)))
                    response["recent_runs"] = [{"job_name": row.job_name,
                        "market_date": row.market_date.isoformat(), "scheduled_for": row.scheduled_for.isoformat(),
                        "status": row.status, "attempts": row.attempts,
                        "started_at": row.started_at.isoformat(),
                        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
                        "details": row.details_json, "error_type": row.error_type} for row in recent]
                    heartbeat = session.get(SchedulerWorkerHeartbeat, "render-market-worker")
                    last_seen = heartbeat.last_seen if heartbeat else None
                    if last_seen and last_seen.tzinfo is None:
                        last_seen = last_seen.replace(tzinfo=timezone.utc)
                    worker_alive = bool(heartbeat and heartbeat.status == "running" and last_seen and
                                        (now - last_seen.astimezone(timezone.utc)).total_seconds() <= 75)
                    response["running"] = worker_alive
                    response["state"] = "running" if worker_alive else "stopped"
                    response["worker_last_seen"] = last_seen.isoformat() if last_seen else None
            except Exception as exc:
                logger.warning("Unable to read external worker history (%s)", type(exc).__name__)
        return response
    return _active_service.status()
