# Known limitations

## Integration and data

- Real Upstox OAuth, live quote/candle retrieval, instrument-master download, and Telegram delivery were not verified in this audit because the environment has no provider credentials. Mock adapters are covered by tests.
- The NIFTY scanner uses current NSE constituent data and validated Upstox mapping only after a user-triggered refresh and authorization; no prices or instrument keys are bundled as live facts.
- Historical backtesting accepts user-supplied OHLCV. The app cannot verify the publisher or completeness beyond structural checks.
- The NSE calendar is verified for its bundled year; new official calendar years and special-session times must be imported. Unknown years fail closed.
- Some market, scanner, journal, and dashboard presentation sections remain explicitly labeled demo samples. They must not be interpreted as live account data.

## Operations and security

- SQLite is designed for local single-user development, not multi-user or horizontally scaled deployment. There is no login/session authorization layer on FastAPI. Keep both services on loopback.
- OAuth access tokens are in process memory and require reauthorization after expiry/restart. There is no refresh-token persistence.
- The local scheduler is best effort and depends on the API process remaining running. Manual UI/API work remains available if it stops.
- No broker order placement, modification, or live execution path is implemented. `LIVE_TRADING_ENABLED` defaults false and must remain false.
- Fee estimates, slippage, daily-bar fill ordering, and simulated execution cannot reproduce all exchange/broker market microstructure.

## Audit coverage

- Provider workflows are primarily unit-tested with mocks; external authentication and retrieval require a credentialed manual check.
- Streamlit render functions do not have broad direct UI automation coverage. API route coverage and deterministic core-function coverage are stronger.
- Backtest report state is kept in the Streamlit session; it is not persisted across a session restart.
- Dependency vulnerability results depend on the selected advisory service and the date of the scan.
