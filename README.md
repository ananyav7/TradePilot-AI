# TradePilot AI

TradePilot AI is a local Python 3.12 research and swing-trading application. Dashboard paper results and market, stock-analysis, signal, and journal views use persisted application records; when records are absent, the interface shows an empty state instead of fabricated market figures or P&L. The NIFTY scanner can retrieve Upstox daily candles after the official universe is loaded and OAuth is authorized, while the Backtesting page runs only on historical data supplied by the user. It does not submit broker orders. Live trading is disabled in configuration.

## Requirements

- Windows 11 (also uses portable Python and pathlib APIs)
- Python 3.12

## Setup

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
python -m database.init_db
```

Edit `.env` only when needed. Never commit credentials. The default SQLite database is created in the project `data` directory when the API starts.

## Run

Start the Streamlit interface at `http://localhost:8501`:

```powershell
python -m streamlit run app.py
```

The terminal has Dashboard Overview, Market Overview, NIFTY 50 Scanner, Stock Analysis, Trading Signals, Trade Approval, Paper Trading, Portfolio, Trade Journal, Backtesting, Performance Analytics, Notifications, and Settings pages. Market and paper figures are source-labelled; absent records produce explicit empty states. Portfolio marks entered by the user are marked with their stated source and timestamp and expire after the configured quote age. The Backtesting page has no built-in market history and requires a user-provided CSV. Trade Approval, Paper Trading, and Portfolio use the persisted SQLite engine.

Start the FastAPI backend in another terminal:

