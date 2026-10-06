# Funding Rate Arbitrage Bot — project.md

> **Status:** Authoritative build specification  
> **Purpose:** Single source of truth for Codex / AI coding agents and human developers  
> **Strategy:** Cross-venue, delta-neutral perpetual-futures funding-rate arbitrage  
> **Opportunity source:** Zero Delta Premium  
> **Execution source of truth:** Direct exchange / venue APIs  
> **Default runtime mode:** Paper trading  
> **Live trading:** Disabled by default

---

# 0. AI AGENT CONTRACT — READ THIS FIRST

This file is the **authoritative specification** for the project.

Any AI coding agent working on this repository MUST read this entire file before writing or modifying code.

If implementation behavior conflicts with this file, the implementation is wrong unless this file has been explicitly updated by the user.

## 0.1 Do not hallucinate

The agent MUST NOT invent:

- Zero Delta endpoints, callback payloads, response fields, or authentication behavior
- exchange API endpoints or rate limits
- symbol mappings or market IDs
- funding intervals
- trading fees
- tick sizes, quantity steps, minimum order sizes, or minimum notionals
- total position size
- delta tolerance
- volatility lookback windows
- slippage or liquidity thresholds
- normal exit rules
- portfolio limits
- liquidation thresholds
- undocumented provider behavior

If a required fact is unknown:

1. inspect the repository and existing captured data;
2. verify from official provider/exchange documentation where appropriate;
3. make user decisions configurable;
4. mark unresolved live requirements as `REQUIRED_BEFORE_LIVE`;
5. never fabricate a value.

## 0.2 Locked requirements

Anything marked **LOCKED REQUIREMENT** must not be changed unless the user explicitly changes it.

## 0.3 Separation of responsibilities

The bot has four major responsibilities:

1. **DISCOVER** — Zero Delta Premium finds historically attractive opportunities.
2. **VERIFY** — Direct venue APIs verify current funding, spread, liquidity, fees, margin and live market state.
3. **EXECUTE** — Paired TWAP opens/closes both legs while minimizing directional imbalance.
4. **PROTECT** — Risk engine monitors delta, equity, margin, liquidation risk, connectivity and failures.

No layer may silently bypass another.

## 0.4 AI / ML must not have final trading authority

AI may later rank or classify opportunities, but it must not directly place trades or bypass deterministic safety rules.

## 0.5 Live trading must default to OFF

```env
RUN_MODE=paper
LIVE_TRADING_ENABLED=false
```

Presence of API keys must never automatically enable live orders.

---

# 1. PROJECT OBJECTIVE

Build a production-oriented crypto funding-rate arbitrage bot that:

- uses **Zero Delta Premium** for opportunity discovery and historical research;
- scans **CEX, DEX and HIP-3** venues;
- uses **at least 7 complete days** of ticker/funding history;
- requires **Minimum Avg 24H Volume > $5,000,000**;
- verifies every candidate directly from the actual trading venues;
- opens an approximately delta-neutral long/short hedge using a **6-hour paired TWAP**;
- uses a **maximum child TWAP size of $100 notional per leg**;
- never exceeds **4x leverage**;
- enters only in sufficiently low-volatility conditions;
- rejects adverse entry spreads that cost more than eight hours of the positive 7-day median funding edge;
- continuously monitors actual filled exposure and account risk;
- performs a **3-hour paired controlled exit** when either side's account equity reaches or falls below 30% of baseline;
- performs an approximately **1-minute paired emergency exit** when either side's equity falls below 10% of baseline;
- continues managing existing positions if Zero Delta becomes unavailable.

The strategy is not primarily directional. The objective is to capture funding-rate differences while keeping long and short economic exposure approximately balanced.

---

# 2. MVP SCOPE

## Included

- Zero Delta Premium provider integration
- authenticated Dash callback ingestion
- CEX / DEX / HIP-3 support
- >$5M average 24-hour volume filter
- >=7-day historical funding analysis
- symbol/market normalization
- funding interval normalization
- candidate pair generation
- opportunity analytics
- direct exchange/venue verification
- executable spread checks from order books
- fees and slippage estimation
- 1-minute volatility regime filter
- paired TWAP entry
- delta monitoring
- partial-fill handling
- account equity monitoring
- margin/liquidation monitoring
- controlled and emergency exits
- paper trading
- testnet support where available
- explicit-gated live mode
- persistence and full audit trail
- Telegram alerts
- restart recovery
- chaos/failure tests

## Out of scope unless explicitly requested

- directional trading
- spot arbitrage
- triangular arbitrage
- options arbitrage
- grid trading
- martingale
- automatic withdrawals
- automatic inter-exchange transfers
- automatic borrowing
- autonomous leverage optimization
- stock/equity trading
- stock earnings-calendar logic for normal crypto pairs
- AI that directly places orders

---

# 3. EXISTING ZERO DELTA CAPTURE CONTEXT

The repository already contains experimental Zero Delta capture tooling similar to:

```text
Capture_zero_Delta/
├── all_callbacks.txt
├── analyze_captured.py
├── btc_daily_funding_history.csv
├── btc_hourly_funding_history.csv
├── btc_v1_captured.json
├── capture_coin.py
├── dash_dependencies.json
├── dash_layout.json
├── eth_hourly_funding_history.csv
├── eth_v1_captured.json
├── playwright_capture.py
├── README.md
├── sol_hourly_funding_history.csv
├── sol_v1_captured.json
├── test_v1_requirements.py
└── zero_delta_fetcher.py
```

The current capture has successfully used the authenticated Dash endpoint:

```text
POST https://www.zero-delta.xyz/_dash-update-component
```

The capture has demonstrated that:

- authenticated production data can be returned;
- current venue/funding snapshots can be decoded;
- historical chart arrays can be decoded;
- BTC, ETH and SOL hourly history has been captured;
- historical arrays may use binary/base64 encoded float data and must be parsed inside the provider layer.

The capture tooling is reverse-engineering / validation infrastructure, not the final production trading interface.

## 3.1 Security

Never commit or log:

- Zero Delta session cookies
- bearer tokens
- API keys
- JWTs
- exchange API secrets
- wallet private keys

Use environment variables or a proper secret manager.

## 3.2 Authorization boundary

Use Zero Delta only in ways authorized by the user's premium account and applicable provider terms.

Do not bypass:

- login
- premium access
- access controls
- CAPTCHA
- anti-bot protections
- rate limits

If an official API becomes available later, the provider abstraction should allow migration without changing strategy/execution logic.

---

# 4. ZERO DELTA ROLE

**LOCKED REQUIREMENT**

Zero Delta Premium is:

```text
PRIMARY OPPORTUNITY DISCOVERY PROVIDER
+
PRIMARY >=7-DAY HISTORICAL RESEARCH PROVIDER
```

Zero Delta is NOT:

```text
FINAL EXECUTION SOURCE OF TRUTH
```

