# Funding-Rate Arbitrage Bot

Delta-neutral, cross-venue perpetual-futures funding arbitrage. The bot holds a long on the venue where funding is
cheap and a short on the venue where it is high, so price moves cancel and the funding difference is the profit.

> **Status: paper trading by default; live trading is built but off.** Live order placement (Binance, Bybit, MEXC,
> BloFin, Bitget) only starts behind the explicit gate in [Going live](#going-live) and has not yet been exercised with
> real API keys. Testnet is not implemented, and the bot refuses to start in that mode. `project.md` is the
> authoritative specification; if this file and `project.md` disagree, `project.md` wins.

## The plan the bot follows

1. **Identify.** Sweep every coin Zero Delta lists (about 430, most liquid first, a couple at a time with a pause after each request) and read every possible pair with its last 7 days of funding. A sweep takes several minutes and repeats 15 minutes after the last one ends. `SCAN_ASSETS` limits it to chosen coins and `EXCLUDE_ASSETS` skips some. `EXCLUDE_VENUES` keeps whole exchanges out (for example `BloFin`, which refuses some regions).
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

### Database-first Zero Delta discovery

`ZERO_DELTA_SOURCE=postgres` uses the owner-provided `marts.fct_coin_exchange_summary` and
`public.perp_market_data`. The existing Dash source remains available with `ZERO_DELTA_SOURCE=dash`.

- Summary rows are **per coin/venue**: screening ranks their **difference**, not the highest individual APR.
- Discovery runs at **:05 UTC each hour** (configurable minute), with at most two bounded retry cycles for failures.
  Waiting selections keep their one-minute watch; existing trades use the independent direct-venue engine.
- The research ceiling defaults to 30 pairs, with capacity reserved for executable venues. Only their markets and
  markets required by due frozen selections receive raw-history queries. Final rankings are labelled **best among
  the validated shortlist**, not an exhaustive raw-history ranking of the market.
- Every raw query includes the indexed base-coin expression, exact exchange/symbol and time bounds. The default
  server statement budget is 12 s, below the owner's 15 s limit; truncated history is rejected. Cached data re-fetches
  its last hour to incorporate corrections. Different raw symbols are never silently combined.
- Hourly observations use the latest real snapshot in each UTC hour, retaining its original source time. Missing hours
  and conflicting source timestamps remain invalid. Rates use each observation's own interval. Funding kind is retained:
  a historical estimate is not represented as a settled payment, and replay cashflows remain approximations.
- Picks show the largest-hour/top-three contribution to positive edge and the mean without the largest observation.
  These are diagnostics, not a new weighted score or an automatic spike-rejection policy.
- Summary batches and selected evidence are frozen in the **local** database. Today's shortlist is not retrospectively
  used as last week's universe; frozen picks receive their original-direction week-later assessment, including coins
  dropped from current summaries. Batch/pick audit history is retained; monitor local database storage growth.
- The supplied schema has no refresh watermark. The console explicitly labels summary refresh as **unverified** and
  validates the common cutoff of selected raw markets separately; it does not infer global refresh completion from one coin.

**Setup:**

```bash
pip install -e ".[postgres,dev]"
python -m app.tools.zerodelta_db_check --asset BTC --exchange binance_usdt --days 14
python -m app.tools.migrate_zerodelta
```

The check uses `ZERO_DELTA_DATABASE_URL` from the environment; `--prompt-url` accepts it privately in an interactive
terminal. It does not instantiate the trading runtime, write application tables, or access the website. It reports
schema, indexes, bounded raw samples, missing semantics, and (when configured) normalized coverage, query latency and
`EXPLAIN` output. Sampling alone does not establish units: confirm the database definitions with the owner.

Set `ZERO_DELTA_DATABASE_URL` and the six `ZERO_DELTA_DB_*` contract fields listed in `.env.example`: volume unit,
funding unit, interval unit, epoch unit, funding kind and UTC interpretation. Leave unknown facts unset: discovery reports
the missing configuration and admits no new trades. Then set `ZERO_DELTA_SOURCE=postgres` and restart in paper mode.
Browser cookie expiry has no bearing on PostgreSQL credentials or grants. Keep the bot's `DATABASE_URL` separate.

Migration 001 adds only `zerodelta_summary_batches`, using the same SQLAlchemy metadata on SQLite and PostgreSQL;
it is idempotent, and normal startup also creates the additive table. No upstream schema changes are made.

**Live preflight, 2026-10-02:** the supplied access worked and returned 7,118 summary rows. Targeted BTC reads on
Binance USDT, Bybit USDT and Hyperliquid confirmed millisecond epochs matching UTC datetimes, hour-based intervals,
and fraction-valued raw funding (different from the website's percent presentation). Direct venue checks identified
running estimates; `nextfundingtime` was null, so verification uses actual venue funding clocks/settlement history.
Observations arrived near **HH:59**: the common analysis boundary at the following :05 is the preceding completed
hour, while entry freshness uses the original source timestamp. Binance and Hyperliquid retained 337 hourly points;
Bybit had one missing hour and was correctly ineligible under the strict default. Sample history reads took about
2.3–4.4 s including connections; `EXPLAIN` confirmed the base/exchange/datetime index after materializing the bounded
market window. These are representative observations, not a guarantee for every market or future query latency.

| Layer | What it does | Where |
|---|---|---|
| Discover | Zero Delta Premium: per-venue hourly funding history (7 complete days), volume > $5M, pair analytics | `app/providers/zerodelta`, `app/strategy/pairs.py` |
| Verify | Direct venue APIs: funding, order book VWAP, spread rule, fees, slippage, depth, volatility filter, Zero Delta cross-check | `app/exchanges`, `app/strategy/verification.py` |
| Execute | Paired 6 h TWAP, at most $100 per leg per slice, both legs concurrent, hedge repair, idempotent order ids | `app/execution` |
| Protect | Equity, margin, liquidation, delta, connectivity; 30 % controlled exit, 10 % emergency exit, kill switch | `app/risk` |
| Account | Funding, fees, slippage estimate and P&L reported separately; full audit trail | `app/accounting`, `app/persistence` |
| Operate | Health, state, authenticated kill switch, Telegram alerts, restart recovery | `app/api`, `app/workers`, `app/notifications` |
| Trade live | Signed order and account clients for Binance, Bybit, MEXC, BloFin and Bitget (USDT-margined perps, one-way mode, market orders), read-only readiness check | `app/exchanges/private`, `app/execution/live.py`, `app/tools/live_check.py` |

Paper mode uses **real market data** and a **simulated account** (fills walk the real order book, with fees,
partial fills and funding cash flows). No order ever reaches an exchange. Live mode runs the same engine against real
accounts: see [Going live](#going-live).

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
python -m app.tools.settle_paper_trade arb0004 --reason venue_excluded  # bot stopped: close a stranded paper trade at
                                                 # its last joint prices (dry run; add --apply to write it)
uvicorn app.main:app --host 127.0.0.1 --port 8000            # paper bot + API
```

Only one bot runs per project folder: a second `uvicorn` start (on any port) is refused at once with the process id of
the first, before it reads the database or calls a venue, because two bots would both manage the same trades. The lock
is `data/bot.lock`; the system frees it when the first bot stops, however it stops, so it never needs deleting.

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
- Live mode needs the flag, the confirmation phrase, every `REQUIRED_BEFORE_LIVE` value set explicitly **and** API
  credentials for at least two venues. Each venue must also pass its read-only readiness check before it can open a
  trade; with fewer than two ready venues new trades are blocked (open trades are always managed and exited).
- **Live orders are never sent twice.** A timed-out order is looked up by its client order id before anything else;
  after a restart every order is looked up on the venue before it is sent, so an order that reached the venue just
  before a crash is adopted, not repeated.
- **The venue is the truth.** A live leg's equity comes from its own fills, fees and funding (the 30 % / 10 % rules mean
  exactly what they mean in paper mode), and is cross-checked against the venue's position after every cycle: if the two
  ever disagree, the trade goes to DEGRADED until they agree again.
- **One trade per venue symbol**, and none on a symbol the account already holds or has open orders on.
- Locked values (4x leverage, 6 h TWAP, $100 child, 60th percentile, 8 h of 7-day median funding for spread, 30 %/10 % exits, $5M, 7 days) cannot be loosened
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

Configure and operate the local controls without copying a token into a command or printing it:

```bash
python -m app.tools.admin setup   # generate a private token in the existing ignored .env; preserves an existing token
# Restart the bot once to load the token.
python -m app.tools.admin check   # authenticates with an invalid body, so no trading action is executed
python -m app.tools.admin pause --reason "maintenance"
python -m app.tools.admin resume --reason "maintenance complete"
# Explicit emergency action only when wanted:
python -m app.tools.admin flatten --reason "operator emergency exit"
python -m app.tools.admin clear-flatten --reason "incident resolved"
```

The CLI uses the localhost API. Clearing a flatten leaves entries paused; resume is a separate action.

### Logs

The console shows only what needs attention: warnings, errors, and a few milestone alerts (startup, recovery, trades
opened and closed, pair selections). Everything else is written to rotating files in `logs/`:

| File | Contents |
|---|---|
| `logs/bot.log` | everything the bot decides at `LOG_LEVEL` (default INFO): candidates rejected, alerts, warnings, full tracebacks |
| `logs/http.log` | every HTTP request the bot makes and every call to its own API. Signatures, API keys and the Telegram token are masked |

Each file rotates at `LOG_MAX_MB` (default 20) and keeps `LOG_BACKUP_COUNT` older files (default 10; `http.log` keeps 3,
it grows by roughly 60 MB a day with open trades). Set `CONSOLE_LOG_LEVEL=INFO` to see the rest of `bot.log` on the
console; the HTTP lines never go to the console. `LOG_DIR` moves the folder (a relative path is relative to the project).
Follow the log live with `Get-Content logs\bot.log -Wait -Tail 30` in PowerShell. The database keeps a durable copy of
every alert, risk event and decision (`/api/alerts`).

### Reliability diagnostics and paper observation

`/api/overview.runtime_metrics` reports per-venue request/attempt/retry/recovery counts, HTTP statuses, timeout types,
and loop durations. PostgreSQL queries log their operation, market, elapsed time and scrubbed outcome; the existing
12-second statement budget remains in force. An individual market timeout produces a partial scan, excludes that
market, and leaves validated candidates usable. Health stays red for the partial scan and discovery makes at most two
one-minute retry cycles. Connection/access failures and widespread history timeouts stop new discovery.

`/state.last_scans` and `/api/overview.discovery.screening[].validation` expose missing-hour counts, stale boundaries,
ambiguous symbols and query failures. PostgreSQL funding checks include both rates and source/reference times. Direct
estimates captured within one minute of the upstream observation can verify its original prediction; a changed later
estimate waits, and a final settled rate never substitutes for a historical prediction. The bounded direct-estimate
buffer is in memory, so it warms up again after a restart. Missing upstream history and the absent summary refresh
watermark still require evidence from the data provider.

For an exact, credential-free gap report suitable for the data provider:

```bash
python -m app.tools.zerodelta_db_check --asset BTC --exchange bybit_usdt --days 14 --output logs/bybit-history-check.json
```

The report lists absent UTC hours in both the seven-day selection and retained-history windows. Incremental history
reads restart at the earliest cached gap, so an actual upstream backfill becomes visible without restarting the bot;
an absent observation still fails continuity. Queries remain exact-market, bounded and read-only.

Decisions record assumed income, round-trip costs and break-even hours. Trade details include equivalent full-size
position hours, integrating actual fills through entry waits and exits. These diagnostics leave the labelled 72-hour
full-size income assumption visible. `/api/performance.groups` separates shadow research from profit-gated results.

Audit saved fills, funding, fees and P&L independently, or collect a day of read-only observation:

```bash
python -m app.tools.observe --audit
python -m app.tools.observe --hours 24 --interval-seconds 60 --output logs/paper-observation.jsonl --audit
```

The observer calls only GET endpoints, appends one JSON record per sample, and never creates a second trading runtime.
With `--audit`, it audits at startup, hourly (configurable with `--audit-interval-seconds`) and at completion, including
trades opened during observation. It records market-validation failures alongside each sample and reports the P&L,
funding and fee change during the observation window separately from lifetime totals. It reports unhealthy samples,
API failures and ledger discrepancies in its final record and exit status. Compare the
request counts, retry rate, cycle times, partial scans and trade states across the observation period. An incomplete
open-trade valuation is labelled `partial`, rather than presented as an audited profit. Paper P&L uses simulated cashflows
and labelled fee assumptions; ledger reconciliation does not establish that historical predictions were settled income.

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

- **Live orders on OKX, Gate and Hyperliquid**: paper only (no signed client). Live trading covers Binance, Bybit, MEXC,
  BloFin and Bitget, USDT-margined perpetuals only.
- **WebSockets** (section 41): market data is polled over REST.
- **Redis**: not used; single-process locking uses database leases.
- **Alembic migrations**: tables are created with `create_all`. Add migrations before changing the schema in production.
- **HIP-3 execution**: Zero Delta gives no builder or market identifier, so HIP-3 markets are classified but the exact
  market must be resolved from Hyperliquid before any order.
- **Unverified assumptions** carried from Zero Delta: the "24hr Volume (M)" column is treated as average 24h volume,
  chart timestamps as UTC, and a positive APR as "longs pay shorts".

## Going live

Live mode sends **real orders with real money**. Paper results say nothing about live profitability: so far no pair has
passed the profit check (`MIN_EXPECTED_NET_EDGE_BPS`), and live mode only opens trades that pass every check, so a live
bot may wait a long time before its first trade. That is the intended behaviour.

**1. Prepare each exchange account** (only the ones you will use; each trade needs two):

| Exchange | Account settings | API key |
|---|---|---|
| Binance | USDⓈ-M futures: One-way mode, Single-Asset mode | Enable Futures, no withdrawals, restrict to your IP |
| Bybit | Unified Trading Account; margin mode Isolated or Cross (not Portfolio) | Contract / Derivatives trade, read-write, no withdrawals, your IP |
| MEXC | KYC done; futures in one-way mode | "Order Placing" + view permissions, your IP |
| BloFin | Futures: one-way (net) mode, isolated margin | TRADE permission, a passphrase, your IP |
| Bitget | A **Classic** account (or a Classic-mode sub-account; Unified accounts cannot use this API); USDT-M one-way mode | Futures read-write, a passphrase, your IP |

Fund each futures account with USDT: every leg needs its margin (`TARGET_NOTIONAL_PER_LEG_USD / TARGET_LEVERAGE`) plus
20 % headroom, so at $100 per side and 4x at least $30 free per venue (more is better). Do not trade the bot's symbols by
hand while it holds them, and switch fee payment with the exchange token off where you can (such fees are valued at the
token's price, which is less exact).

**2. Put the keys in `.env`** (`BINANCE_API_KEY`, `BINANCE_API_SECRET`, ... see `.env.example`).

**3. Check, without trading:** `python -m app.tools.live_check`. It reports every venue as READY or what to fix, and what
the live gate still needs. Repeat until it says `RESULT: ready`.

**4. Set the live configuration in `.env`.** A cautious first run at $100 per side, one trade at a time:

```
RUN_MODE=live
LIVE_TRADING_ENABLED=true
LIVE_TRADING_CONFIRMATION=I_UNDERSTAND_LIVE_ORDERS_USE_REAL_FUNDS
TARGET_NOTIONAL_PER_LEG_USD=100
TARGET_LEVERAGE=4
MAX_CONCURRENT_TRADES=1
MAX_TOTAL_GROSS_NOTIONAL_USD=200
MAX_NOTIONAL_PER_VENUE_USD=100
MAX_NOTIONAL_PER_ASSET_USD=200
RV_WINDOW_MINUTES=60
BASIS_VOL_WINDOW_MINUTES=60
VOL_PERCENTILE_LOOKBACK_DAYS=1
VOL_MIN_OBSERVATIONS=500
MIN_EXPECTED_NET_EDGE_BPS=10
MAX_ALLOWED_SLIPPAGE_BPS=5
MIN_REQUIRED_BOOK_DEPTH_MULTIPLIER=5
MAX_MARKET_DATA_AGE_MS=3000
MAX_DELTA_IMBALANCE_USD=50
MAX_DELTA_IMBALANCE_RATIO=0.05
DELTA_HEDGE_TIMEOUT_MS=10000
MIN_LIQUIDATION_DISTANCE_PCT=1
MAX_MARGIN_UTILIZATION_PCT=80
NORMAL_EXIT_POLICY=edge_decay
ADMIN_API_TOKEN=<a long random string, for the kill switch>
```

These are the paper defaults made explicit, with the size cut to $100 per side. At $100 a side some coins cannot be
traded (BTC's minimum order on several venues is worth more than one $100 slice); the bot then simply skips them.

**5. Start the bot** as usual. The startup alert and `/health` (`live:<venue>` entries) show which venues are ready; the
dashboard header turns red if one is not. The kill switch (`/admin/pause-new-entries`, `/admin/emergency-flatten` with
the `X-Admin-Token` header) works the same as in paper mode.

**How the clients were verified.** Each signed client follows the venue's official documentation; Binance's signing
matches its published test vector, and the signing and order formats of all five were cross-checked against the ccxt
library, which trades these APIs live (it also settles the details MEXC and BloFin leave undocumented: MEXC market orders
carry no price and use side codes 1/3 to open and 2/4 to close; BloFin positions are in contracts). A signature can only be
proven with a real key: the first `live_check` run does that without trading. The live leg also compares the venue's
position with its own fills after every order, so any surprise stops the trade (DEGRADED) instead of trading on wrong
numbers. Funding is read from the venue every 10 minutes. Research ("shadow") trials stay paper-only.