```powershell
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

The API health endpoint is `/health`; interactive API documentation is at `/docs`.

## Verify

```powershell
python -m pytest -q
python -m pytest --cov --cov-report=term-missing
python -m pytest tests/test_e2e_simulated_workflow.py -q
ruff check .
python -m alembic -c alembic.ini check
python -m pip check
pip-audit -r requirements-dev.txt
```

The developer install includes runtime and optional quality dependencies. See [docs/TESTING.md](docs/TESTING.md) for scope and provider test boundaries.

## Configuration

`config/settings.py` loads case-insensitive environment variables and an optional project-root `.env`. `DATABASE_URL` defaults to a local SQLite file. Credentials are blank by default. `LIVE_TRADING_ENABLED` is fixed to false and rejects attempts to enable it; live execution is not implemented.

## Project structure

| Path | Purpose |
| --- | --- |
| `app.py` | Streamlit UI entry point and multi-page navigation. |
| `config/` | Central environment-backed settings. |
| `frontend/` | Shared terminal components, page renderers, repository views, and paper portfolio/order controls. |
| `backend/` | FastAPI app, health endpoint, universe scanner, strategy/research/paper APIs, and scheduler status. |
| `database/` | SQLAlchemy entities, SQLite sessions, Alembic migrations, repositories, and backups. |
| `integrations/` | Boundary for external service adapters such as Upstox and Telegram. |
| `market_data/` | Boundary for market data acquisition and normalization. |
| `strategy/` | Technical indicators, market-condition summaries, configurable swing strategy, and research scanner. |
| `research/` | Evidence collection, deterministic signal explanations, daily summaries, and optional interpretation-only AI adapter. |
| `risk/` | Deterministic sizing, transaction-cost estimates, and the persistent kill switch. |
| `execution/` | SQLite-backed paper order state machine, quote-based simulated fills, position updates, and protective exits; live order execution remains disabled. |
| `portfolio/` | Persistent virtual cash ledger, open position summaries, and realised/unrealised P&L. |
| `notifications/` | Outbound-only Telegram alerts and durable delivery logs. |
| `scheduler/` | APScheduler lifecycle, durable run ledger, NSE calendar, and daily workflow tasks. |
| `backtesting/` | Event-driven historical simulation, cost/slippage accounting, and chronological validation. |
| `reports/` | Source-separated paper analytics, historical backtest reports, and CSV/Excel exports. |
| `docs/` | Windows installation, testing, API integration, user, architecture, and limitations guides. |
| `utils/` | Shared application logging configuration. |
| `tests/` | Pytest package and basic application startup test. |
| `assets/` | Static UI resources; `.gitkeep` preserves the empty directory. |
| `requirements.txt` | Runtime dependency version ranges. |
| `requirements-dev.txt` | Optional coverage, Ruff, and dependency-audit tools. |
| `pyproject.toml` | Pytest, coverage, and Ruff configuration. |
| `.env.example` | Documented environment variable template; copy to `.env`. |
| `.gitignore` | Excludes local secrets, data, caches, and environment files. |
| `README.md` | Setup, run, verification, and architecture documentation. |

## Database layer

The SQLite schema uses SQLAlchemy models and Alembic migrations. Initialize or upgrade the configured database with:

```powershell
python -m database.init_db
```

Create a consistent SQLite online backup with:

```powershell
python -m database.init_db --backup
```

An optional destination can be supplied with `--destination C:\path\backup.db`. Backups default to `data/backups/`. `DATABASE_URL` selects the SQLite file. SQLite foreign key checks are enabled for each application connection. Timestamps are stored as UTC and returned as aware UTC datetimes; `market_date` is stored as a separate SQL `DATE` column. Market candles are unique by stock, interval, and UTC candle time. Approvals and paper orders are unique per source approval/signal. `TradeStatusChanges` records paper order status transitions.

Repositories live in `database/repositories.py`; write methods use transaction scopes and roll back on errors. `database/connection.py` exposes `SessionLocal`, `get_db()`, and `transaction()`.

The schema includes stocks, market data, indicators, trade signals, approvals, paper orders, positions, trade history, watchlists and membership, portfolio, system logs, notification logs, and paper order status audit entries. Migration `0005_paper_trading` adds paper fill linkage, stored position stop/target levels, and the local order states. Migration `0006_manual_approval` stores the reviewed signal version, approved quantity, entry/stop/target, estimated costs, and risk snapshot. Migration `0007_approval_market_quote` records source and timestamp for the reviewed quote. API credentials are not stored in the database.

## Upstox read-only integration

Upstox OAuth and market data are implemented under `integrations/upstox/`. Configure `UPSTOX_CLIENT_ID`, `UPSTOX_CLIENT_SECRET`, and the registered `UPSTOX_REDIRECT_URI` in the local `.env`. With FastAPI running, open `http://127.0.0.1:8000/auth/upstox/login` to start OAuth. Register the exact callback URL from `UPSTOX_REDIRECT_URI` (default `http://localhost:8000/auth/callback`) in the Upstox developer console. The backend validates the returned OAuth state, exchanges the code server-side, and returns only authorization status and expiry; it never returns the bearer token. State and access tokens remain in process memory. Upstox tokens expire at the documented 03:30 IST boundary; this version asks the user to authorize again after expiry. No refresh token or token is written to SQLite or disk.

`MarketDataClient` exposes Historical Candle Data V3, Intraday Candle Data V3, and Full Market Quotes V3 through the official Upstox Python SDK. `InstrumentMasterClient` downloads the official NSE JSON gzip master and caches it locally. Quote snapshots are marked `LIVE_QUOTE`; current-day candles `LIVE_INTRADAY`; historical candles `HISTORICAL`; and the instrument file `REFERENCE_MASTER`. Each response includes a UTC `retrieved_at` and `cache_hit` flag. Standard API rate limits are enforced locally (50 requests/second, 500/minute, 2,000 per 30 minutes); safe read-only requests retry bounded transient failures. No order methods are exposed.

The integration includes an injectable `MockUpstoxAdapter` for tests. No frontend code receives credentials or tokens. Read-only OAuth credentials and Upstox data are not enabled until the `.env` values are configured.

## NIFTY 50 universe and scanner

