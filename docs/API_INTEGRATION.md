# API integration guide

## Local FastAPI

Run the backend with the command in [INSTALLATION.md](INSTALLATION.md). It binds to loopback by default. Verify `/health` and inspect `/docs` for current request schemas. The API does not implement authentication for local clients; keep it bound to `127.0.0.1` and do not expose it to a network.

## Upstox

Set `UPSTOX_CLIENT_ID`, `UPSTOX_CLIENT_SECRET`, and the exact registered `UPSTOX_REDIRECT_URI` in `.env`. Start FastAPI and visit `/auth/upstox/login`. OAuth state and access tokens are held in process memory. Read-only market-data methods use Upstox SDK contracts for historical candles, intraday candles, full quotes, and instrument master data. Missing credentials, expired tokens, timeouts, and malformed provider responses are reported as integration failures; no data is synthesized as a fallback.

Unit tests use `MockUpstoxAdapter`. An end-to-end OAuth/candle/quote check cannot be completed without a registered developer application, credentials, user authorization, and network access. There is no order-placement or order-modification endpoint in this application.

## Application endpoints

- `GET /health`
- `GET /scheduler/status`
- `POST /scheduler/calendar/import`
- `/auth/upstox/*` for local OAuth
- `/universe/*` for constituent import/refresh and scanning
- `/strategy/*` for stored signal generation and lookup
- `/research/*` for sourced explanations and daily summaries
- `/paper/*` for review, explicit decisions, paper orders, fills, portfolio, and monitoring

Use `/docs` as the authoritative current schema. Paper routes accept timestamped, sourced prices and cannot call a broker order API. Telegram is outbound alerting only.

## External providers

Telegram requires `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`; tests mock its HTTP transport. Optional AI explanations require `AI_PROVIDER`, `AI_MODEL`, and `AI_API_KEY`. AI is disabled by default and does not control signals, approvals, or orders.
