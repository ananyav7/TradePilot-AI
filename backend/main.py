"""FastAPI application entry point."""

from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI

from config.settings import get_settings
from database.init_db import initialize_database
from backend.universe_routes import router as universe_router
from backend.strategy_routes import router as strategy_router
from backend.research_routes import router as research_router
from backend.paper_routes import router as paper_router
from integrations.upstox.routes import router as upstox_auth_router
from backend.scheduler_routes import router as scheduler_router
from scheduler.service import DailyAutomationScheduler
from utils.logging_config import configure_logging

settings = get_settings()
configure_logging(settings.log_level)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Initialize local application resources on startup."""
    initialize_database()
    scheduler = DailyAutomationScheduler()
    scheduler.start()  # Best effort; scheduler failure must not block manual API use.
    logger.info("%s API startup complete", settings.app_name)
    try:
        yield
    finally:
        scheduler.shutdown(wait=True)
    logger.info("%s API shutdown complete", settings.app_name)


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.include_router(upstox_auth_router)
app.include_router(universe_router)
app.include_router(strategy_router)
app.include_router(research_router)
app.include_router(paper_router)
app.include_router(scheduler_router)


@app.get("/health", tags=["system"])
def health_check() -> dict[str, str]:
    """Report process health without making market data claims."""
    return {"status": "ok", "application": settings.app_name}
