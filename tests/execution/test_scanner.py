"""Scanner: Zero Delta discovery -> direct verification -> engine, with every failure mode."""

import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest

from app.core.clock import ManualClock
from app.core.config import ZeroDeltaSettings
from app.core.health import Health
from app.core.trading_config import TradingSettings
from app.execution.engine import TradeEngine
from app.execution.venue_provider import PaperVenueProvider
from app.notifications.notifier import Notifier
from app.persistence.repository import Repository
from app.providers.zerodelta import (
    ZeroDeltaAuthError,
    ZeroDeltaProvider,
    ZeroDeltaServerError,
)
from app.risk.killswitch import KillSwitch
from app.workers.scanner import Scanner
from tests.execution.fakes import FakeSource
from tests.providers.helpers import T0 as ZD_T0, response, row, trace

D = Decimal
NOW = ZD_T0 + timedelta(hours=169)  # one hour after the newest history point


def run(coro):
    return asyncio.run(coro)


def calm_closes(n=1500):
    """Wild early, calm late: the latest volatility percentile is low."""
    out, p = [], 100.0
    for i in range(n):
        amp = 0.5 if i < n - 300 else 0.001
        p += amp if i % 2 == 0 else -amp
        out.append(round(p, 6))
    return out


def wild_closes(n=1500):
    out, p = [], 100.0
    for i in range(n):
        amp = 0.001 if i < n - 40 else 1.0
        p += amp if i % 2 == 0 else -amp
        out.append(round(p, 6))
    return out


def zd_payload(*, s_rate_pct_per_interval=0.24, apr_short=262.8, venues=("L", "S"), newest_offset_h=0):
    rows = [row("L", rate=0.0, interval=8), row("S", rate=s_rate_pct_per_interval, interval=8)]
    rows = [r for r in rows if r["Exchange"] in venues]
    traces = []
    for v in venues:
        pts = [float(h) for h in range(169)]
        traces.append(trace(v, pts, apr_short if v == "S" else 0.0))
    return response(rows, traces)


class FakeZD:
    def __init__(self, clock, payload=None, error=None):
        self.clock, self.payload, self.error = clock, payload or zd_payload(), error
        self.inner = ZeroDeltaProvider(ZeroDeltaSettings(_env_file=None))
        self.calls = 0

    async def fetch_asset(self, asset, days=None, keep_days=None):
        self.calls += 1
        if self.error:
            raise self.error
        return self.inner.analyze_response(self.payload, asset, days=days, keep_days=keep_days,
                                           received_at=self.clock.now())

    async def aclose(self):
        return None


class Rig:
    def __init__(self, *, zd=None, closes=None, params_kw=None, sources=None, now=NOW):
        self.clock = ManualClock(now)
        self.repo = Repository("sqlite://", self.clock)
        kw = dict(target_notional_per_leg_usd=500, min_expected_net_edge_bps=1, vol_min_observations=200, sweep_delay_seconds=0,
                  rv_window_minutes=30, basis_vol_window_minutes=30, vol_percentile_lookback_days=1,
                  shadow_paper_enabled=False)
        kw.update(params_kw or {})
        self.params = TradingSettings(_env_file=None, **kw).resolve()
        self.src = sources or {"L": FakeSource("L", 100, clock=self.clock, rate_per_hour="0"),
                               "S": FakeSource("S", 100, clock=self.clock, rate_per_hour="0.0003")}
        for s in self.src.values():
            s.candle_closes = closes if closes is not None else calm_closes()
        self.kill = KillSwitch(self.clock)
        self.notifier = Notifier(self.repo)
        provider = PaperVenueProvider(self.src.get, self.clock, self.repo)
        self.engine = TradeEngine(params=self.params, repo=self.repo, clock=self.clock, killswitch=self.kill,
                                  notifier=self.notifier, provider=provider)
        self.zd = zd if zd is not None else FakeZD(self.clock)
        self.scanner = Scanner(params=self.params, zd_settings=ZeroDeltaSettings(_env_file=None), zd=self.zd,
                               source_for_venue=self.src.get, repo=self.repo, engine=self.engine,
                               notifier=self.notifier, clock=self.clock)

    def scan(self, asset="BTC"):
        return run(self.scanner.scan_asset(asset))

    def alerts(self, kind):
        return self.repo.alerts(kind)


