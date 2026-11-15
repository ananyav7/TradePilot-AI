# Cloud deployment

## Services

- **Streamlit Community Cloud** runs `app.py` as the private single-owner interface.
- **Render Web Service** runs FastAPI (`backend.main:app`) behind Render HTTPS.
- **Render Background Worker** runs `python -m scheduler.worker`; it is the only scheduled-job process.
- **Neon or Supabase PostgreSQL** is the shared persistent database. Use its SSL-enabled pooled URL, sized for both Render processes and Streamlit.
- **Upstox** remains read-only for market data. Live broker orders remain disabled.

The Streamlit UI keeps existing SQLAlchemy repositories on the shared PostgreSQL URL to preserve the full current page set; scanner and scheduler requests use the authenticated FastAPI URL. Do not make the Streamlit app public: it is a single-owner workspace and reads the shared portfolio database server-side.

## PostgreSQL

1. Create a PostgreSQL database with SSL enabled (Neon pooled URL or Supabase pooler URL are suitable). Use a dedicated application role with only the permissions needed for this database.
2. Keep the full URL out of Git. Set the exact same `DATABASE_URL` in the Render API, Render worker, and Streamlit Cloud secrets.
3. For Render, use a paid service plan that supports `preDeployCommand`. `render.yaml` runs `python -m database.init_db` once before the API starts. The worker does not run migrations. Never run migrations manually while a deploy migration is active.
4. Back up the existing SQLite file first. With PostgreSQL configured in the process environment and after migrations complete, copy its records using:
   `python scripts/migrate_sqlite_to_postgres.py data/tradepilot.db`
   The utility preserves primary keys and relationships and refuses to copy into non-empty application tables. Review row counts and foreign-key checks before switching users to the cloud database. It never truncates the target.

SQLite remains the local default. Production settings reject SQLite and fail startup/health checks when PostgreSQL is unreachable; there is no fallback to a new empty database.

## Render

Create services from the repository's `render.yaml` or configure these values manually:

- Build: `pip install -r requirements.txt`
- API pre-deploy: `python -m database.init_db`
- API start: `uvicorn backend.main:app --host 0.0.0.0 --port $PORT`
- Worker start: `python -m scheduler.worker`
- Health path: `/health`

Set `DATABASE_URL` (the same external PostgreSQL URL), `UPSTOX_CLIENT_ID`, `UPSTOX_CLIENT_SECRET`, `UPSTOX_TOKEN_ENCRYPTION_KEY`, `UPSTOX_REDIRECT_URI`, and any Telegram credentials in both relevant Render services. Configure `API_BASE_URL` only in Streamlit secrets. Set `API_AUTH_TOKEN` on the API service; Render generates it. Copy that value into Streamlit secrets. The Upstox redirect URI must be `https://<render-api-host>/auth/callback` and must be registered with Upstox.

Generate `UPSTOX_TOKEN_ENCRYPTION_KEY` with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` and set the same value on the Render API and worker. OAuth access tokens are encrypted in PostgreSQL so the worker can use them without sending them to Streamlit. The API has a public database-aware `/health` endpoint. All other routes require `Authorization: Bearer <API_AUTH_TOKEN>` in production, except the Upstox OAuth start/callback endpoints. API logs avoid returning internal exception details to callers.

## Streamlit Community Cloud

Set the entry point to `app.py`, Python 3.12, and add these values under **App settings → Secrets**:

```toml
APP_ENV = "production"
APP_ROLE = "frontend"
DATABASE_URL = "postgresql://...sslmode=require"
API_BASE_URL = "https://<render-api-host>"
API_AUTH_TOKEN = "<copy from Render API service>"
APP_ACCESS_PASSWORD = "<long unique password>"
SCHEDULER_MODE = "external"
```

Use the provider's pooled SSL URL if the direct connection limit is low. Never commit `.streamlit/secrets.toml`. The password gate protects the single-owner Streamlit UI; additionally restrict app sharing to the owner account.

## Local development

Copy `.env.example` to `.env`, keep `APP_ENV=development`, SQLite, and `SCHEDULER_MODE=embedded`. Run `python -m database.init_db`, then `python -m uvicorn backend.main:app --reload --port 8000` and `streamlit run app.py` in separate terminals. Locally, the service token may be blank. Set `API_BASE_URL` if the API is not at `127.0.0.1:8000`.

## Scheduler behavior

The Render web service is configured with `SCHEDULER_MODE=external`; it never starts APScheduler in API workers. The dedicated Render worker runs the current APScheduler workflow in `Asia/Kolkata`. A database heartbeat makes worker liveness visible to the API. Existing unique run keys, PostgreSQL-aware atomic leases, market calendar checks, and source validation remain in place. The scanner endpoint is still available for manual runs. Keep the worker at one instance. Durable job keys and the PostgreSQL pipeline lease suppress duplicate runs if a second process is started accidentally.

## Verification

Before cutover, verify `/health`, authenticated `/scheduler/status`, `/universe/scanner`, `/paper/portfolio`, OAuth callback configuration, database migration version, expected row counts, and app access restriction. The automated suite runs with an isolated SQLite test URL; a live PostgreSQL smoke test requires a separately provisioned test database and `DATABASE_URL`. Do not use a production database for tests.
