# Integration and quality audit results

**Audit date:** 2026-11-15  
**Environment:** Windows 11 sandbox, CPython 3.12.0, local SQLite

## Executed checks

| Area | Result |
| --- | --- |
| Runtime and development dependency installation | Installed `requirements-dev.txt`; `pip check` passed. |
| Environment loading | Environment override loaded; missing credentials handled safely; attempts to enable live trading are rejected. |
| Database initialization/migrations | `python -m database.init_db` completed; `alembic check` reported no model/schema drift. |
| FastAPI | Started on `127.0.0.1:8000`; `/health` returned `ok`; `/openapi.json` exposed 22 routes. |
| Streamlit | Started on `127.0.0.1:8501`; `/_stcore/health` returned `ok`; `/` returned HTTP 200. |
| Scheduler | `/scheduler/status` reported `running` and `Asia/Kolkata`. |
| Missing Upstox credentials | `/auth/upstox/login` returned HTTP 503 with a configuration error; no credential was logged or returned. |
| Test suite | `python -m pytest -q`: **128 passed** after the final settings fix. |
| Coverage | `python -m pytest --cov=. --cov-branch --cov-report=term-missing -q`: **72% total branch-aware coverage**. |
| Ruff | `ruff check .`: passed under focused correctness rules in `pyproject.toml`. |
| Dependency consistency | `python -m pip check`: no broken requirements. |
| Dependency audit | `pip-audit -r requirements-dev.txt`: no known vulnerabilities after upgrades. |
| Backup/recovery | SQLite backup integrity and committed-row recovery tests passed. |
| Streamlit pages | All 13 routes rendered in the browser; page headings and core content were visible. |

## Changes made during audit

- Upgraded the supported FastAPI range and enforced patched Starlette versions; moved pytest and HTTPX to development dependencies and set a patched pytest minimum after the initial audit found vulnerable versions.
- Aligned ORM signal uniqueness declarations with the unique indexes already created by the migration; `alembic check` now passes without creating a schema change.
- Fixed an undefined scheduler trigger reference for imported special sessions.
- Rejected in-memory SQLite backup attempts rather than treating `sqlite://` as a file path.
- Fixed live-trading environment parsing: `.env` text `false` is accepted, while environment or code attempts to enable it are rejected.
- Converted the Telegram bot token to `SecretStr`; its value is redacted from settings representation, and empty credentials now leave notifications disabled.
- Added test cases for duplicate paper orders, timeout retry bounds, backup restore/integrity, and live-trading configuration rejection, including string-valued dotenv settings.

## Scenario coverage

Mocked or local automated checks cover OAuth state validation, missing/expired/invalid tokens, request retries/timeouts, malformed and missing market data, duplicate stock/candle/approval/order constraints, signal/strategy validity, insufficient cash, invalid stops, signal expiry, approval rejection, partial exits, stop/target/gap fills, restart restoration, kill-switch behavior, Telegram success/failure, scheduler calendar/dedupe/lease behavior, and reporting exports.

The running local app and missing-credential path were verified over HTTP. Real Upstox OAuth, authenticated history/quote requests, official NSE downloads, and Telegram delivery were **not** exercised because no user credentials or provider authorization were configured. Their service behavior is covered only through mocks. No real broker order was attempted or is implemented.

Secret review confirmed `.env` is ignored by Git, Upstox and AI credentials use `SecretStr`, Telegram now uses `SecretStr`, and Upstox tokens remain in memory. No token or secret is returned from the OAuth callback. Application data queries use SQLAlchemy expressions; the small initialization statements use bound parameters. No user-controlled SQL interpolation was found. Live order API methods are absent, and attempts to set `LIVE_TRADING_ENABLED=true` fail validation.

## Remaining medium and low priority items

### Medium

- FastAPI has no user authentication. The supported deployment is a single local user with both servers bound to loopback; do not bind the API to a LAN or public interface.
- Coverage is uneven: Streamlit page renderers are not directly covered by pytest (manual browser render verification covered all 13 routes), paper routes are about 50%, and the scheduler workflow is about 38% branch-aware statement coverage. Expand automated UI and route integration tests before relying on broad operations.
- Real provider integrations remain unverified without valid Upstox/Telegram credentials and external network access.
- The NSE calendar currently includes verified 2026 exceptions. Import each new official annual calendar; unknown years fail closed. Special sessions without published hours do not inherit regular-session job times.
- Historical backtest reports use the latest result in the active Streamlit session and are not preserved after a session restart.
- Dependency files use version ranges rather than a fully hashed lockfile. Re-run `pip-audit` and regression checks when resolving upgrades.

### Low

- Some market/research presentation sections remain clearly labeled demo data; only the persisted paper analytics and supplied-data backtests represent stored/supplied results.
- Ruff currently selects correctness-focused `E4`, `E7`, `E9`, and `F` checks. Broader style rules such as import sorting and FastAPI dependency-default conventions are not enforced.
- Transaction-cost estimates and paper/backtest fills are simplified models, not broker contract notes or guaranteed execution prices.
