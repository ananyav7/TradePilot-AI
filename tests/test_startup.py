"""Basic application foundation startup checks."""

from backend.main import app, health_check
from pydantic import ValidationError
import pytest
from config.settings import Settings
from database.init_db import initialize_database


def test_api_configuration_and_health_handler_are_available() -> None:
    """Initialize the DB and confirm the FastAPI object exposes its health handler."""
    initialize_database()
    assert app.title == "TradePilot AI"
    assert health_check()["status"] == "ok"
    assert "/health" in app.openapi()["paths"]


def test_live_trading_cannot_be_enabled_in_this_version(monkeypatch) -> None:
    assert Settings(_env_file=None).live_trading_enabled is False
    assert Settings(_env_file=None, live_trading_enabled="false").live_trading_enabled is False
    with pytest.raises(ValidationError):
        Settings(_env_file=None, live_trading_enabled=True)
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