Required flow:

```text
ZERO DELTA PREMIUM
        │
        ▼
ZeroDeltaProvider
        │
        ▼
7-day opportunity analysis
        │
        ▼
Candidate
        │
        ▼
DIRECT VENUE VERIFICATION
        │
        ▼
Deterministic strategy gates
        │
        ▼
Paired execution
```

A Zero Delta opportunity alone must never trigger an order.

---

# 5. ZERO DELTA PROVIDER

Refactor the current experimental capture into a reusable provider.

Recommended structure:

```text
app/
└── providers/
    └── zerodelta/
        ├── __init__.py
        ├── client.py
        ├── auth.py
        ├── callbacks.py
        ├── parser.py
        ├── schemas.py
        ├── models.py
        ├── history.py
        ├── normalizer.py
        ├── health.py
        └── exceptions.py
```

The old capture tooling may remain separately under:

```text
tools/zerodelta_capture/
```

## Required public interface

```python
class ZeroDeltaProvider:
    async def health_check(self): ...
    async def get_current_markets(self, asset: str): ...
    async def get_hourly_history(self, asset: str, days: int = 7): ...
    async def get_eligible_markets(self, asset: str): ...
    async def get_candidate_pairs(self, asset: str): ...
```

Strategy code must never depend directly on raw Dash JSON.

---

# 6. ZERO DELTA AUTHENTICATION

Current capture is based on an authenticated browser/session flow.

Never hardcode a copied cookie in source.

```env
ZERO_DELTA_ENABLED=true
ZERO_DELTA_BASE_URL=https://www.zero-delta.xyz
ZERO_DELTA_DASH_UPDATE_PATH=/_dash-update-component
ZERO_DELTA_AUTH_MODE=session_cookie
ZERO_DELTA_SESSION_COOKIE=
```

If an official API token/key becomes available, implement it behind the same provider interface.

## Auth failure behavior

If auth fails while flat:

```text
STOP NEW OPPORTUNITY GENERATION
SEND ALERT
DO NOT OPEN NEW TRADES
```

If auth fails while a live trade is open:

```text
KEEP MANAGING THE POSITION
USE DIRECT VENUE APIS
SEND ALERT
```

Zero Delta failure must never remove the ability to close an existing position.

---

# 7. VENUE UNIVERSE

**LOCKED REQUIREMENT**

```text
CEX   = ENABLED
DEX   = ENABLED
HIP-3 = ENABLED
```

Internal enum:

```text
CEX
DEX
HIP3
```

Preserve exact venue identity as well as venue category.

Fields should include, where available:

```text
venue_name
venue_type
dex_name
market_identifier
asset_identifier
display_symbol
base_asset
quote_asset
settlement_asset
```

## HIP-3

Do not collapse HIP-3 markets into a generic "Hyperliquid" record.

Preserve the exact market / DEX identifiers required to resolve the executable market.

Never execute from a display ticker alone.

---

# 8. MINIMUM VOLUME FILTER

**LOCKED REQUIREMENT**

```text
Minimum Avg 24H Volume > $5,000,000
```

```env
ZERO_DELTA_MIN_AVG_24H_VOLUME_USD=5000000
```

Eligibility:

```text
avg_24h_volume_usd > 5_000_000
```

Boundary:

```text
$4,999,999.99  -> reject
$5,000,000.00  -> reject
$5,000,000.01  -> pass
```

Use Zero Delta's actual displayed/returned metric. Do not substitute another volume definition silently.

---

# 9. HISTORICAL DATA REQUIREMENT

**LOCKED REQUIREMENT**

Primary analysis window:

```text
7 complete days
```

```env
ZERO_DELTA_PRIMARY_LOOKBACK_DAYS=7
ZERO_DELTA_MIN_LOOKBACK_DAYS=7
```

Examples:

```text
1 day           -> invalid
3 days          -> invalid
6.9 days        -> invalid
7 complete days -> valid
14 days         -> valid
30 days         -> valid
```

More than seven days may be stored as supplementary history. Primary opportunity metrics use the trailing seven-day window.

## Coverage validation

Do not use row count alone.

Store:

```text
requested_start
requested_end
first_timestamp
last_timestamp
coverage_seconds
observation_count
expected_observation_count_if_known
missing_intervals
duplicate_count
```

Require at least 168 hours of valid timestamp coverage and acceptable continuity.

Do not silently:

- forward-fill funding
- interpolate missing funding
- treat gaps as real observations

Insufficient history:

```text
DATA_INSUFFICIENT
```

No live entry.

---

# 10. ZERO DELTA DATA MODEL

Preserve every verified field that is relevant, without inventing absent fields.

## Identity

```text
provider_market_id
ticker
base_asset
quote_asset
settlement_asset
venue_name
venue_type
dex_name
market_identifier
display_symbol
```

## Funding

```text
funding_rate_raw
funding_interval
funding_interval_hours
funding_apr_if_provided
provider_timestamp
received_timestamp
```

## Liquidity

```text
avg_24h_volume_usd
```

## Historical, where available

```text
timestamp
funding_rate
funding_apr
price
basis
spread
volume
provider_score
provider_rank
```

Unknown values are nullable. Never fabricate them.

---

# 11. NORMALIZED INTERNAL MODELS

Raw provider data must be converted into stable typed models.

Example:

```python
ZeroDeltaMarketSnapshot(
    provider="zerodelta",
    provider_market_id="...",
    timestamp=...,
    received_at=...,
    asset="BTC",
    venue_name="...",
    venue_type="CEX",
    dex_name=None,
    market_identifier="...",
    display_symbol="...",
    funding_rate_raw=Decimal("..."),
    funding_interval_hours=Decimal("..."),
    normalized_funding_rate_per_hour=Decimal("..."),
    funding_apr=Decimal("...") | None,
    avg_24h_volume_usd=Decimal("...")
)
```

Historical:

```python
ZeroDeltaFundingObservation(
    timestamp=...,
    asset="BTC",
    venue_name="...",
    venue_type="DEX",
    market_identifier="...",
    funding_rate_raw=Decimal("..."),
    funding_interval_hours=Decimal("..."),
    normalized_funding_rate_per_hour=Decimal("..."),
    price=Decimal("...") | None,
    basis=Decimal("...") | None,
    volume=Decimal("...") | None
)
```

---

# 12. FUNDING NORMALIZATION

Never compare raw funding numbers across different intervals without normalization.

Internal sign convention:

```text
positive funding = LONG pays SHORT
negative funding = SHORT pays LONG
```

Screening metric:

```text
normalized_funding_rate_per_hour
=
funding_rate_for_interval
/
funding_interval_hours
```

Example:

```text
+0.08% every 8h
=
+0.01%/h
```

Normalized hourly funding is for comparison. Actual expected P&L must consider real funding timestamps and settlement schedules.

Distinguish, where available:

