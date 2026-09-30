"""Scanning every coin Zero Delta lists: the coin list, pacing, aborts, bounded memory and ranking across coins."""

import asyncio
import json
from decimal import Decimal

import httpx
import pytest

from app.core.clock import ManualClock
from app.core.config import ZeroDeltaSettings
from app.persistence.models import Selection, ZeroDeltaMarket
from app.providers.zerodelta import (
    AssetInfo,
    ZeroDeltaAuthError,
    ZeroDeltaParseError,
    ZeroDeltaProvider,
    ZeroDeltaRateLimitError,
    ZeroDeltaServerError,
)
from app.providers.zerodelta.client import ZeroDeltaClient
from tests.execution.fakes import FakeSource
from tests.execution.test_scanner import NOW, FakeZD, Rig, run
from tests.execution.test_selection_flow import APR, payload

D = Decimal
COINS = [f"COIN{i:03d}" for i in range(40)]


class MultiZD(FakeZD):
    """Zero Delta with many coins. `behaviour[coin]` may be an exception to raise; `apr` scales each coin's edge."""

    def __init__(self, clock, coins, behaviour=None, listing=None):
        super().__init__(clock, payload())
        self.coins, self.behaviour, self.listing = coins, behaviour or {}, listing
        self.fetched: list[str] = []
        self.active = self.peak = 0

    async def list_assets(self):
        if isinstance(self.listing, Exception):
            raise self.listing
        return self.listing if self.listing is not None else [AssetInfo(coin=c, avg_volume_m=D(100 - i)) for i, c in enumerate(self.coins)]

    async def fetch_asset(self, asset, days=None, keep_days=None):
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0)  # let other coins start, so concurrency is observable
            self.fetched.append(asset)
            outcome = self.behaviour.get(asset)
            if isinstance(outcome, Exception):
                raise outcome
            self.payload = payload(asset=asset)
            return await super().fetch_asset(asset, days, keep_days)
        finally:
            self.active -= 1


def rig(coins=COINS, behaviour=None, listing=None, **params_kw):
    clock = ManualClock(NOW)
    zd = MultiZD(clock, coins, behaviour, listing)
    sources = {v: FakeSource(v, 100, clock=None, rate_per_hour=str(D(str(APR[v])) / 8760 / 100)) for v in "ABC"}
    r = Rig(zd=zd, sources=sources, params_kw=dict(selection_count=3, **params_kw))
    zd.clock = r.clock
    for s in sources.values():
        s.clock = r.clock
    r.kill.pause("op", "look only: the fake venues only carry BTC")  # verification still runs; nothing opens
    return r, zd


# ---- the coin list -----------------------------------------------------------------------------

def test_provider_lists_every_coin_most_liquid_first():
    body = {"response": {"coin-table": {"rowData": [
        {"coin": "small", "avg_volume": 3.5}, {"coin": "BTC", "avg_volume": 40000}, {"coin": "", "avg_volume": 1},
        {"coin": "NOVOL", "avg_volume": None}, {"coin": "ETH", "avg_volume": 9000.5}]}}}
    st = ZeroDeltaSettings(_env_file=None, session_cookie="x", user_id=1)
    client = ZeroDeltaClient(st, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)))
    assets = asyncio.run(ZeroDeltaProvider(st, client=client).list_assets())
    assert [a.coin for a in assets] == ["BTC", "ETH", "SMALL", "NOVOL"]  # upper-cased, blank dropped, volume descending
    assert assets[0].avg_volume_m == D("40000") and assets[-1].avg_volume_m is None


def test_provider_rejects_a_response_without_a_coin_table():
    st = ZeroDeltaSettings(_env_file=None, session_cookie="x", user_id=1)
    client = ZeroDeltaClient(st, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"response": {}})))
    with pytest.raises(ZeroDeltaParseError):
        asyncio.run(ZeroDeltaProvider(st, client=client).list_assets())


