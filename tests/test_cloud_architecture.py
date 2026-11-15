"""Production API access and cloud configuration guardrails."""
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
import pytest

import backend.main as backend_main
from config.settings import Settings


def test_private_api_requires_bearer_token_in_production(monkeypatch):
    monkeypatch.setattr(backend_main.settings, "app_env", "production")
    monkeypatch.setattr(backend_main.settings, "api_auth_token", SecretStr("a" * 40))
    client = TestClient(backend_main.app)
    assert client.get("/scheduler/status").status_code == 401
    response = client.get("/scheduler/status", headers={"Authorization": f"Bearer {'a' * 40}"})
    assert response.status_code == 200


def test_health_route_remains_public_and_reports_database_failure(monkeypatch):
    monkeypatch.setattr(backend_main.settings, "app_env", "production")
    monkeypatch.setattr(backend_main, "database_health", lambda: False)
    response = TestClient(backend_main.app).get("/health")
    assert response.status_code == 503
    assert response.json()["database"] == "down"


def test_production_rejects_sqlite_and_requires_role_specific_secrets():
    with pytest.raises(ValidationError, match="Production requires a PostgreSQL"):
        Settings(_env_file=None, app_env="production")

    frontend = Settings(_env_file=None, app_env="production", app_role="frontend",
                        database_url="postgresql://user:pass@db.example/app",
                        app_access_password="x" * 16)
    assert frontend.app_role == "frontend"
    with pytest.raises(ValidationError, match="API_AUTH_TOKEN"):
        Settings(_env_file=None, app_env="production", app_role="api",
                 database_url="postgresql://user:pass@db.example/app")


def test_streamlit_api_client_uses_configured_https_url_and_bearer(monkeypatch):
    from types import SimpleNamespace
    from frontend import api_client

    observed = {}

    class Response:
        ok = True
        status_code = 200

        def raise_for_status(self):
            return None

    def fake_request(method, url, *, headers, timeout, **kwargs):
        observed.update(method=method, url=url, headers=headers, timeout=timeout)
        return Response()

    monkeypatch.setattr(api_client, "_secret", lambda name, default="": {
        "API_BASE_URL": "https://api.example.test/", "API_AUTH_TOKEN": "service-token",
    }.get(name, default))
    monkeypatch.setattr(api_client, "api_base_url", lambda: "https://api.example.test")
    monkeypatch.setattr(api_client, "get_settings", lambda: SimpleNamespace(
        app_env="production", api_auth_token=SecretStr(""), api_request_timeout_seconds=9))
    monkeypatch.setattr(api_client.requests, "request", fake_request)

    response = api_client.api_request("GET", "/paper/portfolio")
    assert response.status_code == 200
    assert observed["url"] == "https://api.example.test/paper/portfolio"
    assert observed["headers"]["Authorization"] == "Bearer service-token"
    assert observed["timeout"] == 9


def test_postgresql_urls_use_psycopg_ssl_and_bounded_pool(monkeypatch):
    import database.connection as connection

    observed = {}
    sentinel = object()

    def fake_create_engine(url, **kwargs):
        observed.update(url=url, kwargs=kwargs)
        return sentinel

    monkeypatch.setattr(connection, "create_engine", fake_create_engine)
    result = connection._build_engine("postgresql://user:secret@db.example.test/tradepilot")
    assert result is sentinel
    assert observed["url"].startswith("postgresql+psycopg://")
    assert "sslmode=require" in observed["url"]
    assert observed["kwargs"]["pool_size"] == 5
    assert observed["kwargs"]["max_overflow"] == 5
    assert observed["kwargs"]["connect_args"]["prepare_threshold"] is None


def test_external_scheduler_status_uses_worker_heartbeat(monkeypatch):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    import scheduler.service as service
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from database.connection import Base
    from database.models import SchedulerWorkerHeartbeat

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(service, "SessionLocal", factory)
    monkeypatch.setattr(service, "_active_service", None)
    monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(scheduler_mode="external"))
    with factory() as session:
        row = session.get(SchedulerWorkerHeartbeat, "render-market-worker")
        if row is None:
            row = SchedulerWorkerHeartbeat(worker_id="render-market-worker", status="running",
                                           last_seen=datetime.now(timezone.utc))
            session.add(row)
        else:
            row.status = "running"
            row.last_seen = datetime.now(timezone.utc)
        session.commit()
    response = service.scheduler_status()
    assert response["running"] is True
    assert response["state"] == "running"
    engine.dispose()
