# Funding-Rate Arbitrage Bot

Delta-neutral, cross-venue perpetual-futures funding arbitrage. The bot holds a long on the venue where funding is
cheap and a short on the venue where it is high, so price moves cancel and the funding difference is the profit.

> **Status: paper trading only.** Live and testnet order placement is not implemented, and the bot refuses to
> start in those modes. `project.md` is the authoritative specification; if this file and `project.md` disagree,
> `project.md` wins.

## The plan the bot follows

1. **Identify.** Sweep every coin Zero Delta lists (about 430, most liquid first, a couple at a time with a pause after each request) and read every possible pair with its last 7 days of funding. A sweep takes several minutes and repeats 15 minutes after the last one ends. `SCAN_ASSETS` limits it to chosen coins and `EXCLUDE_ASSETS` skips some.
2. **Select.** Rank all pairs by their 7-day average funding edge and take the best `SELECTION_COUNT` (default 3), naming the
   exchange of each leg. Two lists are kept: the best pairs in the whole market (tradable or not, so it is always clear what
   the best opportunities are) and the best pairs the bot can actually trade (both venues have an adapter, different exchanges).
3. **Simulate honestly.** Keep the full 14 days. Select on week 1 and test the original direction on unseen week 2.
   The selection-week replay is labelled **in-sample diagnostic**, not forward performance. Saved picks freeze their
   direction and parameters, then receive a week-later assessment. Hourly replays include TWAP, fees, slippage, normal
   exits and equity exits when complete prices exist; missing prices explicitly mark equity exits as untested.
4. **Verify and trade.** Only the selected tradable pairs go to the live checks (executable prices, spread rule, fees, volatility,
    Zero Delta cross-check) and, if every check passes, to a paper trade. With `SHADOW_PAPER_ENABLED=true` (default),
    a net-edge or portfolio rejection can instead become a clearly tagged **shadow research** trial in dedicated paper
    accounts. It keeps market safety and Alex's spread/volatility/earnings rules. The original rejection remains recorded.
5. **Watch.** Waiting selections are verified every minute; every entry slice rechecks volatility, funding and executable
   spread. A new sweep replaces the watch list. Existing positions keep their original direction until closed.

The ranking uses one plain metric on purpose: `project.md` forbids inventing a weighted score. Steadiness, sign flips and volume
are shown next to each pick. Each shortlist defaults to one pair per coin and excludes nonpositive current edges.
Fees in the simulation are labelled assumptions, not your account's real ones. Historical tests still use today's volume
and venue universe; they do not reconstruct historical books, venue basis or the 1-minute entry filter. Forward paper
trials use actual current venue data and the same execution/risk engine as gated paper trades.

## What runs today

| Layer | What it does | Where |
|---|---|---|
| Discover | Zero Delta Premium: per-venue hourly funding history (7 complete days), volume > $5M, pair analytics | `app/providers/zerodelta`, `app/strategy/pairs.py` |
| Verify | Direct venue APIs: funding, order book VWAP, spread rule, fees, slippage, depth, volatility filter, Zero Delta cross-check | `app/exchanges`, `app/strategy/verification.py` |
| Execute | Paired 6 h TWAP, at most $100 per leg per slice, both legs concurrent, hedge repair, idempotent order ids | `app/execution` |
| Protect | Equity, margin, liquidation, delta, connectivity; 30 % controlled exit, 10 % emergency exit, kill switch | `app/risk` |
| Account | Funding, fees, slippage estimate and P&L reported separately; full audit trail | `app/accounting`, `app/persistence` |
| Operate | Health, state, authenticated kill switch, Telegram alerts, restart recovery | `app/api`, `app/workers`, `app/notifications` |