`market_data/universe.py` loads the official NIFTY 50 CSV linked from the [NSE NIFTY 50 index page](https://www.nseindia.com/static/products-services/indices-nifty50-index). The source is not bundled as a static list: refresh downloads it at the user's request, requires exactly 50 distinct symbols, and retains the last valid database snapshot if the source is unavailable or malformed. The UI displays the source and UTC update timestamp. CSV import/export supports local lists. Upstox instrument keys are resolved dynamically from the official NSE instrument master, accepting only an unambiguous NSE cash-equity (`NSE_EQ`, `EQ`) match. Missing or ambiguous mappings remain unavailable and cannot be enabled for scans.

Stock records track NIFTY membership, mapping availability, source/update time, and enabled state. Membership refresh records additions/removals without deleting stocks or overriding enabled state for removed constituents. Manual additions use the separate `Manual watchlist`, and the existing unique NSE symbol constraint prevents duplicate stocks. Run `python -m database.init_db` after upgrading; migration `0002_stock_universe` adds the universe columns.

The scanner implementation is in `strategy/scanner.py`; it requests daily V3 candles from the FastAPI backend at `/universe/scanner` for enabled stocks with validated mappings. It requires at least 60 complete unique daily candles with no gaps over ten calendar days and records source/retrieval metadata. The shared analysis engine is in `strategy/technical_analysis.py` and `strategy/indicators.py`; it validates and sorts OHLCV, drops incomplete rows without filling, excludes the as-of date in end-of-day mode unless completion is explicitly confirmed, and calculates EMA20/EMA50, Wilder RSI14, Wilder ATR14, 20-session volume SMA, relative volume against the prior 20 sessions, daily percentage change, and prior-20-session support/resistance. The `ta` package supplies EMA, RSI, and ATR implementations when installed; equivalent local Pandas/NumPy formulas keep these calculations available in restricted/offline environments. `strategy/market_conditions.py` creates descriptive trend, volatility, momentum, and volume labels. Rolling values are trailing-only; support/resistance exclude the current candle. No missing values are backfilled. `TechnicalAnalysisResult.plotly_frame` supplies timestamped OHLCV and indicator columns for charting.

The research scanner classifications are: bullish when close > EMA20 > EMA50, RSI 50–70, and volume is at least 1.5x average; bearish when close < EMA20 < EMA50 and RSI <50; otherwise neutral. Classified directions show an illustrative 2-ATR stop, 3-ATR target, and derived 1.5:1 risk/reward. These are informational scanner levels and do not submit or authorize orders. Data errors and incomplete history appear separately from valid scan results. Filters cover direction, strong volume, RSI, EMA trend, minimum risk/reward, and sorting.

Indicator methodology follows the `ta` project’s [EMA](https://github.com/bukosabino/ta/blob/master/ta/trend.py), [RSI](https://github.com/bukosabino/ta/blob/master/ta/momentum.py), and [ATR](https://github.com/bukosabino/ta/blob/master/ta/volatility.py) implementations. Expected-value tests also calculate reference results independently to guard against look-ahead and formula drift.

## Experimental swing strategy

`strategy/swing_strategy.py` evaluates completed end-of-day `TechnicalAnalysisResult` objects only. User-adjustable thresholds and market filters live in `strategy/strategy_config.toml`; restart the application after editing. Defaults require EMA20 > EMA50, close > EMA20, RSI 50–65 inclusive, relative volume >1.2, and configured trend/volatility/momentum/volume/ATR-percent filters. A qualifying BUY includes a 2-ATR stop, 3-ATR target, 1.5:1 reference risk/reward, a three-calendar-day expiry, and no quantity until sizing is implemented. All rules are experimental and do not guarantee outcomes.

Trigger a scan for enabled, mapped stocks using the authorized FastAPI backend with `POST http://127.0.0.1:8000/strategy/signals/generate`; optionally pass `?symbol=RELIANCE` for one symbol. It uses Upstox historical daily candles, excludes the current as-of market date, stores BUY, HOLD, and NO TRADE snapshots in SQLite, and returns individual data failures separately. Migrations `0003_swing_strategy_signals` and `0004_trailing_high_watermark` add the reproducible indicator/exit snapshot and a unique `(stock_id, candle_at)` constraint. Repeating a generation request for the same completed candle returns the prior row rather than creating a second signal.

For an open long position, pass its previously stored stop, target, trailing stop, and high-watermark as `OpenPositionContext`. Exit checks use completed-candle low/high/close. When one daily candle crosses both stop and target, the engine conservatively reports the stop first because intraday ordering is unavailable. It reports the required signal type `HOLD` with `recommended_action=EXIT` and an exit reason; no exit order is sent. The next trailing stop and high-watermark are returned for the next completed candle and persisted with the signal snapshot. Manual exit is an explicit input. BUY remains a research signal awaiting separate user approval.

## Historical backtesting

`backtesting/engine.py` simulates the configured swing strategy over validated daily OHLCV candles. Signals use completed candles and enter at the next session's open with adverse configurable slippage. Daily stop/target hits are simulated from OHLC; if one candle touches both, the stop is assumed to occur first. Strategy invalidation is evaluated at candle close and exits at the following open. Any final open position is liquidated at the final close. The simulator enforces one position, shares the configured risk limits and transaction-fee estimates, tracks cash/equity, and records each completed trade, costs, slippage, holding sessions, and entry regime.

`backtesting/validation.py` supplies chronological holdout and expanding-window walk-forward evaluation. In a holdout, earlier rows warm causal indicators, but trades begin at the later test boundary. Walk-forward parameters stay fixed; there is no automated parameter search or test-set optimization. Results include equity, drawdown, monthly return, trade distribution, regime breakdown, and requested performance metrics. `warnings` flags histories below 252 test bars or 30 trades by default; these are context thresholds, not statistical proof of validity.

The Backtesting page accepts a user-uploaded CSV with a date or timestamp plus open, high, low, close, and volume columns. Confirm that its source is an authorized NSE historical-data provider and that its last candle is complete; the app does not validate the publisher or fetch missing prices. Charges use configurable estimate rates, and slippage is separately configurable; neither reproduces all market microstructure or guarantees actual fills. Historical simulation is not a forecast and does not guarantee future returns.

## AI-assisted market research

`research/` explains the immutable stored signal snapshot and aggregates explicitly sourced news. The deterministic report includes signal type and rationale, EMA20/EMA50 and close comparisons, RSI against configured thresholds, relative volume against its threshold, and the per-share reward/risk formula. NO TRADE levels are labeled hypothetical. Facts and source records remain separate from the optional `ai_interpretation` field. This module cannot change signals or parameters and exposes no approval or execution operation.

Read-only API endpoints are `GET /research/signals/{signal_id}` and `GET /research/daily-summary?market_date=YYYY-MM-DD`. Source records carry publisher, HTTPS link, publication timestamp when supplied, and UTC retrieval timestamp. An RSS item's absence does not prove that no announcement or news exists. Retrieval failures and unconfigured feeds are surfaced as coverage uncertainty, never replaced with generated headlines.

AI is disabled by default. Optional configuration uses `AI_PROVIDER=openai`, `AI_MODEL=...`, and `AI_API_KEY=...`; without a key or model, deterministic Python explanations remain available. The OpenAI adapter uses the Responses API without tools and requests non-persisted output. Only stored signal facts and selected public evidence are sent; broker tokens, API credentials, and portfolio/account details are never included. Retrieved titles and summaries are treated as untrusted data. Output must match a narrow schema, cite only supplied evidence IDs, and cannot include trading action language; malformed/provider-failure output is discarded while deterministic facts remain. AI can add context and uncertainty notes only; the strategy engine remains the sole source of signal parameters.

Market-news RSS URLs are configurable with `RESEARCH_MARKET_NEWS_FEEDS`. The default is the verified Business Standard Markets RSS feed (`https://www.business-standard.com/rss/markets-106.rss`). Company-announcement URLs are deliberately not guessed: configure verified feeds by symbol in `RESEARCH_ANNOUNCEMENT_FEEDS` as JSON, for example `{"RELIANCE":["https://verified-publisher.example/feed.xml"]}`. NSE documents RSS feeds for corporate announcements, but this application requires the actual feed URL to be supplied explicitly. Feed responses are cached in process for five minutes and requests have bounded sizes and timeouts; RSS is not a guarantee of complete or real-time coverage.

## Deterministic risk engine

`risk/engine.py` sizes long-only delivery positions using Decimal arithmetic and whole-share rounding. The default ₹3,000 capital and 0.5% risk budget cap estimated loss at the stop (price loss plus estimated round-trip costs) to ₹15 per trade; configured quantity is reduced further to satisfy buy-side estimated charges, available cash, and remaining exposure. The assessment reports stop and target distances, risk/reward, capital required, buy and round-trip costs, gross/net expected reward, and exposure. A proposal is rejected if its inputs are invalid, its market snapshot is missing/stale/future-dated, risk/reward is below 1.5:1, a position is already open, the daily loss limit is reached, cash/exposure is insufficient, or the kill switch is active. The engine cannot place orders and explicitly rejects leverage or derivatives.

Risk limits and fee assumptions are environment-configurable with the `RISK_*` variables in `.env.example`. Fee defaults are estimates for planning, not a broker quote; verify them against current Upstox contract notes and applicable charges before relying on net reward. Cost estimates include configurable brokerage, sell-side STT, exchange and SEBI fees, GST, buy-side stamp duty, and optional depository sell fee. Daily loss is supplied to each assessment as the current day's combined P&L; callers must provide accurate account/cash/exposure values and completed market-data timestamp. The kill switch state is stored at `data/risk_kill_switch.json`, fails closed if unreadable, remains active until an explicit manual deactivation with a reason, and is ignored only when no state file exists. It is not automatically reset when a new day begins.

## Paper trading

`execution/paper_engine.py` is a local-only paper execution adapter. BUY orders must reference a persisted BUY signal approval and move through `PENDING` → `APPROVED` → `PARTIALLY_FILLED`/`FILLED`; rejection, cancellation, and expiry are also audited states. Manual SELL orders require explicit approval. Fills accept a timestamped quote with source and optional bid/ask, use the executable side when supplied, then apply adverse configurable slippage. Cash affordability, one-open-position limit, stop/target geometry, current risk limits, and estimated charges are checked before each BUY fill.

`execution/position_manager.py` updates weighted average entry, partial quantity, protective levels, cash deltas, and realised P&L atomically with a fill record. `portfolio/portfolio_manager.py` reconstructs balances from the persistent fill ledger after restart; unrealised P&L is shown only when a caller supplies a current price. Stop monitoring accepts completed sourced OHLC bars. A gap through a stop uses the observed open plus adverse slippage; a target gap can receive the observed open less slippage. If both stop and target are touched in one daily bar, the stop is assumed first because OHLC cannot identify intraday order. Order states and fills are stored in SQLite.

FastAPI exposes local paper operations under `/paper`: portfolio view, versioned signal review and decision, BUY/SELL order creation, order lifecycle, quote fills, manual exits, protective-bar monitoring, and a manual daily report. `/paper/approvals/opportunities` lists unreviewed BUY candidates. Call `POST /paper/approvals/{signal_key}/review` with a timestamped, sourced fresh quote to run current risk checks and persist a review snapshot. Then call `POST /paper/approvals/{approval_id}/decision` with that signal version and either `reject` or `approve`. Approval requires a fresh quote, a quantity no greater than the current risk-qualified maximum, and `confirm_text` exactly equal to `APPROVE {signal_key}`. Approval creates an `APPROVED` local paper order; a later sourced quote is still required to simulate its fill. Rejects and expiry are persisted with decision timestamps, and each signal can be reviewed only once. The Streamlit Trade Approval, Paper Trading, and Portfolio pages use the persisted engine. These interfaces require caller-supplied sourced prices; they do not fetch or invent quotes. The paper engine makes no Upstox order calls, and live trading remains disabled.

Telegram alerts use the official Bot API `sendMessage` method only ([Telegram Bot API](https://core.telegram.org/bots/api#sendmessage)). Configure `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` locally; values are never stored in SQLite or sent to the frontend. Alerts cover risk-qualified new opportunities, expired signals, recorded approvals/rejections, completed paper orders, stop/target exits, and a user-triggered daily paper report. The integration has no polling, webhook, or incoming-message handler, so Telegram cannot approve or execute trades. Failed alert delivery is logged and does not undo a paper decision or fill. Telegram sends are tested with a mocked HTTP transport.

## Daily automation scheduler

`scheduler/service.py` runs APScheduler 3.x in the FastAPI lifespan using `Asia/Kolkata`. The daily workflow runs at 08:30 connectivity check, 08:45 official instrument/universe refresh when the instrument cache requires it, 09:00 quote availability review, 15:45 completed daily-candle retrieval, 16:00 indicator calculation, 16:05 NIFTY 50 scan, 16:10 signal generation and deterministic risk validation, 16:15 dashboard-ledger update, and 16:20 Telegram daily report. Paper-position quote monitoring runs every `SCHEDULER_MONITOR_INTERVAL_MINUTES` during the NSE session; it sends stop/target alerts only and never creates an exit order.

`database/migrations/versions/0008_daily_scheduler.py` persists reviewed calendar years, exchange exceptions, and each scheduled operation. `0009_scheduler_leases` adds an atomic SQLite lease across processes for the candle/indicator/scan/signal pipeline. Daily job keys are unique, completed work is not repeated, failed or interrupted work can be retried, and API restarts reconstruct the APScheduler jobs and asynchronously catch up today's past stages. All stored run timestamps are UTC. A scheduler startup or provider failure is logged and shown on the dashboard while manual pages remain usable.

The scheduler uses the official NSE equity holiday calendar. The bundled 2026 exceptions come from NSE's [Market Timings & Holidays page](https://www.nseindia.com/resources/exchange-communication-holidays) and its [2026 holiday circular](https://nsearchives.nseindia.com/content/circulars/CMTR71775.pdf); the NSE-noted Muhurat session on 8 November is marked as a special session. Standard daily jobs do not run on weekends or holidays. Special sessions are excluded from the fixed 08:30–16:20 workflow; if an official special-session open/close time is imported, market-hour quote monitoring follows that session. Unknown calendar years fail closed and require a complete official NSE CSV import from the Dashboard scheduler panel. This avoids assuming weekdays are trading days or relying on a stale future calendar.

At 15:45 the pipeline stores only validated completed daily candles. Stocks without a current-date candle, complete OHLCV, or the 60-session indicator warm-up are omitted from downstream indicators and cannot produce a scheduled signal. The scanner uses those persisted candles, so the scheduler does not claim missing data is neutral or valid. Each phase records status, counts, failures, and UTC timestamps; `/scheduler/status` and the Dashboard show next runs, the calendar source, and recent outcomes. No scheduler task submits broker orders.

## Reporting and analytics

`reports/service.py` builds analytics from the persisted SQLite paper fill ledger and from a single explicitly supplied historical backtest result. Paper daily, Monday-to-date weekly, month-to-date, individual exit-fill, strategy, and open risk exposure reports are downloadable as CSV or Excel from **Performance Analytics**. Excel workbooks include report metadata and UTC generation time; backtest workbooks also include a separate performance summary sheet. INR amount columns are formatted as rupees in Excel.

Paper P&L and historical backtest P&L are never combined. Each export carries a result-type label, source, and UTC generation timestamp. Paper trades include quote provenance classified as live, simulated, historical, or unclassified, and paper results are also grouped by that provenance; a live quote used to simulate a paper fill remains a paper result, not live trading P&L. Backtest source provenance is kept separately. Unrealized P&L is reported as unavailable when no validated current quote is present. Metrics use realized paper exit fills and include daily/weekly/monthly P&L, return on starting capital, deployed and available capital, win rate, profit factor, average realized R multiple, realized-equity drawdown, holding period, charges/slippage, and strategy version performance.

## Safety and scope

Paper trading and historical backtesting are local simulations. They do not reproduce all market microstructure or guarantee fills or outcomes. No live broker order functionality is present; read-only Upstox market-data integration remains separate from paper execution.

## Guides

- [Installation](docs/INSTALLATION.md)
- [Testing and quality](docs/TESTING.md)
- [API and provider integration](docs/API_INTEGRATION.md)
- [User guide](docs/USER_GUIDE.md)
- [System architecture](docs/ARCHITECTURE.md)
- [Known limitations](docs/KNOWN_LIMITATIONS.md)
#   T r a d e P i l o t - A I  
 