```text
historical settled funding
current funding
predicted funding
next funding timestamp
```

---

# 13. CANDIDATE PAIR GENERATION

Generate compatible pairs across:

```text
CEX  <-> CEX
CEX  <-> DEX
DEX  <-> DEX
CEX  <-> HIP3
DEX  <-> HIP3
HIP3 <-> HIP3
```

Only pair economically compatible instruments.

Validate:

```text
underlying
contract structure
linear vs inverse
quote/settlement semantics
contract multiplier
quantity units
collateral implications
```

Do not pair based only on similar display names.

For MVP, prioritize compatible linear perpetual contracts.

---

# 14. SEVEN-DAY ANALYTICS

For every eligible pair calculate, where data exists:

```text
observation_count
coverage_hours

current_funding_edge
7d_mean_funding_edge
7d_median_funding_edge
7d_min_funding_edge
7d_max_funding_edge
7d_funding_edge_stddev

positive_edge_ratio
direction_consistency_ratio
sign_flip_count
longest_positive_edge_streak

current_basis
7d_mean_basis
7d_median_basis
7d_basis_stddev

provider_score_if_available
provider_rank_if_available
```

Do not invent a weighted ranking formula.

Expose metrics individually until the user defines ranking weights.

---

# 15. DIRECT VENUE VERIFICATION

**LOCKED REQUIREMENT**

Before any order, verify directly from both venues:

```text
market tradability/status
current funding
funding interval
next funding timestamp
best bid
best ask
order book
executable VWAP
instrument metadata
fees where available
account equity
available margin
current position
margin mode
leverage
liquidation information
open orders
recent fills
```

If Zero Delta and direct venue data materially disagree:

```text
DO NOT TRADE
```

Record:

```text
ZERO_DELTA_EXCHANGE_MISMATCH
```

and alert.

---

# 16. VENUE ADAPTER INTERFACE

Every executable venue should implement a consistent internal interface.

```python
class ExchangeAdapter:
    async def connect_market_streams(self): ...
    async def connect_private_streams(self): ...

    async def get_server_time(self): ...
    async def list_instruments(self): ...
    async def get_instrument_metadata(self, symbol): ...

    async def get_funding_info(self, symbol): ...
    async def get_funding_history(self, symbol): ...
    async def get_next_funding_time(self, symbol): ...

    async def get_ticker(self, symbol): ...
    async def get_order_book(self, symbol): ...
    async def get_candles(self, symbol, timeframe): ...

    async def get_account_equity(self): ...
    async def get_available_balance(self): ...
    async def get_positions(self): ...
    async def get_position(self, symbol): ...

    async def get_open_orders(self, symbol=None): ...
    async def get_order(self, order_id=None, client_order_id=None): ...
    async def get_fills(self, symbol=None): ...

    async def set_leverage(self, symbol, leverage): ...
    async def place_order(self, ...): ...
    async def cancel_order(self, ...): ...

    async def close(self): ...
```

Exchange-specific semantics remain inside adapters.

---

# 17. DYNAMIC INSTRUMENT METADATA

Do not hardcode changing exchange metadata.

Fetch where available:

```text
tick size
quantity step
minimum quantity
minimum notional
maximum quantity
symbol status
contract multiplier
funding interval
settlement asset
```

---

# 18. EXECUTABLE ENTRY SPREAD

Do not use last-traded price.

For a planned child order:

```text
LONG leg:
ask-side executable VWAP

SHORT leg:
bid-side executable VWAP
```

Definitions:

```text
P_long_buy   = estimated VWAP to buy long slice
P_short_sell = estimated VWAP to sell short slice

P_ref = (P_long_buy + P_short_sell) / 2

entry_spread_pct =
(P_short_sell - P_long_buy) / P_ref

negative_spread_cost_pct =
MAX(0, -entry_spread_pct)
```

---

# 19. NEGATIVE SPREAD RULE

**LOCKED REQUIREMENT**

Do not enter unless current venue funding is positive and the adverse entry spread costs no more than eight hours of the positive 7-day median net funding edge.

```text
negative_entry_spread_cost
<=
expected_net_funding_over_8_hours_at_7d_median
```

```text
current_net_funding_per_hour = short_funding_per_hour - long_funding_per_hour
median_net_funding_per_hour_7d = median of the aligned 7-day hourly pair edges
expected_net_funding_over_8_hours_at_7d_median = median_net_funding_per_hour_7d * 8
```

Current funding and the 7-day median must both be positive; missing median history does not pass. Otherwise:

```text
REJECT
```

---

# 20. FEES AND EXPECTED NET EDGE

Expected net edge:

```text
Expected Net Edge
=
Expected Funding Income
-
Entry Fees
-
Estimated Exit Fees
-
Negative Entry Basis Cost
-
Estimated Slippage
-
Other Explicit Trading Costs
```

Do not hardcode unverified fees.

```env
MIN_EXPECTED_NET_EDGE_BPS=
```

Status:

```text
REQUIRED_BEFORE_LIVE
```

---

# 21. LIQUIDITY / SLIPPAGE

Before every child slice:

- read current order books;
- estimate executable VWAP;
- estimate slippage;
- verify sufficient depth.

```env
MAX_ALLOWED_SLIPPAGE_BPS=
MIN_REQUIRED_BOOK_DEPTH_MULTIPLIER=
MAX_MARKET_DATA_AGE_MS=
```

Status:

```text
REQUIRED_BEFORE_LIVE
```

Never trade from stale order-book data.

---

# 22. LIVE VOLATILITY FILTER

**LOCKED REQUIREMENT**

Use 1-minute live market data.

Calculate:

```text
underlying_rv_percentile
basis_volatility_percentile
```

Then:

```text
low_vol_score =
MAX(
    underlying_rv_percentile,
    basis_volatility_percentile
)
```

Entry allowed only if:

```text
low_vol_score <= 60
```

```env
LOW_VOL_MAX_PERCENTILE=60
```

If >60:

```text
WAIT
```

not permanent rejection.

## Unspecified volatility parameters

Do not invent:

```text
RV window
basis-volatility window
percentile lookback
minimum observations
warm-up period
```

```env
RV_WINDOW_MINUTES=
BASIS_VOL_WINDOW_MINUTES=
VOL_PERCENTILE_LOOKBACK_DAYS=
VOL_MIN_OBSERVATIONS=
```

Status:

```text
REQUIRED_BEFORE_LIVE
```

---

# 23. POSITION SIZE

Total position size is not yet specified.

```env
TARGET_NOTIONAL_PER_LEG_USD=
```

Status:

```text
REQUIRED_BEFORE_LIVE
```

Target:

```text
long economic notional ≈ short economic notional
```

Do not blindly use equal contract quantities.

---

# 24. LEVERAGE

**LOCKED REQUIREMENT**

```text
Maximum leverage = 4x
```

```env
MAX_ALLOWED_LEVERAGE=4
TARGET_LEVERAGE=
```

