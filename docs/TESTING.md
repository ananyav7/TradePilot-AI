# Testing and quality guide

Activate the project environment, then run:

```powershell
python -m pytest -q
python -m pytest --cov --cov-report=term-missing
ruff check .
python -m alembic -c alembic.ini check
python -m pip check
pip-audit -r requirements-dev.txt
```

The suite uses in-memory or temporary SQLite databases and mocked HTTP/provider adapters. It covers database relationships and migrations, OAuth state and token handling, API response validation/retries, universe mapping, technical indicators, strategy/risk decisions, paper order/approval/position behavior, scheduler leases/calendar rules, and report calculations/exports. No test submits an Upstox order.

`tests/test_e2e_simulated_workflow.py` exercises the full deterministic research-to-paper path against a temporary file-backed SQLite database: simulated 50-stock universe scan, indicators, signal generation, risk review, approve/reject, quote-based fills, cash/position/P&L checks, daily report generation, audit/notification records, and a close/reopen restart check. It also covers insufficient cash, stale quotes, malformed signals, duplicate approval/order attempts, and oversized order quantities. Telegram is configured without credentials and its HTTP poster is guarded so the test fails if a network send is attempted.

Coverage is a diagnostic, not a completeness guarantee. Some Streamlit renderers, the scheduler's provider-backed workflow, and deployment/lifespan wiring have lower direct unit coverage than deterministic core engines. Live Upstox and Telegram behavior requires credentials and network access and is not asserted by the mocked suite.

`requirements-dev.txt` installs the optional quality tools. `pyproject.toml` configures pytest, coverage, and Ruff's correctness-focused rules. `pip-audit` requires access to a vulnerability database; record the tool result and date when running it.
