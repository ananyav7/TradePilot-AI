# User guide

1. Start FastAPI and Streamlit using [INSTALLATION.md](INSTALLATION.md). Without credentials, use sample-labeled research pages and local paper-account features.
2. To retrieve NSE market data, configure Upstox OAuth and authorize through the backend. Refresh the NIFTY 50 universe and verify the displayed source/update timestamp and instrument mappings before scanning.
3. Review scanner completeness/errors, then generate strategy signals. BUY signals are candidates, not orders.
4. On Trade Approval, open a candidate, review its source timestamp and deterministic risk assessment, then explicitly approve or reject using the UI confirmation. Approval creates only a local paper order.
5. Paper orders require a fresh sourced quote for simulated fills. Inspect fills, partial exits, positions, cash, and trade history in Paper Trading and Portfolio. Do not use a fabricated or stale quote.
6. Backtesting requires a user-provided historical OHLCV CSV. Verify its publisher, date range, and completed candles. Backtest results are historical simulations, not forecasts.
7. Performance Analytics and the Dashboard show persisted paper ledger results. Download daily, weekly, monthly, individual fill, strategy, risk exposure, and latest session backtest reports as CSV or Excel. Result types and market-data provenance are labeled.
8. Telegram can send alerts but cannot receive commands or approve/execute trades. The kill switch and configured risk limits should be reviewed before any paper testing.

Sample symbols and synthetic charts are presentation examples, not market recommendations. No live orders are available.