def test_all_means_every_listed_coin_and_exclusions_are_honoured():
    r, zd = rig(["BTC", "ETH", "TSLA", "SOL"])
    assert run(r.scanner.resolve_assets(["ALL"])) == ["BTC", "ETH", "TSLA", "SOL"]
    assert run(r.scanner.resolve_assets(["all"], ["tsla", "sol"])) == ["BTC", "ETH"]
    assert run(r.scanner.resolve_assets(["btc", "doge"])) == ["BTC", "DOGE"]  # an explicit list is used as given
    assert run(r.scanner.resolve_assets(["ALL", "PUMP"], ["ETH"])) == ["BTC", "TSLA", "SOL", "PUMP"]  # extras are added


def test_a_failing_coin_list_falls_back_to_the_last_known_list_and_alerts():
    r, zd = rig(["BTC", "ETH"])
    assert run(r.scanner.resolve_assets(["ALL"])) == ["BTC", "ETH"]
    zd.listing = ZeroDeltaServerError(503)
    assert run(r.scanner.resolve_assets(["ALL"])) == ["BTC", "ETH"]  # remembered from the last good answer
    assert r.alerts("zero_delta_outage")
    fresh, zd2 = rig(["BTC"], listing=ZeroDeltaAuthError(401))
    assert run(fresh.scanner.resolve_assets(["ALL"])) == [] and fresh.alerts("zero_delta_auth_failure")


# ---- the sweep ---------------------------------------------------------------------------------

def test_sweep_scans_every_coin_gently_and_reports_totals():
    r, zd = rig(COINS, sweep_concurrency=2)
    reports = run(r.scanner.scan_all(COINS))
    assert len(reports) == 40 and sorted(zd.fetched) == COINS
    assert zd.peak == 2  # never more than the configured concurrency at once
    sw = r.scanner.sweep
    assert sw["assets_total"] == 40 and sw["assets_scanned"] == 40 and sw["assets_failed"] == 0 and sw["aborted"] is None
    assert sw["pairs_total"] == 40 * 6 and sw["tradable_pairs"] == 40 * 3
    assert r.scanner.progress["running"] is False and r.scanner.progress["done"] == 40


def test_pacing_pauses_after_every_request():
    r, zd = rig(COINS[:5], sweep_delay_seconds=2, sweep_concurrency=1)
    before = r.clock.now()
    run(r.scanner.scan_all(COINS[:5]))
    assert (r.clock.now() - before).total_seconds() >= 10  # 5 coins x 2 s pause


def test_selection_ranks_across_all_coins_not_per_coin():
    r, _ = rig(COINS)
    run(r.scanner.scan_all(COINS))
    sel = r.scanner.last_selection
    assert sel["pairs_considered"] == 240  # every pair of every coin counted...
    assert len(sel["overall"]) == 3 and len(sel["tradable"]) == 3  # ...but only the best three are selected
    assert sel["overall"][0]["score_apr"] == pytest.approx(400.0)
    with r.repo.session() as s:
        assert s.query(Selection).count() == 6


def test_a_bad_coin_is_skipped_and_the_sweep_carries_on():
    r, zd = rig(COINS, behaviour={"COIN005": ZeroDeltaServerError(500), "COIN006": ZeroDeltaParseError("bad json")})
    reports = run(r.scanner.scan_all(COINS))
    sw = r.scanner.sweep
    assert sw["assets_scanned"] == 38 and sw["assets_failed"] == 2 and sw["aborted"] is None
    assert not r.alerts("zero_delta_outage")  # two isolated failures are not an outage
    assert r.scanner.last_selection["overall"]  # and the other 38 coins were still ranked