def test_a_good_candidate_is_verified_and_opened():
    r = Rig()
    rep = r.scan()
    assert rep.pairs_found == 1 and rep.executable_pairs == 1 and rep.verified == 1
    assert rep.opened == ["arb0001"] and rep.outcomes == {"ENTER": 1}
    t = r.repo.get_trade("arb0001")
    assert t.state == "ENTERING" and t.long_venue == "L" and t.short_venue == "S"
    assert t.context["decision"]["outcome"] == "ENTER" and "zero_delta" in t.context  # audit trail (section 43)
    assert r.alerts("candidate_found") and r.alerts("entry_started")
    from app.persistence.models import StrategyDecision, ZeroDeltaMarket, ZeroDeltaObservation
    with r.repo.session() as s:
        assert s.query(StrategyDecision).count() == 2  # the ENTER check list plus the ENTERED record
        assert s.query(ZeroDeltaMarket).count() == 2
        assert s.query(ZeroDeltaObservation).count() == 2 * 169  # raw Zero Delta history kept for the trade


def test_the_same_pair_is_not_opened_twice():
    r = Rig()
    r.scan()
    rep = r.scan()
    assert rep.opened == [] and len(r.repo.all_trades()) == 1


def test_zero_delta_auth_failure_stops_new_trades_and_alerts_once():
    r = Rig(zd=FakeZD(ManualClock(NOW), error=ZeroDeltaAuthError(401)))
    for _ in range(3):
        rep = r.scan()
    assert not rep.zero_delta_ok and r.repo.all_trades() == []
    assert len(r.alerts("zero_delta_auth_failure")) == 1  # not repeated every scan


def test_zero_delta_outage_alerts_and_recovers():
    zd = FakeZD(ManualClock(NOW), error=ZeroDeltaServerError(503))
    r = Rig(zd=zd)
    zd.clock = r.clock
    r.scan()
    assert r.alerts("zero_delta_outage")
    zd.error = None
    assert r.scan().opened == ["arb0001"]  # normal service resumes without a restart


def test_zero_delta_stale_history_blocks_entry():
    r = Rig(now=NOW + timedelta(hours=10))  # newest chart point is 11 hours old
    rep = r.scan()
    assert not rep.zero_delta_ok and r.alerts("zero_delta_stale_data") and r.repo.all_trades() == []


def test_zero_delta_outage_never_stops_an_open_trade():
    """project.md section 38: the engine works from the venues alone."""
    zd = FakeZD(ManualClock(NOW))
    r = Rig(zd=zd)
    zd.clock = r.clock
    r.scan()
    zd.error = ZeroDeltaAuthError(403)  # from now on Zero Delta is gone
    assert not r.scan().zero_delta_ok
    a = r.engine.active["arb0001"]
    for _ in range(8):
        r.clock.advance(minutes=75)
        run(r.engine.run_once())
        if r.repo.get_trade("arb0001").state == "HEDGED_OPEN":
            break
    assert r.repo.get_trade("arb0001").state == "HEDGED_OPEN"
    r.kill.flatten("op", "test")  # and it can still be closed
    for _ in range(90):
        r.clock.advance(seconds=1)
        run(r.engine.run_once())
        if r.repo.get_trade("arb0001").state == "CLOSED":
            break
    assert r.repo.get_trade("arb0001").state == "CLOSED"


def test_zero_delta_vs_venue_disagreement_is_a_mismatch_and_blocks_the_trade():
    r = Rig()
    r.src["S"].rate_per_hour = D("0.0010")  # the venue says 0.10 %/h, Zero Delta said 0.03 %/h
    rep = r.scan()
    assert rep.outcomes == {"EXCHANGE_MISMATCH": 1} and rep.opened == []
    assert r.alerts("direct_venue_mismatch") and r.repo.all_trades() == []


