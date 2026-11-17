# AI Spot Trading Agent (simulation only)

A locally deployable, AI-assisted cryptocurrency **spot** trading platform built on Binance market data. It:

* discovers the top-10 eligible USDT pairs;
* analyses them with technical indicators and multi-timeframe rules (plus an optional ML filter);
* sizes every trade through an independent risk engine;
* executes trades **only** in a local paper-trading engine or on the **Binance Spot Testnet**;
* tracks the portfolio, P&L, risk limits and history in a React dashboard.

> **No real money.** Live trading does not exist in this release. The configuration rejects any mode other than `PAPER` or `TESTNET`, credentials are only ever sent to `testnet.binance.vision`, and market data comes from Binance's read-only data hosts. Default mode is **PAPER**.
>
> Nothing here is financial advice. The strategy's "confidence" is a rule-based score, not a probability of profit. No claim is made that the strategy is profitable; backtest results are hypothetical.

## Contents

- [Features](#features)
- [Quick start with Docker](#quick-start-with-docker)
- [Running without Docker](#running-without-docker)
- [Binance Spot Testnet](#binance-spot-testnet)
- [Configuration](#configuration)
- [Using the dashboard](#using-the-dashboard)
- [API](#api)
- [Testing](#testing)
- [Project layout](#project-layout)
- [Known limitations](#known-limitations)

Architecture details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

## Features

| Area | Highlights |
|---|---|
| **Market data** | Binance REST and combined WebSocket streams (trade, 24h ticker, kline, book ticker). Reconnects with exponential backoff and jitter, has a heartbeat watchdog, validates payloads, de-duplicates out-of-order events, detects stale data, and backfills gaps after a reconnect. Only *closed* candles are stored (one per symbol, timeframe and open time). |
| **Discovery** | Top-N USDT pairs by 24h quote volume. Excludes stablecoins, leveraged tokens, tokenized stocks/ETFs, suspended or delisted markets, pairs failing filters or minimum-notional checks, and pairs with insufficient history. Testnet availability is *reported* per pair, never silently substituted. |
| **Analysis** | RSI(14), EMA 9/20/50/200, MACD(12,26,9), Bollinger(20,2), ATR(14), ADX(14) and volume MA(20), implemented with TA-Lib-compatible definitions and verified against reference implementations. Classifies trend, overbought/oversold, volume and volatility, and finds support/resistance. Signals use weighted scoring plus hard gates. An optional Gradient Boosting / Random Forest filter uses walk-forward validation and is only used if it beats the base rate out-of-sample. |
| **Risk** | Risk per trade, maximum open positions, daily loss limit and maximum drawdown (both *hard halts* that need an explicit reset), per-position and total allocation, minimum reward:risk *after costs*, maximum order size, consecutive-loss cooldown, per-symbol cooldown, volatility limit, stale-data and connectivity guards. ATR- or percent-based stops, reward:risk or percent targets, and an optional trailing stop. |
| **Execution** | Unique client order IDs, idempotency keys, persist-before-submit, reconciliation of unknown outcomes, partial fills, rejections, limit-order timeouts, fee accounting (quote, base or BNB) and slippage tracking. The paper engine walks the **live order book**. |
| **Portfolio** | Exact `Decimal` accounting throughout: equity, available balance, invested capital, realised/unrealised P&L, fees, win rate, profit factor and drawdown. |
| **Backtesting** | Chronological, no look-ahead (verified by a test that changes future data), next-bar-open fills, fees and slippage, stop-first rule for intrabar SL/TP. Reports Sharpe ratio, profit factor and drawdown. |
| **Dashboard** | 11 pages (the 10 specified plus Backtesting), dark/light mode, live WebSocket updates, an always-visible environment banner, and TradingView Lightweight Charts. |
| **Security** | Argon2 password hashing, JWT auth on every endpoint and WebSocket, admin-only write endpoints, rate limiting, restricted CORS, secret redaction in logs, fail-closed configuration, and non-root containers. |

## Quick start with Docker

### 1. Install Docker Desktop

Download it from <https://www.docker.com/products/docker-desktop/>. On Linux you can install Docker Engine with the Compose plugin instead.

### 2. Clone the project

```bash
git clone <your-repo-url> trading_bot
cd trading_bot
```

### 3. Configure environment variables

Copy the example file:

**Windows PowerShell**
```powershell
Copy-Item .env.example .env
```

**Linux / macOS**
```bash
cp .env.example .env
```

Generate a random JWT secret:

**Windows PowerShell**
```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

**Linux / macOS**
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Then edit `.env` and set at least:

```ini
JWT_SECRET=<paste the generated value>
ADMIN_PASSWORD=<a password of 10+ characters>
POSTGRES_PASSWORD=<a database password>
```

The app **refuses to start** if `JWT_SECRET` or `ADMIN_PASSWORD` is missing, too short, or still a `change-me` placeholder.

### 4. Get Binance Spot Testnet credentials (optional)

Paper trading needs no keys. For testnet trading, see [Binance Spot Testnet](#binance-spot-testnet).

### 5. Start the services

```bash
docker compose up -d --build
docker compose ps          # wait until backend shows "healthy"
```

This starts `postgres`, `redis`, `backend`, `trading-worker` and `frontend`.

### 6. Run the database migrations

The backend container runs `alembic upgrade head` automatically on start. To run it manually:

```bash
docker compose exec backend alembic upgrade head
```

### 7. Open the dashboard

| URL | What |
|---|---|
| <http://localhost:5173> | Dashboard (sign in as `ADMIN_USERNAME` / `ADMIN_PASSWORD`) |
| <http://localhost:8000/docs> | API documentation |

If port 8000 or 5173 is already taken, set `BACKEND_PORT` or `FRONTEND_PORT` in `.env`.

### 8. Enable paper trading

The engine starts in **STOPPED**. Choose either option:

* Press **Start** on the Dashboard or the System Settings page.
* Call the API:
  ```bash
  curl -X POST http://localhost:8000/api/system/start -H "Authorization: Bearer <token>"
  ```

Within about a minute the worker discovers the top 10 pairs, backfills candles and begins analysing on every closed 5-minute candle. Trades happen only when every entry condition and every risk check passes, which can take a while — this is by design.

### 9. Connect to the testnet when desired

1. Add the testnet keys to `.env`.
2. Run `docker compose up -d` to apply them.
3. **Stop** the engine.
4. Open **System Settings → Trading mode** and choose *Binance Spot Testnet*.
5. **Start** the engine again.

Logs and shutdown:

```bash
docker compose logs -f trading-worker      # worker activity
docker compose down                        # stop (data kept in the pgdata volume)
docker compose down -v                     # stop AND delete all data
```

## Running without Docker

This mode needs Python 3.12+ and Node 20+. SQLite works for a quick start, and the worker runs inside the API process (`RUN_WORKER_IN_API=true`), so neither Redis nor PostgreSQL is required. If you use SQLite, `Decimal` values are stored as exact strings, so precision is not lost.

**Windows PowerShell**
```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
# Uses ..\.env. Either set DATABASE_URL in it, or for this shell only:
$env:DATABASE_URL = "sqlite:///./data/trading.db"
alembic upgrade head
python -m app.seed                       # admin user + default settings
uvicorn app.main:app --port 8000
```

**Linux / macOS**
```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
export DATABASE_URL=sqlite:///./data/trading.db
alembic upgrade head
python -m app.seed
uvicorn app.main:app --port 8000
```

Frontend, in a second terminal:

```bash
cd frontend
npm install
npm run dev            # http://localhost:5173 - proxies /api and /ws to VITE_API_TARGET (default http://localhost:8000)
```

To point the dev proxy at a backend on a different port:

**Windows PowerShell**
```powershell
$env:VITE_API_TARGET = "http://localhost:8010"; npm run dev
```

**Linux / macOS**
```bash
VITE_API_TARGET=http://localhost:8010 npm run dev
```

**Seed data for UI development:** `python -m app.seed --demo` inserts 40 clearly-labelled demo trades into a separate `DEMO` ledger. They never mix with PAPER or TESTNET results. View them with `GET /api/trading/history?mode=DEMO`.

## Binance Spot Testnet

1. Go to <https://testnet.binance.vision/> and sign in with GitHub.
2. Choose **Generate HMAC_SHA256 Key** and copy the API key and secret. Testnet keys are separate from production keys; production keys will not work there and must never be used.
3. Add them to `.env`:
   ```ini
   BINANCE_TESTNET_API_KEY=...
   BINANCE_TESTNET_API_SECRET=...
   ```
4. Restart the backend or worker, stop the engine, and switch the mode to **TESTNET** in System Settings.

How testnet mode behaves:

* Not every top-10 production pair is listed on the testnet. Unlisted pairs are marked **unavailable** and are not traded in TESTNET mode; no other symbol is substituted. They remain tradable in PAPER mode.
* Before every entry, the worker checks your real testnet USDT balance.
* After every order, the worker reconciles order status and fills with the testnet. It never falls back to another endpoint.
* The system ledger starts from `INITIAL_BALANCE_USDT`. Testnet accounts hold large demo balances, but this ledger only tracks what the agent itself trades.

## Configuration

**Environment (`.env`):** see [.env.example](.env.example). These values are validated at startup, and the app fails closed if any check fails:

* endpoint host allowlists;
* the mode is `PAPER` or `TESTNET` only;
* secret lengths;
* testnet keys are present whenever `EXECUTION_MODE=TESTNET`.

**Runtime settings** are stored in the database and edited from the dashboard (Risk Management and System Settings pages) or through `PUT /api/settings` and `PUT /api/settings/risk`. Every value is validated; for example, per-position allocation can't exceed total allocation, and strategy weights must sum to 100.

Default risk parameters, which are conservative test values and not a guarantee of safety:

| Setting | Default |
|---|---|
| Initial virtual balance | 10,000 USDT |
| Risk per trade | 0.5 % of equity |
| Max open positions | 3 |
| Max daily loss | 2 % *(hard halt)* |
| Max drawdown | 10 % *(hard halt)* |
| Max per position / total | 20 % / 60 % |
| Min reward:risk (after fees and slippage) | 1.5 |
| Stop / target | 2 × ATR / 2R |
| Consecutive-loss cooldown | 3 losses → 60 min |

## Using the dashboard

| Page | What it shows |
|---|---|
| **Dashboard** | Equity, balance, daily/realised/unrealised P&L, win rate, positions, mode, latest AI decisions, and Start/Pause/Stop controls. |
| **Live Market** | The top 10 with live prices, 24h change, volume, RSI, MACD, signal, score and status. Searchable and sortable. |
| **Trading Terminal** | Pair and timeframe selector, live candlestick chart with EMA and Bollinger overlays, RSI pane, buy/sell markers, and entry/SL/TP lines. |
| **AI Analysis** | Trend, market condition, all indicator values, entry and exit checklists, score, and the exact reason. Explanations come only from evaluated conditions. |
| **Open Positions** | Entry, current price, quantity, invested amount, SL/TP, unrealised P&L, duration, and manual close. |
| **Trading History** | Round-trip trades with fees, net P&L, exit reason and mode, plus every order with fills, fees, slippage and rejections. |
| **Analytics** | Equity curve, drawdown, daily and monthly P&L, volume, profit factor, and performance by coin and by strategy. |
| **Backtesting** | Run historical simulations and view metrics, the equity curve and trades. Always labelled hypothetical. |
| **Risk Management** | Limit usage gauges, editable limits, the risk event log, and hard-halt reset. |
| **System Settings** | Mode (PAPER/TESTNET only), auto-trading, strategy, market and execution settings, and connection health. |
| **Activity Logs** | AI decisions, order requests, executions, rejections, risk warnings, API and WebSocket events, and start/stop events. |

Engine states:

| State | Behaviour |
|---|---|
| **RUNNING** | Fully automated. |
| **PAUSED** | No new entries; strategy exits and SL/TP stay active. |
| **STOPPED** | No automated decisions. **SL/TP protection of open positions stays active** as a safety measure. |

## API

All endpoints except `POST /api/auth/login` and `GET /api/health` require `Authorization: Bearer <token>`. WebSockets require `?token=<token>`. Interactive documentation is at `/docs`.

| Group | Endpoints |
|---|---|
| Auth | `POST /api/auth/login` · `GET /api/auth/me` |
| Dashboard | `GET /api/dashboard/summary` |
| Market | `GET /api/market/top-coins` · `/ticker/{symbol}` · `/candles/{symbol}?timeframe=&limit=` · `/orderbook/{symbol}` |
| AI | `GET /api/ai/signals` · `/signals/{symbol}` · `/analysis/{symbol}` · `POST /api/ai/ml/train/{symbol}` |
| Trading | `GET /api/trading/orders` · `/orders/{id}` · `/positions` · `/history` · `POST /api/trading/close-position/{id}` |
| Portfolio | `GET /api/portfolio/summary` · `/performance` · `/snapshots` |
| Settings | `GET /api/settings` · `PUT /api/settings` · `PUT /api/settings/risk` |
| System | `GET /api/system/status` · `POST /api/system/start` · `/stop` · `/pause` · `/resume` · `/risk/reset` · `GET /api/system/logs` |
| Risk | `GET /api/risk/status` · `GET /api/risk/events` |
| Backtest | `POST /api/backtest/run` · `GET /api/backtest/results` · `/results/{id}` |
| WebSocket | `WS /ws/market` · `/ws/dashboard` · `/ws/trading` |

Additional conventions:

* Monetary values are returned as **strings**, which preserves exact decimals.
* Errors use the shape `{"error": "...", "detail": ...}`.

## Testing

```bash
cd backend && pytest -q                         # unit + API + engine + migration tests (mocked exchange)
cd frontend && npm test                         # Vitest + React Testing Library
```

Opt-in integration tests against real Binance endpoints. Public data and paper trading run with no keys; the testnet test needs dedicated testnet keys.

**Windows PowerShell**
```powershell
$env:RUN_INTEGRATION = "1"
pytest -m integration
# optional testnet check (uses /api/v3/order/test only):
$env:TESTNET_KEY = "..."; $env:TESTNET_SECRET = "..."; pytest -m integration
```

**Linux / macOS**
```bash
RUN_INTEGRATION=1 pytest -m integration
# optional testnet check (uses /api/v3/order/test only):
RUN_INTEGRATION=1 TESTNET_KEY=... TESTNET_SECRET=... pytest -m integration
```

Automated tests never use production credentials or production order endpoints.

## Project layout

```
backend/
  app/
    config.py            fail-closed settings, endpoint allowlists
    models.py, db.py     SQLAlchemy models, exact Decimal/UTC column types
    exchange/            REST clients (public + testnet), WebSocket manager, symbol filters
    market/              candles + quality checks, top-N discovery, price cache
    analysis/            indicators, strategy, optional ML
    risk/engine.py       position sizing, SL/TP, limits
    execution/           paper + testnet adapters, order manager
    portfolio/service.py Decimal accounting
    backtest/engine.py   look-ahead-free backtester
    worker/              trading engine + standalone worker entry point
    api/                 REST routes + WebSocket relay
  alembic/               migrations
  tests/                 pytest suite
frontend/
  src/pages/             11 dashboard pages
  src/components/        UI primitives, charts, layout
  src/lib/               API client, live WebSocket feeds, queries
docker/                  postgres init
docker-compose.yml
```

## Known limitations

* **Tokenized stocks/ETFs.** Binance lists these (e.g. `NVDAB`, `SOXLB`) without a documented flag. They are excluded via a configurable permission-group list (`excluded_permission_groups`, default `TRD_GRP_261`). That group ID is an observed pattern, not an API contract, so review the market list periodically.
* **Paper limit orders** are matched against the best bid/ask and its displayed size. Real queue position is not modelled.
* **Testnet order tracking** uses REST reconciliation (polling every 5 s) rather than the user-data stream.
* **BNB fees.** A fee paid in a third asset such as BNB is converted to USDT at the cached price and deducted from the USDT ledger. This is conservative.
* **ML filter.** Off by default. Models are stored as local pickle files, created only by this application.
* **Single admin user.** The API supports a single admin user (bootstrapped from `.env`).