Paper mode uses **real market data** and a **simulated account** (fills walk the real order book, with fees,
partial fills and funding cash flows). No order ever reaches an exchange.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env                                         # then edit .env (never commit it)
python -m pytest                                             # the full suite needs no network or credentials
python -m app.tools.paper_demo                               # watch a whole simulated trade in seconds
python -m app.tools.paper_demo --scenario crash              # ...or a margin shock, or --scenario partial
python -m app.tools.paper_demo --scenario stoploss           # true 4x account, 30% rule, full 3-hour exit
python -m app.tools.public_check Bitget OKX Gate "MEXC USDT" "BloFin USDT"  # public read-only connector check
python -m app.tools.zerodelta_check BTC --offline            # sanity check from saved fixtures
python -m app.tools.zerodelta_opportunities BTC --offline
uvicorn app.main:app --host 127.0.0.1 --port 8000            # paper bot + API
```

Docker (bot + PostgreSQL, API bound to localhost):

```bash
cp .env.example .env    # set POSTGRES_PASSWORD, ADMIN_API_TOKEN, ZERO_DELTA_SESSION_COOKIE, ZERO_DELTA_USER_ID
docker compose up -d --build
curl http://127.0.0.1:8000/health
```

### Getting the Zero Delta credentials

Log in at zero-delta.xyz in your browser. Open DevTools, then Application, Cookies, and copy the value of the
`session` cookie into `ZERO_DELTA_SESSION_COOKIE`. Your numeric `user_id` appears in the response of the request
that outputs `store-auth-session` (Network tab, search `user_id`). The cookie expires: when the
`zero_delta_auth_failure` alert fires, log in again and update it. While it is invalid the bot finds no new trades
but keeps managing existing ones.

## What to expect

Funding arbitrage is a fee game. With taker fees on both legs at entry and exit, the funding spread has to be large and
persistent before a trade pays. At the time of the first live check the best executable pairs earned about 9 % APR before
costs, so the bot correctly found **no trade**, and all three assets were in the top decile of recent volatility. Expect the
bot to sit idle most of the time. That is the intended behaviour, not a fault.

Public-data adapters cover Binance, Bybit, Hyperliquid (plus Trade.xyz), OKX, Gate, Bitget, MEXC and BloFin. All execution
is simulated. Unsupported venues are shown with an availability reason; they cannot fabricate a forward fill.

Paper accounts default to $1,000 per side and $250 equity per side (target leverage 4x), with the same base quantity
on both legs rounded to a common lot. The $100 limit is per entry slice, not the total position. These remain configurable
paper assumptions. Research capacity is bounded to `SELECTION_COUNT` simultaneous shadow trials; further picks wait.
The independent margin/liquidation guards use maintenance-margin risk and normally sit behind the
30% controlled / below-10% emergency equity exits. Shadow trials do not consume the gated portfolio's allocation.

Stocks are classified using venue metadata and checked within **±3 calendar days** of earnings. Nasdaq's public
calendar covers US listings; `EARNINGS_CALENDAR_FILE` accepts `SYMBOL,YYYY-MM-DD` rows for other listings. Missing
calendar data blocks stock entry. Crypto has no earnings condition.

Saved-pick windows overlap between sweeps; their estimates are repeated observations, not independent portfolio returns.

## Safety model

- `RUN_MODE=paper`, `LIVE_TRADING_ENABLED=false` by default. API keys never enable live trading.
- Live mode needs the flag, the confirmation phrase **and** every `REQUIRED_BEFORE_LIVE` value set explicitly, and even
  then it refuses today because real order placement is missing (phase 8).
- Locked values (4x leverage, 6 h TWAP, $100 child, 45th percentile, 30 %/10 % exits, $5M, 7 days) cannot be loosened
  from the environment.
- **Same underlying.** Zero Delta groups markets by ticker, and one ticker can name two assets (live: "BB" is BounceBit
  on Binance and BlackBerry stock on Trade.xyz). Both legs' prices must agree within `MAX_LEG_PRICE_GAP_PCT` (5 %) at
  verification and before every slice, so an "equal quantity" hedge is always one asset against itself.
- **Zero Delta cross-check.** Zero Delta shows the last settled funding rate while venues publish a running estimate
  for the next settlement; a rate agrees when it matches either one. A wrong symbol, interval or sign matches neither.
- **Open-trade profit is never a false number.** If a leg's venue misses a cycle, the trade is valued with that leg's
  last price (up to 5 minutes old, labelled with its age); after that the dashboard shows "price unavailable" instead
  of a total that would leave the leg's whole position out.
- **A locked database cannot stop a sweep.** File SQLite runs in WAL mode (another program reading the file never
  blocks the bot), waits up to 30 s for another writer, and every scanner write is buffered during an outage. Use
  PostgreSQL (docker-compose) for a long-running deployment.
- **Health means healthy.** The engine is green only when its loop completed and no trade is DEGRADED, RECONCILING,
  in ERROR or waiting for a human; health details and alerts show the first line of the error.
- **Stale data.** A book older than `MAX_MARKET_DATA_AGE_MS` is fetched once more before it counts as stale (MEXC
  sometimes serves a cached snapshot); data that stays old still pauses slices at once, with one alert per episode that lasts 30 s or more
  (MEXC's single-cycle blips no longer each raise a warning). Keep the
  PC clock synchronised: a clock that runs ahead makes every venue's data look older than it is.
- **An entry always ends.** The per-slice gates can postpone slices for hours, so an entry still unfinished after
  `MAX_ENTRY_HOURS` (12 h, twice the locked TWAP) stops and keeps what it holds, and an entry whose funding edge has
  decayed (the normal-exit edge rule) stops at once. From there the normal exit rules decide whether the reduced
  position stays open; an entry that never filled is simply closed. (Live: AERO once sat 44 h at 6 of 10 slices with
  zero edge, holding a research slot.)
- **One slow venue cannot freeze the others.** Open trades are stepped together, venue connections stay open for 60 s
  (the default 5 s made every cycle reconnect) and a connect that has not completed in 3 s is retried.
- **Crypto is never held to the earnings calendar.** A venue's own contract label (Binance, Gate, Bitget) decides the
  asset class; venues that list crypto next to stocks (Ondo Perps, Lighter on Robinhood) only decide for unlabelled coins.
- In paper mode, blank `REQUIRED_BEFORE_LIVE` values use **labelled paper defaults** (`app/core/trading_config.py`).
  The startup alert and `/state` list exactly which parameters are defaults, so a default is never mistaken for a decision.
- Zero Delta never triggers an order by itself, and a Zero Delta outage never stops risk management or an exit.
- When information is missing, stale or ambiguous the bot chooses **no trade**.
- The engine keeps trade state in memory and buffers database writes during an outage, so an exit can complete with the
  database down; buffered writes replay in order when it returns.
- Secrets are read from the environment only, never logged, and scrubbed from alerts.

## Dashboard

With the bot running, open **http://127.0.0.1:8000/dashboard**. It refreshes every 5 seconds (**Pause updates**
stops that) and has one focused view per job. Every view has its own address, so links and the Back button work:

| View | Address | What it shows |
|---|---|---|
| Overview | `#/overview` | status, a one-line summary, 4 headline numbers, the profit curve, open trades, the selected pairs, alerts that need attention |
| Trades | `#/trades` | every trade (filter All / Open / Closed) and where each trade's profit comes from |
| One trade | `#/trades/<id>` | profit breakdown and entry progress, then tabs: **Summary** (timing, prices, funding payments), **Charts** (profit, funding, each account against the 30 % / 10 % exits, long vs short exposure), **Why it opened** (entry checks, 14-day funding), **Activity** (state changes, grouped risk events, orders) |
| The plan | `#/plan` | Alex's four steps with numbers, the selected pairs and the best in the whole market (with exchanges), the walk-forward check, the track record |
| Scanner | `#/scanner` | the latest sweep as a funnel, why nothing was traded, alerts, every coin with pairs (searchable) |
| Market | `#/market` | the strongest funding spreads of the latest sweep (search, "tradable only") |
| Decisions | `#/decisions` | every live check with its measured values (filter passed / waiting / turned down) |
| Settings | `#/settings` | the rules from the specification, the paper-mode choices, the health of each part |

