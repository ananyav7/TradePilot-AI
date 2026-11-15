"""FastAPI application entry point."""

from contextlib import asynccontextmanager
import hmac
import logging

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from fastapi import FastAPI

from config.settings import get_settings
from database.connection import database_health
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
if settings.app_env.lower() in {"production", "prod"} and settings.app_role != "api":
    raise RuntimeError("Set APP_ROLE=api for the FastAPI service")
configure_logging(settings.log_level)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Initialize local application resources on startup."""
    if settings.app_env.lower() not in {"production", "prod"}:
        initialize_database()
    if not database_health():
        raise RuntimeError("Configured database is unavailable; refusing to start without shared storage")
    scheduler = DailyAutomationScheduler(enabled=settings.scheduler_mode == "embedded")
    if settings.scheduler_mode == "embedded":
        scheduler.start()
    logger.info("%s API startup complete", settings.app_name)
    try:
        yield
    finally:
        scheduler.shutdown(wait=True)
    logger.info("%s API shutdown complete", settings.app_name)


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def require_service_token(request: Request, call_next):
    """Require the private Streamlit-to-API token outside local development."""
    public_paths = {"/health", "/auth/upstox/login", "/auth/callback"}
    if settings.app_env.lower() in {"production", "prod"} and request.url.path not in public_paths:
        expected = settings.api_auth_token.get_secret_value()
        supplied = request.headers.get("authorization", "")
        supplied = supplied[7:] if supplied.lower().startswith("bearer ") else ""
        if not expected or not hmac.compare_digest(supplied, expected):
            return JSONResponse(status_code=401, content={"detail": "Authentication required"})
    return await call_next(request)


@app.exception_handler(SQLAlchemyError)
async def database_exception_handler(request: Request, exc: SQLAlchemyError):
    logger.exception("Database operation failed for %s", request.url.path)
    return JSONResponse(status_code=503, content={"detail": "Database temporarily unavailable"})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled API error for %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})
app.include_router(upstox_auth_router)
app.include_router(universe_router)
app.include_router(strategy_router)
app.include_router(research_router)
app.include_router(paper_router)
app.include_router(scheduler_router)


@app.get("/health", tags=["system"])
def health_check() -> dict[str, str]:
    """Report process health without making market data claims."""
    if not database_health():
        return JSONResponse(status_code=503, content={"status": "unavailable", "database": "down"})
    return {"status": "ok", "database": "ok", "application": settings.app_name}