`TARGET_LEVERAGE` is `REQUIRED_BEFORE_LIVE`.

Do not automatically use 4x just because it is the maximum.

---

# 25. ENTRY TWAP

**LOCKED REQUIREMENT**

```text
Entry duration = 6 hours
```

```env
TWAP_ENTRY_DURATION_MINUTES=360
```

Maximum child order:

```text
$100 notional per leg
```

```env
TWAP_MAX_CHILD_NOTIONAL_USD=100
```

One conceptual hedge slice:

```text
LONG  <= $100
SHORT <= $100
```

---

# 26. PAIRED EXECUTION

Each TWAP slice is one hedge intent:

```text
         HEDGE SLICE
         /            LONG LEG    SHORT LEG
         \        /
        FILL CHECK
            |
        DELTA CHECK
```

Submit both legs concurrently as technically possible.

```python
await asyncio.gather(
    submit_long_leg(),
    submit_short_leg(),
)
```

Log:

```text
long_submit_timestamp
short_submit_timestamp
submission_time_difference_ms
```

---

# 27. DELTA NEUTRALITY

**LOCKED REQUIREMENT**

Remain approximately delta neutral during entry, holding and exit.

Use actual filled exposure:

```text
net_delta_usd
=
long_filled_exposure_usd
-
short_filled_exposure_usd
```

Also:

```text
delta_ratio
=
abs(net_delta_usd)
/
max(long_exposure_usd, short_exposure_usd)
```

Do not use requested quantities.

Required:

```env
MAX_DELTA_IMBALANCE_USD=
MAX_DELTA_IMBALANCE_RATIO=
DELTA_HEDGE_TIMEOUT_MS=
```

Status:

```text
REQUIRED_BEFORE_LIVE
```

---

# 28. PARTIAL FILLS

Example:

```text
LONG  fills $100
SHORT fills $62
```

Imbalance:

```text
+$38 long
```

Required:

```text
STOP NEW TWAP SLICES
        ↓
RECONCILE ACTUAL FILLS
        ↓
COMPLETE MISSING HEDGE
        ↓
IF UNSUCCESSFUL:
REDUCE / FLATTEN EXCESS EXPOSURE
```

Do not continue the next slice while unsafe imbalance exists.

---

# 29. ONE-LEG FAILURE

Example:

```text
LONG accepted
SHORT rejected
```

Required:

```text
PAUSE ENTRY
        ↓
CHECK ACTUAL LONG FILL
        ↓
ATTEMPT SHORT HEDGE
        ↓
IF HEDGE FAILS:
FLATTEN EXCESS LONG
        ↓
RECONCILE
```

Never blindly retry both legs.

---

# 30. ORDER IDEMPOTENCY

Use deterministic client order IDs where supported:

```text
tradeId-sliceId-leg-attempt
```

Example:

```text
arb0192-s0042-L-a01
arb0192-s0042-S-a01
```

If an order call times out:

```text
DO NOT ASSUME FAILURE
```

Query actual order state before retrying.

---

# 31. HOLDING STATE

After the full hedge is established:

```text
state = HEDGED_OPEN
```

Continuously monitor:

```text
funding rates
next funding timestamps
actual funding payments
basis
basis volatility
underlying volatility
long account equity
short account equity
margin ratio
liquidation distance
delta
market-data freshness
exchange connectivity
order state
```

---

# 32. BASELINE EQUITY

Immediately before entry record separately:

```text
baseline_long_account_equity
baseline_short_account_equity
```

Prefer dedicated strategy accounts/subaccounts where possible.

---

# 33. CONTROLLED EXIT

**LOCKED REQUIREMENT**

If either side's equity reaches or falls below 30% of baseline:

```text
equity_ratio
=
current_account_equity / baseline_account_equity

equity_ratio <= 0.30
```

Action:

```text
CONTROLLED_EXIT
```

Duration:

```text
3 hours
```

```env
CONTROLLED_EXIT_EQUITY_RATIO=0.30
CONTROLLED_EXIT_DURATION_MINUTES=180
```

Use paired TWAP reduction and continue protecting delta.

---

# 34. EMERGENCY EXIT

**LOCKED REQUIREMENT**

If either side's equity falls below 10%:

```text
equity_ratio < 0.10
```

Action:

```text
EMERGENCY_EXIT
```

Target:

```text
approximately 1 minute
```

```env
EMERGENCY_EXIT_EQUITY_RATIO=0.10
EMERGENCY_EXIT_TARGET_SECONDS=60
```

Check emergency before controlled-exit logic.

Priority:

```text
1. Avoid liquidation/catastrophic loss
2. Preserve hedge as closely as possible
3. Close both legs
4. Minimize slippage where possible
```

---

# 35. LIQUIDATION / MARGIN OVERRIDE

Equity thresholds may not always prevent liquidation.

Support independent margin/liquidation safety rules.

```env
MIN_LIQUIDATION_DISTANCE_PCT=
MAX_MARGIN_UTILIZATION_PCT=
```

Status:

```text
REQUIRED_BEFORE_LIVE
```

Emergency risk reduction may occur before the 10% equity trigger if liquidation danger requires it.

---

# 36. NORMAL EXIT POLICY

The user has not yet defined the normal strategy exit.

Do not invent it.

Examples that are NOT authoritative:

```text
funding edge disappears
N funding settlements completed
expected edge turns negative
maximum hold duration
manual exit
```

```env
NORMAL_EXIT_POLICY=
```

Status:

```text
REQUIRED_BEFORE_LIVE
```

Until defined:

```text
PAPER MODE = allowed
TESTNET    = allowed
LIVE MODE  = blocked
```

---

# 37. RISK PRECEDENCE

Recommended priority:

```text
1. Imminent liquidation / margin emergency
2. Equity <10%
3. Severe unresolved delta imbalance
4. Exchange/order-system failure
5. Equity <=30%
6. Normal exit
7. Continue holding
```

---

# 38. ZERO DELTA FAILURE POLICY

If Zero Delta is unavailable before entry:

```text
DO NOT OPEN NEW TRADES
```

If Zero Delta is unavailable during an existing trade:

```text
CONTINUE MANAGEMENT USING DIRECT VENUE APIS
```

A Zero Delta outage must never prevent exit/risk management.

---

# 39. STALE DATA

Every market datum needs:

```text
source timestamp
local receive timestamp
age
```

If older than configured freshness limit:

```text
NO NEW ENTRY
```

During an active trade:

```text
pause new slices
reconcile directly
continue risk management
```

---

# 40. CONNECTION FAILURE

If required venue disconnected while flat:

```text
DO NOT ENTER
```

During entry:

```text
PAUSE TWAP
RECONCILE ORDERS
RECONCILE POSITIONS
PROTECT DELTA
```

During holding:

```text
TRY OFFICIAL REST/ALTERNATE CHANNEL
ALERT
DO NOT ASSUME POSITION STATE
```