Open trades show profit "so far" (valued at current prices, before closing costs). If an exchange misses an update,
the value uses its last price (labelled with its age) and then shows "price unavailable" instead of a wrong number.
The page is read-only, has a dark and a light theme, works from 390 px phones upwards and loads no external files.
The same data is available as JSON under `/api/*`. Design rules for adding to the page are in
`design-system/arbitrage-bot-console/MASTER.md`.

## Operating it

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /dashboard` | none | the live dashboard page |
| `GET /api/performance`, `/api/overview`, `/api/decisions`, `/api/opportunities`, `/api/alerts`, `/api/trades`, `/api/trades/{id}` | none | read-only JSON behind the dashboard |
| `GET /api/selection`, `/api/selection/track-record` | none | current picks, walk-forward estimates, forward trial status and week-later scores |
| `GET /health` | none | component health (database, Zero Delta, scanner, engine) |
| `GET /state` | none | kill-switch state, active trades, last scan results, which parameters use paper defaults |
| `POST /admin/pause-new-entries` | `X-Admin-Token` | stop new positions, keep managing open ones |
| `POST /admin/resume-entries` | `X-Admin-Token` | resume (refused while a flatten is latched) |
| `POST /admin/emergency-flatten` | `X-Admin-Token` | cancel entry work and close every trade with the emergency exit |
| `POST /admin/clear-emergency-flatten` | `X-Admin-Token` | release the latch (entries stay paused until resumed) |

Bodies are `{"actor": "...", "reason": "..."}`. The admin API is disabled unless `ADMIN_API_TOKEN` is set. Put a TLS
reverse proxy in front of the API, or use an SSH tunnel; do not expose it directly.

## Layout

```
app/core         settings, live gate, clock, health
app/domain       market data models, trade state machine
app/providers    Zero Delta provider (discovery)
app/exchanges    direct venue adapters (public market data)
app/strategy     pair analytics, executable price math, verification and entry decision
app/market_data  volatility filter
app/execution    paper venue, TWAP, delta manager, pair executor, trade engine
app/risk         risk engine, exit policy, kill switch
app/accounting   P&L
app/persistence  SQLAlchemy models and repository
app/workers      scanner and run loops
app/api          FastAPI app
tests/           unit, provider, exchange, execution, risk, chaos
```

## What is not done

- **Real order placement** (phase 8): account and order calls raise `ExchangeNotSupported`. Venue fee schedules, funding
  payment reconciliation from the venue, and contract-multiplier verification need account access.
- **WebSockets** (section 41): market data is polled over REST.
- **Redis**: not used; single-process locking uses database leases.
- **Alembic migrations**: tables are created with `create_all`. Add migrations before changing the schema in production.
- **HIP-3 execution**: Zero Delta gives no builder or market identifier, so HIP-3 markets are classified but the exact
  market must be resolved from Hyperliquid before any order.
- **Unverified assumptions** carried from Zero Delta: the "24hr Volume (M)" column is treated as average 24h volume,
  chart timestamps as UTC, and a positive APR as "longs pay shorts".

## Before any live use

Everything in `project.md` sections 71 and 58 must be true: the parameters set explicitly, testnet mechanics verified on every
venue, and the chaos and recovery tests passing against real venue behaviour. Paper results say nothing about live profitability.
