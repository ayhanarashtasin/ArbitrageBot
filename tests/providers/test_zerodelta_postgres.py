"""Database contract and decision-level regressions. No production connections or credentials."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.clock import ManualClock
from app.core.config import ZeroDeltaSettings
from app.providers.zerodelta.exceptions import ZeroDeltaConfigError, ZeroDeltaError
from app.providers.zerodelta.models import SummaryBatch, SummaryMarket
from app.providers.zerodelta.postgres import PostgresClient, next_refresh
from app.providers.zerodelta.provider import ZeroDeltaProvider
from app.strategy.analytics import compute_edge_stats
from app.strategy.pairs import generate_pairs
from app.strategy.selection import screen_summary
from app.strategy.verification import zero_delta_mismatch

D = Decimal
END = datetime(2026, 10, 2, 12, tzinfo=UTC)


def run(coro):
    return asyncio.run(coro)


def settings(**kwargs):
    defaults = dict(source="postgres", database_url="postgresql://test:test@localhost/test",
        db_volume_unit="millions", db_funding_unit="fraction", db_interval_unit="hours",
        db_timestamp_unit="milliseconds", db_funding_kind="settled", db_datetime_utc=True)
    defaults.update(kwargs)
    return ZeroDeltaSettings(_env_file=None, **defaults)


def summary(coin="BTC", exchange="binance_usdt", apr=0, volume=10):
    return dict(coin=coin, exchange=exchange, avg_volume=volume, avg_open_interest=None, avg_apr_7d=apr)


def history(exchange="binance_usdt", rate=0, interval=8, end=END, coin="BTC"):
    out = []
    for n in range(337):
        at = end - timedelta(hours=336 - n) + timedelta(minutes=59, seconds=5)
        out.append(dict(exchange=exchange, symbol=f"{coin}/USDT:USDT", datetime=at.replace(tzinfo=None),
            timestamp=int(at.timestamp() * 1000), fundingrate=rate, interval=interval,
            markprice=100, volume=123, openinterest=None, nextfundingtime=int((at + timedelta(hours=8)).timestamp() * 1000)))
    return out


class Database:
    def __init__(self):
        self.rows = [summary(), summary(exchange="bybit_usdt", apr=262.8)]
        self.data = {("BTC", "binance_usdt"): history(),
                     ("BTC", "bybit_usdt"): history("bybit_usdt", rate=0.0024)}
        self.requests = []
        self.error = None

    async def summary(self):
        if self.error:
            raise self.error
        return self.rows

    async def symbols(self, coin, exchange, start, end):
        return [{"symbol": s} for s in sorted({r["symbol"] for r in self.data.get((coin, exchange), [])})]

    async def history(self, coin, exchange, symbol, start, end):
        self.requests.append((coin, exchange, symbol, start, end))
        return [r for r in self.data.get((coin, exchange), [])
                if r["symbol"] == symbol and start <= r["datetime"].replace(tzinfo=UTC) <= end]

    async def aclose(self):
        pass


def provider(**kwargs):
    db = Database()
    clock = ManualClock(END + timedelta(hours=1, minutes=5))
    p = ZeroDeltaProvider(settings(**kwargs), postgres_client=db, now=clock.now)
    return p, db, clock


def test_database_units_produce_expected_edge_and_boundary_inclusive_history():
    p, db, _ = provider()
    a = run(p.fetch_asset("BTC", keep_days=14))
    assert all(m.eligible for m in a.markets)
    assert a.markets[0].snapshot.avg_24h_volume_usd == D(10_000_000)
    assert a.markets[0].snapshot.open_interest_usd is None
    assert len(a.histories["Bybit USDT"]) == 337
    pair = generate_pairs(a, p._settings)[0]
    assert pair.coverage.coverage_seconds == 168 * 3600
    assert pair.coverage.observation_count == 169
    assert pair.stats.mean == D("0.03")
    assert pair.stats.mean * 8760 == D("262.80")
    assert pair.short_leg.exchange_key == "bybit_usdt"
    assert len(db.requests) == 2


@pytest.mark.parametrize("unit,rate", [("fraction", .0008), ("percent", .08)])
@pytest.mark.parametrize("interval_unit,interval", [("hours", 8), ("minutes", 480), ("seconds", 28800)])
def test_funding_units_and_intervals_do_not_change_the_hourly_yield(unit, rate, interval_unit, interval):
    p, db, _ = provider(db_funding_unit=unit, db_interval_unit=interval_unit)
    db.data[("BTC", "bybit_usdt")] = history("bybit_usdt", rate=rate, interval=interval)
    db.data[("BTC", "binance_usdt")] = history(interval=interval)
    a = run(p.fetch_asset("BTC"))
    assert a.markets[1].snapshot.normalized_funding_rate_per_hour == D("0.01")


def test_interval_changes_use_each_observations_interval():
    p, db, _ = provider()
    for r in db.data[("BTC", "bybit_usdt")][170:]:
        r.update(interval=4, fundingrate=.0012)
    a = run(p.fetch_asset("BTC", keep_days=14))
    assert {o.normalized_funding_rate_per_hour for o in a.histories["Bybit USDT"]} == {D("0.03")}


@pytest.mark.parametrize("volume,ok", [(4.99, False), (5, False), (5.01, True)])
def test_volume_boundary_is_strict_per_venue(volume, ok):
    p, db, _ = provider()
    db.rows[1]["avg_volume"] = volume
    a = run(p.fetch_asset("BTC"))
    assert a.markets[1].eligible is ok


def test_missing_tail_does_not_pass_because_another_venue_is_current():
    p, db, _ = provider()
    db.data[("BTC", "bybit_usdt")].pop()
    a = run(p.fetch_asset("BTC"))
    assert a.markets[0].eligible
    assert not a.markets[1].eligible
    assert "END_NOT_COVERED" in a.markets[1].reasons
    assert generate_pairs(a, p._settings) == []


def test_gaps_are_not_filled_and_conflicts_are_not_arbitrarily_chosen():
    p, db, _ = provider()
    del db.data[("BTC", "bybit_usdt")][250]
    assert not run(p.fetch_asset("BTC")).markets[1].eligible
    p, db, _ = provider()
    row = dict(db.data[("BTC", "bybit_usdt")][250], fundingrate=1)
    db.data[("BTC", "bybit_usdt")].append(row)
    a = run(p.fetch_asset("BTC"))
    assert len(a.markets) == 1 and a.issues[0].code == "INVALID_HISTORY"


def test_ambiguous_symbols_and_duplicate_summaries_do_not_select_a_market():
    p, db, _ = provider()
    db.rows.append(dict(db.rows[1]))
    batch = run(p.refresh_summary())
    assert [m.exchange for m in batch.markets] == ["binance_usdt"]
    p, db, _ = provider()
    db.data[("BTC", "bybit_usdt")].append(dict(db.data[("BTC", "bybit_usdt")][-1], symbol="BTC/USDC:USDC"))
    a = run(p.fetch_asset("BTC"))
    assert "Bybit USDT" not in a.histories
    assert "unique" in a.issues[0].message


def test_wrong_epoch_units_and_nan_are_reported_not_zeroed():
    p, db, _ = provider(db_timestamp_unit="seconds")
    a = run(p.fetch_asset("BTC"))
    assert not a.markets and a.issues
    p, db, _ = provider()
    db.data[("BTC", "bybit_usdt")][-1]["fundingrate"] = float("nan")
    a = run(p.fetch_asset("BTC"))
    assert "Bybit USDT" not in a.histories


def test_incremental_fetch_refreshes_last_hour_and_keeps_full_window():
    p, db, _ = provider()
    run(p.fetch_asset("BTC", keep_days=14))
    db.data[("BTC", "bybit_usdt")][-1]["fundingrate"] = .0048
    a = run(p.fetch_asset("BTC", keep_days=14))
    assert db.requests[-1][3] == END - timedelta(hours=1)
    assert len(a.histories["Bybit USDT"]) == 337
    assert a.histories["Bybit USDT"][-1].normalized_funding_rate_per_hour == D("0.06")


@pytest.mark.parametrize("keep_days", [7, 14])
def test_incremental_fetch_adopts_an_older_real_backfill_without_filling_the_gap(keep_days):
    p, db, _ = provider()
    rows = db.data[("BTC", "bybit_usdt")]
    absent = rows.pop(250)  # 86 hours before the current boundary: outside the ordinary one-hour refresh.
    a = run(p.fetch_asset("BTC", keep_days=keep_days))
    assert not a.markets[1].eligible
    assert a.markets[1].coverage.missing_intervals == 1
    a = run(p.fetch_asset("BTC", keep_days=keep_days))
    assert not a.markets[1].eligible  # no interpolation while the upstream observation is still absent
    assert db.requests[-1][3] == END - timedelta(hours=86)
    rows.insert(250, absent)
    a = run(p.fetch_asset("BTC", keep_days=keep_days))
    assert a.markets[1].eligible and a.markets[1].coverage.missing_intervals == 0
    assert len(a.histories["Bybit USDT"]) == keep_days * 24 + 1
    run(p.fetch_asset("BTC", keep_days=keep_days))
    assert db.requests[-1][3] == END - timedelta(hours=1)


def test_increasing_the_retained_window_fetches_the_older_real_history():
    p, db, _ = provider()
    assert len(run(p.fetch_asset("BTC", keep_days=7)).histories["Bybit USDT"]) == 169
    a = run(p.fetch_asset("BTC", keep_days=14))
    assert db.requests[-1][3] == END - timedelta(days=14)
    assert len(a.histories["Bybit USDT"]) == 337


def test_unknown_database_contract_is_not_inferred_from_dash_settings():
    p = ZeroDeltaProvider(ZeroDeltaSettings(_env_file=None, source="postgres"), postgres_client=Database())
    with pytest.raises(ZeroDeltaConfigError, match="semantics"):
        run(p.refresh_summary())


def test_upstream_cannot_be_used_as_the_bots_writable_database():
    from app.bootstrap import build_runtime
    from app.core.trading_config import TradingSettings
    with pytest.raises(ZeroDeltaConfigError, match="must not point"):
        build_runtime(TradingSettings(_env_file=None, database_url="postgresql+psycopg://other:other@localhost/test"),
                      settings())


def test_single_spike_has_high_concentration_and_stable_series_does_not():
    spiky = compute_edge_stats([D(0)] * 168 + [D(169)])
    stable = compute_edge_stats([D(1)] * 169)
    assert spiky.mean == stable.mean == 1
    assert spiky.median == 0 and stable.median == 1
    assert spiky.largest_positive_contribution == 1
    assert spiky.mean_without_largest == 0
    assert stable.top_three_positive_contribution == D(3) / 169
    assert compute_edge_stats([D(0), D(0)]).largest_positive_contribution is None


@pytest.mark.parametrize("at,expected", [(END, END + timedelta(minutes=5)),
    (END + timedelta(minutes=5), END + timedelta(hours=1, minutes=5)),
    (END + timedelta(minutes=57), END + timedelta(hours=1, minutes=5))])
def test_refresh_is_a_wall_clock_boundary_not_scan_duration_plus_an_hour(at, expected):
    assert next_refresh(at) == expected


def test_screening_uses_spreads_and_reserves_budget_for_executable_venues():
    rows = [SummaryMarket(coin=coin, exchange=v, venue_name=v, avg_volume_usd=D(10_000_000), avg_apr_7d=D(apr))
        for coin, v, apr in [("BTC", "A", 65), ("BTC", "B", 70), ("ETH", "A", -10), ("ETH", "B", 25),
                             ("SOL", "X", -500), ("SOL", "Y", 500)]]
    batch = SummaryBatch(id="batch", as_of=END, fetched_at=END, markets=tuple(rows))
    sources = {"A": object(), "B": object()}
    screened = screen_summary(batch, None, set(), D(5_000_000), 2, sources.get)
    assert [r["asset"] for r in screened] == ["SOL", "ETH"]
    assert screened[1]["provisional_apr"] == 35
    assert screened[1]["long"] == "A"


def test_settled_cross_check_uses_the_settlement_before_the_source_timestamp():
    from tests.unit.helpers import funding
    p, _, _ = provider()
    pair = generate_pairs(run(p.fetch_asset("BTC")), p._settings)[0]
    lf, sf = funding("0"), funding("0.0007")
    # A new settlement since the snapshot has a different rate. The earlier matching period agrees.
    rows = ((END - timedelta(hours=4), D("0.0024")), (END + timedelta(hours=4), D("0.0056")))
    checks = zero_delta_mismatch(pair, lf, sf, D("0.000001"),
        histories=(((END - timedelta(hours=4), D(0)),), rows))
    assert all(c.passed for c in checks)
    checks = zero_delta_mismatch(pair, lf, sf, D("0.000001"))
    assert any(not c.passed and c.blocking == "wait" for c in checks)


def test_predicted_snapshot_without_next_time_uses_direct_funding_period():
    from tests.unit.helpers import funding
    p, db, _ = provider(db_funding_kind="predicted")
    for rows in db.data.values():
        for row in rows:
            row["nextfundingtime"] = None
    pair = generate_pairs(run(p.fetch_asset("BTC")), p._settings)[0]
    long = funding("0").model_copy(update={"next_funding_time": END + timedelta(hours=4)})
    short = funding("0.0003").model_copy(update={"next_funding_time": END + timedelta(hours=4)})
    assert all(c.passed for c in zero_delta_mismatch(pair, long, short, D("0.000001")))
    # Later: neither the unrelated next-period estimate nor the final settlement proves the historical prediction.
    later = short.model_copy(update={"funding_rate": D("0.0056"),
                                     "next_funding_time": END + timedelta(hours=12)})
    later_long = long.model_copy(update={"next_funding_time": END + timedelta(hours=12)})
    checks = zero_delta_mismatch(pair, later_long, later, D("0.000001"),
        histories=(((END + timedelta(hours=4), D(0)),), ((END + timedelta(hours=4), D("0.0024")),)))
    assert any(not c.passed and c.blocking == "wait" for c in checks)
    assert not any(not c.passed and c.blocking == "mismatch" for c in checks)
    # Direct estimates captured near the actual upstream source time do prove the historical prediction.
    at = pair.short_leg.funding_as_of
    recorded_long = long.model_copy(update={"source_time": at, "received_at": at, "is_predicted": True})
    recorded_short = short.model_copy(update={"source_time": at, "received_at": at, "is_predicted": True})
    checks = zero_delta_mismatch(pair, later_long, later, D("0.000001"),
        predictions=((recorded_long,), (recorded_short,)))
    assert all(c.passed for c in checks)


def test_changed_later_estimate_waits_but_a_same_time_disagreement_is_a_mismatch():
    from tests.unit.helpers import funding
    p, db, _ = provider(db_funding_kind="predicted")
    for rows in db.data.values():
        for row in rows:
            row["nextfundingtime"] = None
    pair = generate_pairs(run(p.fetch_asset("BTC")), p._settings)[0]
    at = pair.short_leg.funding_as_of
    long = funding("0", now=at).model_copy(update={"next_funding_time": END + timedelta(hours=4)})
    short = funding("0.0008", now=at + timedelta(minutes=5)).model_copy(update={"next_funding_time": END + timedelta(hours=4)})
    checks = zero_delta_mismatch(pair, long, short, D("0.000001"))
    changed = next(c for c in checks if c.name == "short_funding_rate_matches")
    assert not changed.passed and changed.blocking == "wait"
    assert "later_estimate" in changed.value and "zd=" in changed.value and "source=" in changed.value
    same_time = short.model_copy(update={"source_time": at, "received_at": at})
    checks = zero_delta_mismatch(pair, long, same_time, D("0.000001"))
    assert next(c for c in checks if c.name == "short_funding_rate_matches").blocking == "mismatch"


def test_driver_errors_do_not_expose_connection_secrets(monkeypatch):
    import psycopg
    async def connect(**kwargs):
        raise psycopg.OperationalError("connection password=private-password")
    monkeypatch.setattr(psycopg.AsyncConnection, "connect", connect)
    with pytest.raises(ZeroDeltaError) as error:
        run(PostgresClient(settings()).query("SELECT 1"))
    assert "private-password" not in str(error.value)
    assert error.value.__suppress_context__


def test_driver_sets_read_only_timeout_and_binds_parameters(monkeypatch):
    import psycopg
    seen = {}
    class Cursor:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): seen["cursor_closed"] = True
        async def execute(self, sql, params): seen.update(sql=sql, params=params)
        async def fetchall(self): return [{"answer": 1}]
    class Connection:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): seen["connection_closed"] = True
        def cursor(self, **kwargs): return Cursor()
    async def connect(**kwargs):
        seen["options"] = kwargs["options"]
        seen["sslmode"] = kwargs["sslmode"]
        return Connection()
    monkeypatch.setattr(psycopg.AsyncConnection, "connect", connect)
    assert run(PostgresClient(settings()).query("SELECT %s", ("BTC' OR TRUE --",))) == [{"answer": 1}]
    assert "read_only=on" in seen["options"] and "statement_timeout=12000" in seen["options"]
    assert seen["params"] == ("BTC' OR TRUE --",) and seen["sslmode"] == "require"
    assert seen["connection_closed"] and seen["cursor_closed"]


def test_history_timeout_excludes_only_that_market_and_never_uses_its_old_cache():
    from app.providers.zerodelta.exceptions import ZeroDeltaTimeoutError
    p, db, _ = provider()
    run(p.fetch_asset("BTC"))  # the old cache must not stand in for the failed current read
    original = db.history
    async def timeout(coin, exchange, *args):
        if exchange == "bybit_usdt":
            raise ZeroDeltaTimeoutError("bounded history read timed out")
        return await original(coin, exchange, *args)
    db.history = timeout
    a = run(p.fetch_asset("BTC"))
    assert [m.snapshot.venue_name for m in a.markets] == ["Binance USDT"]
    assert "Bybit USDT" not in a.histories and generate_pairs(a, p._settings) == []
    assert a.issues[0].code == "QUERY_TIMEOUT" and a.issues[0].venue == "Bybit USDT"


def test_query_timeout_metrics_and_logs_do_not_expose_credentials(monkeypatch, caplog):
    import psycopg
    from app.providers.zerodelta.exceptions import ZeroDeltaTimeoutError
    async def connect(**kwargs):
        raise psycopg.errors.QueryCanceled("secret-password")
    monkeypatch.setattr(psycopg.AsyncConnection, "connect", connect)
    client = PostgresClient(settings())
    with pytest.raises(ZeroDeltaTimeoutError):
        run(client.query("SELECT 1"))
    d = client.diagnostics()
    assert d["queries"] == d["failed_queries"] == d["timeouts"] == 1
    assert d["statement_budget_ms"] == 12000
    assert "timeout" in caplog.text and "secret-password" not in caplog.text