If state cannot be established:

```text
DEGRADED
```

---

# 41. WEBSOCKET RECOVERY

Where WebSockets are used:

- reconnect automatically;
- use exponential backoff;
- detect gaps where supported;
- reload snapshots after reconnect;
- reconcile missed private events;
- verify state before resuming.

---

# 42. DATABASE

Recommended:

```text
PostgreSQL
```

Fast state / locks:

```text
Redis
```

Minimum entities:

```text
venues
instruments

zerodelta_markets
zerodelta_observations
zerodelta_history
zerodelta_candidates
zerodelta_provider_events

opportunities
strategy_decisions

trades
trade_legs
twap_slices

orders
fills
positions

funding_payments
equity_snapshots
risk_events
alerts
audit_logs
```

---

# 43. AUDITABILITY

Every trade must answer:

```text
What did Zero Delta report?
When?
Which venues?
What volume?
Was >=7 days available?
What were the 7-day funding metrics?
What did direct venues report?
What spread/fees/slippage were expected?
What volatility values were used?
Why was entry approved?
What orders were sent?
What actually filled?
What was maximum delta imbalance?
What funding was received?
Why did the trade exit?
What was final P&L?
```

---

# 44. MONEY PRECISION

Never use binary floating point for money-critical accounting.

Use:

```text
Decimal
```

or venue-native integer precision.

Round only when required for tick/quantity rules or display.

---

# 45. TIME

Use UTC internally.

Store UTC timestamps.

Never base trading logic on local timezone assumptions.

---

# 46. P&L

```text
Net P&L
=
Long Trading P&L
+
Short Trading P&L
+
Funding Received
-
Funding Paid
-
Trading Fees
-
Other Explicit Costs
```

Report separately:

```text
funding P&L
hedge/trading P&L
fees
slippage estimate
total realized P&L
```

---

# 47. FUNDING PAYMENT RECONCILIATION

Where supported, retrieve actual account funding transactions after settlement.

Store:

```text
venue
trade_id
symbol
timestamp
rate
notional
expected_payment
actual_payment
difference
```

Never treat predicted funding as actual received funding.

---

# 48. ALERTING

Send alerts for:

```text
Zero Delta auth failure
Zero Delta stale data
Zero Delta outage
candidate found
candidate rejected
direct venue mismatch
waiting for low volatility
entry started
entry paused
entry resumed
entry completed
partial fill
one-leg failure
delta imbalance
venue disconnect
stale direct data
margin warning
liquidation warning
30% controlled exit
<10% emergency exit
trade closed
funding payment
final P&L
critical error
manual intervention required
```

Never include secrets.

---

# 49. HEALTH MONITORING

Expose health for:

```text
PostgreSQL
Redis
Zero Delta auth
Zero Delta callback
Zero Delta data freshness
each venue REST
each venue public WS
each venue private WS
scanner
candidate analyzer
direct verifier
TWAP engine
delta manager
risk engine
accounting
notifications
```

---

# 50. KILL SWITCH

Support:

```text
PAUSE_NEW_ENTRIES
EMERGENCY_FLATTEN
```

`PAUSE_NEW_ENTRIES` stops new positions but keeps risk management active.

`EMERGENCY_FLATTEN` cancels entry work and begins protected closing of existing positions.

Require explicit authenticated action.

---

# 51. PORTFOLIO LIMITS

Not yet specified:

```env
MAX_CONCURRENT_TRADES=
MAX_TOTAL_GROSS_NOTIONAL_USD=
MAX_NOTIONAL_PER_VENUE_USD=
MAX_NOTIONAL_PER_ASSET_USD=
```

Status:

```text
REQUIRED_BEFORE_LIVE
```

---

# 52. CONCURRENCY

Two workers must never manage the same trade.

Use database locking, unique constraints and Redis distributed locks if multi-process.

One active trade must have one execution owner.

---

# 53. RETRIES

Read requests may use bounded exponential backoff.

Trading requests must not be blindly retried.

Ambiguous order outcome:

```text
timeout
  ↓
query actual order state
  ↓
reconcile
  ↓
then decide
```

---

# 54. RATE-LIMIT PRIORITY

When constrained:

```text
1. emergency risk requests
2. position/order reconciliation
3. execution
4. live market data
5. opportunity scanning
```

Scanner traffic must never starve risk management.

---

# 55. RESTART RECOVERY

On startup:

```text
Load unfinished trades
        ↓
Query actual positions
        ↓
Query open orders
        ↓
Query recent fills
        ↓
Compare DB vs venue state
        ↓
Reconcile
        ↓
Resume only if consistent
```

Otherwise:

```text
MANUAL_INTERVENTION_REQUIRED
```

No new trades.

---

# 56. PAPER MODE

Paper mode should use real market/provider data and simulated execution.

Reuse the same:

- scanner
- validator
- volatility filter
- TWAP scheduler
- delta manager
- risk engine
- state machine

Simulate where feasible:

```text
bid/ask
book depth
partial fills
fees
slippage
funding cashflows
TWAP
delta imbalance
controlled exit
emergency exit
```

---

# 57. TESTNET

Testnet verifies execution mechanics, not profitability.

Verify:

```text
authentication
signing
public/private streams
orders
cancellation
leverage
precision
reduce-only
client order IDs
partial fills
reconciliation
disconnect recovery
duplicate prevention
```

---

# 58. LIVE SAFETY GATE

Live mode must refuse startup unless all `REQUIRED_BEFORE_LIVE` values exist and pass validation.

```env
RUN_MODE=live
LIVE_TRADING_ENABLED=true
LIVE_TRADING_CONFIRMATION=I_UNDERSTAND_LIVE_ORDERS_USE_REAL_FUNDS
```

Without all gates:

```text
NO LIVE ORDERS
```

---

# 59. SECRET SECURITY

Never commit, log or expose:

```text
Zero Delta session
exchange API keys
API secrets
wallet private keys
Telegram bot secrets
```

Use environment variables / Docker secrets / secret manager.

Trading keys should not have withdrawal permission.

Use IP restrictions where supported.

---

# 60. NO AUTOMATIC WITHDRAWALS

**LOCKED SAFETY REQUIREMENT**

The bot must not:

- withdraw funds;
- modify withdrawal addresses;
- automatically transfer funds between unrelated venues.

Capital rebalancing is outside MVP.

---

# 61. RECOMMENDED TECH STACK

Unless explicitly changed:

```text
Python 3.12+
FastAPI
asyncio
Pydantic
SQLAlchemy
Alembic
httpx
PostgreSQL
Redis
pytest
pytest-asyncio
Docker
Docker Compose
```

Do not introduce Kubernetes for MVP unless requested.

---

# 62. RECOMMENDED PROJECT STRUCTURE

