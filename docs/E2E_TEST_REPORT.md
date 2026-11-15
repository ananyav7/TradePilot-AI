# Simulated end-to-end test report

**Run date:** 2026-11-15  
**Runtime:** Windows 11, CPython 3.12, SQLite  
**Test:** `tests/test_e2e_simulated_workflow.py::test_complete_simulated_nifty_to_paper_trade_restart_workflow`

## Result

| Check | Result | Evidence |
| --- | --- | --- |
| Application startup | PASS | FastAPI `/health` returned HTTP 200 with `status: ok`; Streamlit `/_stcore/health` returned HTTP 200 with `ok`. |
| Simulated universe load | PASS | 50 temporary SQLite stock records loaded with unique simulated instrument mappings. These are synthetic test constituents, not an assertion about the current official NIFTY 50 membership. |
| Historical candles and scan | PASS | Provider made 50 simulated read calls. 49 stocks had complete 80-candle histories and were classified Bullish; the final stock had no candles and was reported `Insufficient data`. |
| Indicators and signals | PASS | EMA 20/50, RSI 14, ATR 14, 20-day volume mean, and relative volume calculated; three completed-candle BUY signals were persisted. |
| Risk and invalid cases | PASS | ₹15 maximum risk was checked; zero cash returned a rejected risk assessment; stale quote and a BUY signal without a stop were rejected; duplicate review and oversized order were rejected. |
| Human decisions | PASS | One signal was explicitly approved and one rejected. The rejected signal created no order. |
| Duplicate-order guard | PASS | A second order for the same approval was blocked by SQLite's unique constraint; the order count remained one before the exit order was created. |
| Buy execution | PASS | Approved simulated BUY filled 3 shares at ₹115.3152. Estimated buy charges were ₹0.19 and available cash became ₹2,653.86. |
| Unrealised P&L | PASS | At a simulated ₹116.3152 mark, the open 3-share position showed ₹3.00 unrealised price P&L. |
| Exit and realised P&L | PASS | Simulated SELL filled at ₹124.5288; sell charges were ₹0.52; gross P&L was ₹27.6408 and net realised P&L after buy/sell charges was ₹26.9308. |
| Cash reconciliation | PASS | Closed portfolio cash was ₹3,026.93, equal to initial ₹3,000 plus rounded realised net P&L. Position quantity became zero and status closed. |
| Journal and daily report | PASS | One simulated-provenance paper trade appeared in the journal and daily report with net P&L ₹26.9308. |
| Audit and notification records | PASS | SQLite retained two approval decisions, two fill records, six order status transitions, and six disabled Telegram notification records. No Telegram HTTP request was made. |
| Restart persistence | PASS | Closed and reopened the temporary SQLite file through a fresh SQLAlchemy session; signals, approvals, paper orders, fill history, notification logs, statuses, and final balance remained available. |
| Real broker order safety | PASS | The workflow instantiated the local paper adapter and simulated data provider only. No Upstox order client is attached to the paper engine and no broker order was submitted. |

## Executed test evidence

```text
python -m pytest tests/test_e2e_simulated_workflow.py -q
1 passed (included in the full regression run)

python -m pytest -q
129 passed in 28.02s

ruff check .
All checks passed!

python -m alembic -c alembic.ini check
No new upgrade operations detected.
```

## Configuration note

The default experimental strategy uses a 3 ATR target and a 2 ATR stop, exactly 1.5:1 before costs. With 10 basis points of adverse paper slippage, a simulated fill can reduce the reward/risk below the configured 1.5 minimum; the execution-time risk recheck correctly refuses such a fill. To exercise the successful fill and exit path without weakening risk limits, this test uses a versioned test strategy configuration with a 4 ATR target. The production strategy configuration was not changed.

## Scope limits

The test uses synthetic `SIM##` stocks and deterministic generated OHLCV, not current exchange constituents, Upstox responses, or real market prices. The journey is driven through the existing services and repositories rather than clicking each Streamlit form; the application health endpoints were independently checked on loopback. It does not claim that live provider access or Telegram delivery works. The test database is temporary and does not modify the user's configured local paper account.
