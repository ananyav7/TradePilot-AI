"""Standalone Render worker entry point for scheduled market jobs."""
from __future__ import annotations

import logging
import signal
from datetime import datetime, timezone
from threading import Event

from config.settings import get_settings
from database.connection import SessionLocal, database_health
from database.models import SchedulerWorkerHeartbeat
from scheduler.service import DailyAutomationScheduler
from utils.logging_config import configure_logging


def _record_heartbeat(status: str) -> None:
    with SessionLocal() as session:
        row = session.get(SchedulerWorkerHeartbeat, "render-market-worker")
        if row is None:
            session.add(SchedulerWorkerHeartbeat(worker_id="render-market-worker", status=status,
                                                last_seen=datetime.now(timezone.utc)))
        else:
            row.status = status
            row.last_seen = datetime.now(timezone.utc)
        session.commit()


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    logger = logging.getLogger(__name__)
    if settings.app_env.lower() in {"production", "prod"} and settings.app_role != "worker":
        raise RuntimeError("Set APP_ROLE=worker for the dedicated scheduler service")
    if settings.scheduler_mode != "external":
        raise RuntimeError("Set SCHEDULER_MODE=external for the dedicated worker")
    if not database_health():
        raise RuntimeError("Configured shared database is unavailable; scheduler worker will not start")
    stopped = Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    scheduler = DailyAutomationScheduler(enabled=True)
    if not scheduler.start():
        raise RuntimeError(f"Scheduler failed to start: {scheduler.start_error or 'unknown error'}")
    _record_heartbeat("running")
    logger.info("External market worker started; timezone=%s", "Asia/Kolkata")
    try:
        while not stopped.wait(20):
            _record_heartbeat("running")
    finally:
        scheduler.shutdown(wait=True)
        _record_heartbeat("stopped")
        logger.info("External market worker stopped")


if __name__ == "__main__":
    main()