```text
funding-arbitrage-bot/
│
├── project.md
├── README.md
├── .env.example
├── pyproject.toml
├── docker-compose.yml
│
├── app/
│   ├── main.py
│   ├── core/
│   ├── domain/
│   ├── providers/
│   │   └── zerodelta/
│   ├── exchanges/
│   ├── market_data/
│   ├── strategy/
│   ├── execution/
│   ├── risk/
│   ├── accounting/
│   ├── persistence/
│   ├── notifications/
│   ├── api/
│   ├── workers/
│   └── tools/
│
├── tests/
│   ├── unit/
│   ├── providers/
│   ├── adapters/
│   ├── strategy/
│   ├── execution/
│   ├── risk/
│   ├── integration/
│   ├── chaos/
│   └── fixtures/
│
└── tools/
    └── zerodelta_capture/
```

---

# 63. TRADE STATE MACHINE

```text
DISCOVERED
    │
    ▼
VALIDATING_HISTORY
    ├── insufficient ──► DATA_INSUFFICIENT
    ▼
DIRECT_VERIFYING
    ├── mismatch ──────► EXCHANGE_MISMATCH
    ├── invalid ───────► REJECTED
    ▼
WAITING_FOR_LOW_VOL
    ▼
ENTRY_READY
    ▼
ENTERING
    ▼
HEDGED_OPEN
    ├── normal exit ───► EXIT_PENDING
    ├── <=30% ─────────► CONTROLLED_EXIT
    └── emergency ─────► EMERGENCY_EXIT
                            ▼
                         EXITING
                            ▼
                          CLOSED
```

Failure states:

```text
DEGRADED
RECONCILING
ERROR
MANUAL_INTERVENTION_REQUIRED
```

Persist all transitions.

---

# 64. DEVELOPMENT CLI

Required:

```bash
python -m app.tools.zerodelta_check BTC
```

Output:

```text
venue
venue type
market ID
current funding
funding interval
normalized hourly funding
avg 24h volume
history coverage
observation count
volume eligible?
history eligible?
```

Also:

```bash
python -m app.tools.zerodelta_opportunities BTC
```

Output:

```text
long venue
short venue
venue types
current normalized edge
7D mean edge
7D median edge
7D stddev
positive-edge ratio
direction consistency
sign flips
history coverage
volume status
```

Support BTC, ETH and SOL using existing fixtures.

---

# 65. TESTS — ZERO DELTA

Tests must use sanitized fixtures and must not hit the production premium account by default.

Test:

```text
valid callback response
missing field
unknown extra field
malformed JSON
binary/base64 history decode
duplicate timestamp
out-of-order timestamps
missing intervals
<7 days -> reject
exact 7 days -> accept
>7 days -> accept
volume below threshold
volume exactly threshold
volume above threshold
CEX
DEX
HIP3
unknown venue type
symbol mapping mismatch
funding interval normalization
auth failure
timeout
401
403
429
5xx
```

---

# 66. TESTS — STRATEGY

Funding normalization:

```text
1h
4h
8h
positive
negative
zero
```

Eight-hour 7-day median spread rule:

```text
current funding <= 0 -> reject
7-day median funding <= 0 or unavailable -> reject
spread < 8h of 7-day median funding -> pass
spread = 8h of 7-day median funding -> pass
spread > 8h of 7-day median funding -> reject
```

Low-volatility:

```text
max(20,40)=40 -> pass
max(60,60)=60 -> pass
max(20,61)=61 -> wait
```

Leverage:

```text
4.00x -> allowed
4.01x -> forbidden
```

TWAP child:

```text
$100.00 -> allowed
$100.01 -> forbidden
```

Equity:

```text
31%   -> no controlled trigger
30%   -> controlled exit
29%   -> controlled exit
10%   -> controlled exit because emergency is strictly <10%
9.99% -> emergency exit
```

---

# 67. TESTS — EXECUTION

Simulate:

```text
both legs full fill
long first
short first
long partial
short partial
one rejected
both rejected
timeout
duplicate acknowledgement
late fill after cancel
order-book movement
```

Verify unsafe imbalance blocks the next slice.

---

# 68. CHAOS TESTS

Test:

```text
Zero Delta outage
Zero Delta auth expiry
Zero Delta malformed response
venue disconnect
REST up / WS down
WS up / REST down
database outage
Redis outage
crash during entry
crash during exit
crash after submit before ACK
funding sign flip
basis widening
volatility spike
equity collapse
margin warning
liquidation warning
```

---

# 69. IMPLEMENTATION PHASES

## Phase 1 — Zero Delta provider

Inspect first:

```text
Capture_zero_Delta/zero_delta_fetcher.py
Capture_zero_Delta/capture_coin.py
Capture_zero_Delta/playwright_capture.py
Capture_zero_Delta/analyze_captured.py
Capture_zero_Delta/dash_dependencies.json
Capture_zero_Delta/dash_layout.json
Capture_zero_Delta/all_callbacks.txt
Capture_zero_Delta/*_v1_captured.json
Capture_zero_Delta/*_hourly_funding_history.csv
```

Deliver:

```text
ZeroDeltaProvider
normalization
>=7-day coverage validator
>$5M filter
BTC/ETH/SOL diagnostics
provider tests
```

No live orders.

## Phase 2 — 7-day analytics

Deliver compatible pair generation and 7-day metrics.

No live orders.

## Phase 3 — Direct venue market data

Deliver funding, next funding, books, candles, fees, metadata, freshness and health.

No live orders.

## Phase 4 — Deterministic validator

Deliver provider/direct mismatch check, 8h 7-day-median spread rule, fees, slippage, liquidity, expected net edge and volatility filter.

No live orders.

## Phase 5 — Paper paired TWAP

Deliver sizing, 6h TWAP, <=$100 child per leg, partial fills, delta manager and reconciliation.

## Phase 6 — Risk engine

Deliver equity thresholds, margin/liquidation monitoring, exits and kill switch.

## Phase 7 — Accounting / alerts

Deliver funding reconciliation, fees, P&L, audit and Telegram.

## Phase 8 — Testnet execution

Implement venue-specific order placement where supported.

## Phase 9 — Recovery / chaos

Do not skip.

## Phase 10 — Live readiness

Only after all required parameters are explicitly supplied and all gates pass.

---

# 70. `.env.example`