def test_high_volatility_waits_and_alerts_without_rejecting_forever():
    r = Rig(closes=wild_closes())
    rep = r.scan()
    assert rep.outcomes == {"WAIT": 1} and rep.opened == []
    assert r.alerts("waiting_for_low_volatility")
    for s in r.src.values():  # the market calms down: the next scan may enter
        s.candle_closes = calm_closes()
    assert r.scan().opened == ["arb0001"]


def test_missing_candles_wait_instead_of_guessing():
    r = Rig(closes=[])
    assert r.scan().outcomes == {"WAIT": 1}


def test_thin_edge_is_rejected_by_the_net_edge_gate():
    r = Rig(params_kw=dict(min_expected_net_edge_bps=5000))
    rep = r.scan()
    assert rep.outcomes == {"REJECT": 1} and rep.opened == []
    with r.repo.session() as s:
        from app.persistence.models import StrategyDecision
        d = s.query(StrategyDecision).first()
        failed = [c["name"] for c in d.checks if not c["passed"]]
        assert failed == ["expected_net_edge"]  # every check is recorded so the rejection is explainable


def test_pairs_on_unsupported_venues_are_skipped_not_traded():
    r = Rig()
    r.src.pop("S")  # no executable adapter for the short venue
    rep = r.scan()
    assert rep.pairs_found == 1 and rep.executable_pairs == 0 and rep.opened == []


def test_kill_switch_and_limits_stop_new_entries():
    r = Rig()
    r.kill.pause("op", "maintenance")
    rep = r.scan()
    assert rep.outcomes == {"ENTER": 1} and rep.opened == []  # verified, but not allowed to open
    r.kill.resume("op", "done")
    r2 = Rig(params_kw=dict(max_concurrent_trades=1))
    r2.scan()
    r2.zd.payload = zd_payload()
    assert r2.scan().opened == []


def test_manual_intervention_block_stops_new_entries():
    r = Rig()
    r.repo.set_kv("block_new_trades", {"reason": "arb0009 needs a human"})
    assert r.scan().opened == []


def test_venue_read_failure_skips_the_pair_safely():
    r = Rig()

    async def broken(*a, **k):
        from app.exchanges.base import ExchangeRequestError
        raise ExchangeRequestError("boom")

    r.src["L"].get_order_book = broken
    rep = r.scan()
    assert rep.verified == 0 and rep.opened == []


@pytest.mark.parametrize("min_edge,outcome", [(1, "ENTER"), (5000, "REJECT")])
def test_venue_health_recovers_after_timeout_even_if_candidate_is_rejected(min_edge, outcome):
    r = Rig(params_kw=dict(min_expected_net_edge_bps=min_edge))
    health = r.scanner.health = Health(r.clock)
    original = r.src["L"].get_funding_info

    async def timed_out(*args, **kwargs):
        from app.exchanges.base import ExchangeRequestError
        raise ExchangeRequestError("funding request timed out")

    r.src["L"].get_funding_info = timed_out
    assert r.scan().verified == 0
    assert not health.snapshot()["venue:L/S"]["ok"]
    r.clock.advance(seconds=10)
    r.src["L"].get_funding_info = original
    assert r.scan().outcomes == {outcome: 1}
    assert health.snapshot()["venue:L/S"]["ok"]
    assert health.snapshot()["venue:L/S"]["age_seconds"] == 0
    assert health.all_ok


def test_pairs_with_both_legs_on_one_exchange_are_skipped():
    r = Rig()
    shared = r.src["L"]
    r.src["S"] = shared  # the same adapter serves both venue names, like Binance USDT and Binance USDC
    rep = r.scan()
    assert rep.pairs_found == 1 and rep.executable_pairs == 0 and rep.opened == []