def test_many_errors_in_a_row_abort_the_sweep_as_an_outage():
    bad = {c: ZeroDeltaServerError(503) for c in COINS[:8]}
    r, zd = rig(COINS, behaviour=bad, sweep_concurrency=1)
    run(r.scanner.scan_all(COINS))
    assert r.scanner.sweep["aborted"] == "outage" and r.scanner.sweep["assets_skipped"] > 20
    assert len(r.alerts("zero_delta_outage")) == 1
    assert len(zd.fetched) < 20  # it stopped asking instead of hammering a broken service


def test_auth_failure_stops_the_sweep_at_once():
    r, zd = rig(COINS, behaviour={"COIN003": ZeroDeltaAuthError(403)}, sweep_concurrency=1)
    run(r.scanner.scan_all(COINS))
    assert r.scanner.sweep["aborted"] == "auth" and len(zd.fetched) == 4
    assert len(r.alerts("zero_delta_auth_failure")) == 1 and r.repo.all_trades() == []


def test_rate_limit_stops_the_sweep_and_is_not_worked_around():
    r, zd = rig(COINS, behaviour={"COIN002": ZeroDeltaRateLimitError("Zero Delta rate limit hit (HTTP 429)")},
                sweep_concurrency=1)
    run(r.scanner.scan_all(COINS))
    assert r.scanner.sweep["aborted"] == "rate_limit" and len(zd.fetched) == 3  # no retry, no further requests
    assert "rate limited" in r.alerts("zero_delta_outage")[0].message


def test_partial_results_are_still_used_when_a_sweep_is_cut_short():
    r, zd = rig(COINS, behaviour={"COIN010": ZeroDeltaRateLimitError("429")}, sweep_concurrency=1)
    run(r.scanner.scan_all(COINS))
    assert r.scanner.sweep["assets_scanned"] == 10 and r.scanner.last_selection["overall"]  # the first ten were ranked


# ---- memory, storage and display ---------------------------------------------------------------

def test_only_candidate_pairs_are_kept_per_coin():
    r, _ = rig(["BTC"], KEEP=None) if False else rig(["BTC"])
    report, kept, pairs, earlier, failure = run(r.scanner._discover("BTC"))
    assert failure is None and report.pairs_found == 6
    assert len(pairs) <= 6 and set(kept.histories) <= {"A", "B", "C", "X"}
    # the histories kept are exactly the venues of the kept pairs, never the whole 30-venue book of a big coin
    assert set(kept.histories) == {v for p in pairs for v in (p.long_leg.venue_name, p.short_leg.venue_name)}


def test_old_discovery_rows_are_pruned_but_selections_and_trades_are_not():
    r, _ = rig(["BTC"])
    run(r.scanner.scan_all(["BTC"]))
    with r.repo.session() as s:
        fresh = s.query(ZeroDeltaMarket).count()
    assert fresh > 0
    r.clock.advance(hours=100)
    r.repo.prune_discovery(72)
    with r.repo.session() as s:
        assert s.query(ZeroDeltaMarket).count() == 0
        assert s.query(Selection).count() > 0  # the history of what was selected is kept


def test_the_global_top_pairs_list_carries_the_asset():
    r, _ = rig(COINS[:4])
    run(r.scanner.scan_all(COINS[:4]))
    top = r.scanner.last_top_pairs
    assert top and {row["asset"] for row in top} <= set(COINS[:4])
    assert [row["mean_apr"] for row in top] == sorted((row["mean_apr"] for row in top), reverse=True)


def test_overview_summarises_the_sweep_and_lists_only_relevant_coins():
    from tests.unit.test_dashboard import make
    r, _ = rig(COINS)
    run(r.scanner.scan_all(COINS))
    c, _ = make(r)
    o = c.get("/api/overview").json()
    assert o["sweep"]["assets_total"] == 40 and o["sweep"]["pairs_total"] == 240 and o["sweep"]["progress"]["running"] is False
    assert len(o["scans"]) <= 24  # not hundreds of cards
    opp = c.get("/api/opportunities").json()["pairs"]
    assert opp and "asset" in opp[0] and len(opp) <= 60
    json.dumps(o)