```env
# Runtime
RUN_MODE=paper
LIVE_TRADING_ENABLED=false
LIVE_TRADING_CONFIRMATION=

# Zero Delta
ZERO_DELTA_ENABLED=true
ZERO_DELTA_BASE_URL=https://www.zero-delta.xyz
ZERO_DELTA_DASH_UPDATE_PATH=/_dash-update-component
ZERO_DELTA_AUTH_MODE=session_cookie
ZERO_DELTA_SESSION_COOKIE=

ZERO_DELTA_MIN_AVG_24H_VOLUME_USD=5000000
ZERO_DELTA_PRIMARY_LOOKBACK_DAYS=7
ZERO_DELTA_MIN_LOOKBACK_DAYS=7

ZERO_DELTA_REQUEST_TIMEOUT_SECONDS=
ZERO_DELTA_REFRESH_INTERVAL_SECONDS=

# Locked strategy
MAX_ALLOWED_LEVERAGE=4
TWAP_ENTRY_DURATION_MINUTES=360
TWAP_MAX_CHILD_NOTIONAL_USD=100
LOW_VOL_MAX_PERCENTILE=60
FUNDING_SCREENING_HORIZON_MINUTES=480
CONTROLLED_EXIT_EQUITY_RATIO=0.30
CONTROLLED_EXIT_DURATION_MINUTES=180
EMERGENCY_EXIT_EQUITY_RATIO=0.10
EMERGENCY_EXIT_TARGET_SECONDS=60

# REQUIRED BEFORE LIVE
TARGET_NOTIONAL_PER_LEG_USD=
TARGET_LEVERAGE=

RV_WINDOW_MINUTES=
BASIS_VOL_WINDOW_MINUTES=
VOL_PERCENTILE_LOOKBACK_DAYS=
VOL_MIN_OBSERVATIONS=

MIN_EXPECTED_NET_EDGE_BPS=
MAX_ALLOWED_SLIPPAGE_BPS=
MIN_REQUIRED_BOOK_DEPTH_MULTIPLIER=
MAX_MARKET_DATA_AGE_MS=

MAX_DELTA_IMBALANCE_USD=
MAX_DELTA_IMBALANCE_RATIO=
DELTA_HEDGE_TIMEOUT_MS=

MIN_LIQUIDATION_DISTANCE_PCT=
MAX_MARGIN_UTILIZATION_PCT=

MAX_CONCURRENT_TRADES=
MAX_TOTAL_GROSS_NOTIONAL_USD=
MAX_NOTIONAL_PER_VENUE_USD=
MAX_NOTIONAL_PER_ASSET_USD=

NORMAL_EXIT_POLICY=

# Venue credentials
BINANCE_ENABLED=
BINANCE_API_KEY=
BINANCE_API_SECRET=

BYBIT_ENABLED=
BYBIT_API_KEY=
BYBIT_API_SECRET=

HYPERLIQUID_ENABLED=
HYPERLIQUID_WALLET_ADDRESS=
HYPERLIQUID_PRIVATE_KEY=

# Infrastructure
DATABASE_URL=
REDIS_URL=

# Notifications
TELEGRAM_ENABLED=false
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

---

# 71. REQUIRED USER DECISIONS BEFORE LIVE

Do not guess these:

```text
TARGET_NOTIONAL_PER_LEG_USD
TARGET_LEVERAGE

MAX_DELTA_IMBALANCE_USD
MAX_DELTA_IMBALANCE_RATIO
DELTA_HEDGE_TIMEOUT_MS

RV_WINDOW_MINUTES
BASIS_VOL_WINDOW_MINUTES
VOL_PERCENTILE_LOOKBACK_DAYS
VOL_MIN_OBSERVATIONS

MIN_EXPECTED_NET_EDGE_BPS
MAX_ALLOWED_SLIPPAGE_BPS
MIN_REQUIRED_BOOK_DEPTH_MULTIPLIER
MAX_MARKET_DATA_AGE_MS

MIN_LIQUIDATION_DISTANCE_PCT
MAX_MARGIN_UTILIZATION_PCT

MAX_CONCURRENT_TRADES
MAX_TOTAL_GROSS_NOTIONAL_USD
MAX_NOTIONAL_PER_VENUE_USD
MAX_NOTIONAL_PER_ASSET_USD

NORMAL_EXIT_POLICY
```

Until defined:

```text
PAPER MODE = ALLOWED
TESTNET    = ALLOWED
LIVE MODE  = BLOCKED
```

---

# 72. FORBIDDEN AI AGENT BEHAVIOR

The agent MUST NOT:

- invent Zero Delta callback fields;
- invent payloads or endpoints;
- invent exchange API behavior;
- invent fees;
- invent funding intervals;
- invent position size;
- invent volatility windows;
- invent normal exit logic;
- invent delta tolerance;
- compare unnormalized funding;
- use last price as executable spread;
- execute from CSV files;
- blindly trust Zero Delta for execution;
- continue TWAP during unsafe imbalance;
- retry ambiguous trading requests blindly;
- assume accepted order = filled order;
- hardcode changing exchange precision;
- enable live trading by default;
- store/log secrets;
- bypass premium/login controls;
- make withdrawals;
- allow AI to override deterministic risk controls;
- claim perfect zero delta can always be guaranteed.

---

# 73. DEFINITION OF DONE

The project is complete only when:

```text
✓ Zero Delta capture is a reusable provider
✓ CEX / DEX / HIP-3 supported
✓ >$5M filter enforced
✓ >=7 complete days required
✓ BTC / ETH / SOL fixtures pass
✓ funding intervals normalized
✓ compatible venue pairs generated
✓ 7-day analytics reproducible
✓ provider/direct mismatches detected
✓ direct venue data used for execution checks
✓ executable bid/ask/VWAP spread used
✓ fees/slippage included
✓ low-vol filter implemented exactly
✓ 4x max leverage enforced
✓ 6-hour paired TWAP works
✓ <=$100 child per leg enforced
✓ both legs submitted concurrently where possible
✓ partial fills reconciled
✓ delta monitored continuously
✓ unsafe imbalance blocks new slices
✓ one-leg failures handled safely
✓ 30% -> 3h controlled exit
✓ <10% -> emergency exit
✓ liquidation override exists
✓ exit remains approximately delta neutral
✓ Zero Delta outage cannot prevent risk management
✓ restart reconciliation works
✓ stale data blocks unsafe entry
✓ duplicate orders prevented
✓ actual funding reconciled
✓ P&L explainable
✓ audit history complete
✓ paper tests pass
✓ testnet tests pass where supported
✓ chaos/restart tests pass
✓ secrets protected
✓ live mode disabled by default
```

---

# 74. END-TO-END FLOW

```text
ZERO DELTA PREMIUM
        │
        ▼
Authenticated ZeroDeltaProvider
        │
        ▼
CEX + DEX + HIP-3
        │
        ▼
Avg 24H Volume > $5M
        │
        ▼
>= 7 Complete Days History
        │
        ▼
7-Day Opportunity Analytics
        │
        ▼
Candidate Pair
        │
        ▼
Direct Venue Verification
        │
        ├── Current funding
        ├── Funding interval
        ├── Next funding
        ├── Bid / Ask
        ├── Order book
        ├── Executable VWAP
        ├── Fees
        ├── Account equity
        ├── Margin
        └── Instrument metadata
        │
        ▼
Negative Spread <= 2H Expected Funding?
        │
        ├── NO ─────────► REJECT
        ▼
Fees / Slippage / Liquidity / Net Edge Valid?
        │
        ├── NO ─────────► REJECT
        ▼
1-Minute Volatility Filter
        │
        │ LowVolScore =
        │ MAX(RV percentile, Basis-vol percentile)
        │
        ├── >60 ────────► WAIT
        ▼
Position Sizing
        ▼
Leverage <= 4x
        ▼
6-Hour Paired TWAP
        │
        │ <= $100 child per leg
        ▼
Concurrent Long + Short
        ▼
Actual Fill Reconciliation
        ▼
Delta Safe?
        │
        ├── NO ─────────► HEDGE / RECONCILE / PAUSE
        ▼
HEDGED OPEN
        ▼
Funding Collection + Continuous Risk Monitoring
        │
        ├── Equity <=30% ─► 3-Hour Paired Controlled Exit
        ├── Equity <10%  ─► ~1-Minute Emergency Exit
        └── Margin/Liquidation Emergency ─► Emergency Risk Reduction
        ▼
CLOSED
        ▼
Funding / Fees / P&L Reconciliation
        ▼
Audit + Alerts
```

---

# 75. CORE DESIGN PRINCIPLE

When uncertain, prefer:

```text
NO TRADE
```

over:

```text
TRADE USING ASSUMED OR INVENTED INFORMATION
```

The bot may miss an opportunity.

It must not knowingly create uncontrolled directional exposure because information is missing, stale or ambiguous.

---

# 76. INSTRUCTION TO CODEX / AI AGENT

When working on this repository:

1. Read this entire `project.md`.
2. Inspect the current repository before modifying anything.
3. Inspect the existing `Capture_zero_Delta` implementation and fixtures.
4. Preserve verified capture behavior while refactoring it.
5. Before coding, output a concise implementation plan containing:
   - files to create;
   - files to modify;
   - database migrations;
   - tests;
   - unresolved facts.
6. Implement **Phase 1 only** unless the user explicitly asks for later phases.
7. Do not jump directly to live order placement.
8. Run tests after each phase.
9. Report any mismatch between this specification and actual provider/exchange data instead of guessing.
10. Never enable live trading without all required configuration and explicit user instruction.

This `project.md` is the single source of truth.

---

# 77. USER-REQUESTED PAPER RESEARCH WORKFLOW (2026-09-30)

The user's follow-up expands paper research to stock contracts with an earnings blackout and requests these fixes:

- Retain 14 days of Zero Delta hourly data. Rank on week 1, assess unchanged picks on week 2, and freeze each
  sweep's picks/parameters for a week-later score. Selection-week replay is explicitly in-sample.
- Default to one selected pair per underlying, showing current edge next to the seven-day mean; drop current
  nonpositive edges. Unsupported venues retain an explicit unavailable-forward-simulation status.
- Run forward shadow paper trials when the discretionary net-edge/portfolio gate rejects an otherwise valid
  selected pair. Persist the standard rejection and tag research trades; no live gate is bypassed.
- Both legs use equal BASE quantity, rounded to a common valid lot, with per-entry-child notional capped at $100.
- Target leverage 4x in paper accounts, equity per side = target notional / leverage. The 30% and below-10% equity
  rules remain primary in normal paper scenarios; independent liquidation protection remains available.
- Stock contracts require earnings clearance within +/-3 calendar days. Missing calendar data blocks stocks;
  crypto has no such condition. Non-US stocks require an explicit calendar date.
- Recheck spread and 1-minute volatility before every entry slice; watch waiting selections every minute.
- Add read-only public adapters for OKX, Gate, Bitget, MEXC and BloFin; order calls remain unsupported.

Historical hourly estimates disclose missing book/1-minute volatility data, ignored venue basis, assumed fees/slippage,
and today's volume/venue-universe filter. They are distinct from actual forward paper fills, funding and risk management.
The total position size and normal exit policy remain labelled paper parameters, not authoritative live decisions.

---

# 78. USER-REQUESTED POSTGRESQL DISCOVERY (2026-10-02)

The user authorized implementing the database-first plan based on the Zero Delta owner's table access:

- `marts.fct_coin_exchange_summary`: small hourly per-coin/per-venue screening feed.
- `public.perp_market_data`: indexed, bounded history queries, under the owner's 15-second query limit.
- `ZERO_DELTA_SOURCE=postgres` selects this transport behind the existing provider; `dash` remains the default until
  the URL and database contract are configured. PostgreSQL discovery does not require website session credentials.
- Never infer DATABASE units, timezones, funding kind, or access lifetime from the website's presentation. Explicitly
  configure the verified volume/funding/interval/epoch units, funding kind and UTC interpretation. Unknown values stop
  new discovery/admission. Raw observations preserve their source times; estimates are labelled and are not actual payments.
- Fetch the summary at UTC :05 hourly, with bounded retries. Screen **pair APR differences**, reserve research capacity
  for executable venues, then validate seven complete days of exact-market history. The bounded shortlist is disclosed;
  its ranks are not presented as an exhaustive raw-history search. Open interest is informational, not a new filter.
- Both legs must cover the same hourly analysis cutoff. Latest observations per UTC hour are retained without filling
  gaps. Each rate is normalized using its own interval; the existing missing-hour default remains zero.
- Compare provider funding against the timestamp-matched direct settlement or upcoming estimate period. Missing
  reference evidence waits; disagreement blocks. Existing spread, volatility, fee, sizing, leverage and risk gates apply.
- Keep the upstream connection separate from application persistence. Add an immutable local summary-batch table and
  freeze selected evidence/parameters. Re-fetch original selected markets for week-later assessment even if delisted
  from the current summary. Preserve trade observations and clearly labelled paper/shadow results.
- Do not retrospectively rank last week's entire market using today's shortlisted universe. Database mode uses frozen
  prospective picks; historical approximations retain their stated limitations.
- Expose spike concentration and mean-without-largest as diagnostics. Do not introduce ranking weights or automatic
  spike rejection thresholds without an explicit user-defined policy.
- The supplied summary schema has no authoritative refresh marker. Disclose this limitation; selected raw-market
  coverage establishes only those markets' freshness, not global summary refresh completion.
- Upstream access/timeout failures block new admission, clear waiting selections and alert, while the independent
  direct-venue engine continues managing existing positions and exits. No live mode is enabled by this migration.

Read-only preflight on 2026-10-02 confirmed the supplied access, 7,118 summaries, raw fraction funding, UTC/millisecond
timestamps and hour intervals on BTC Binance/Bybit/Hyperliquid. Raw samples arrive near HH:59 and are running estimates;
the analysis cutoff is the preceding completed hour at the next :05. Original source timestamps control freshness.
Null next-funding timestamps are resolved for comparison using actual direct-venue clocks/history, not guessed offsets.
Binance/Hyperliquid had 337 points; Bybit's missing hour remained a strict rejection. No global refresh watermark exists.
