"""Dashboard API: read-only JSON built from a real scanner run and a real trade."""

import asyncio
from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.bootstrap import Runtime
from app.core.health import Health
from app.core.trading_config import TradingSettings
from tests.execution.test_scanner import Rig, wild_closes

TOKEN = "dash-secret-token"


def make(rig: Rig, **settings_kw):
    settings = TradingSettings(_env_file=None, admin_api_token=TOKEN, **settings_kw)
    rt = Runtime(settings, rig.params, rig.repo, rig.clock, rig.kill, rig.notifier, Health(rig.clock), rig.engine,
                 rig.scanner, SimpleNamespace(), rig.zd)
    return TestClient(create_app(rt, start_runner=False)), rt


def test_page_is_served_and_is_self_contained():
    c, _ = make(Rig())
    r = c.get("/dashboard")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert "no-store" in r.headers["cache-control"]
    html = r.text
    assert "Net profit over time" in html and "Time to enter" in html and "Timing" in html
    assert "http://" not in html.replace("http://127", "") and "https://" not in html  # no external resources
    assert "<script src" not in html and "<link" not in html
    assert c.get("/").json()["links"]["dashboard"] == "/dashboard"


def test_overview_before_any_scan():
    c, rt = make(Rig())
    o = c.get("/api/overview").json()
    assert o["mode"] == "paper" and o["live_trading_enabled"] is False
    assert o["scans"] == {} and o["open_trades"] == 0 and o["top_blockers"] == []
    assert o["locked"]["emergency_exit_ratio"] == "0.10" and "target_leverage" in o["paper_defaults_in_use"]
    assert o["database_ok"] is True and TOKEN not in c.get("/api/overview").text


def test_overview_after_a_rejecting_scan_explains_why():
    rig = Rig(closes=wild_closes())
    rig.scan()
    c, _ = make(rig)
    o = c.get("/api/overview").json()
    s = o["scans"]["BTC"]
    assert s["pairs"] == 1 and s["executable_pairs"] == 1 and s["verified"] == 1 and s["zero_delta_ok"]
    assert s["outcomes"] == {"WAIT": 1} and s["eligible_markets"] == 2 and s["markets"] == 2
    assert o["top_blockers"][0]["check"] == "low_vol_score"
    assert "volatile" in o["top_blockers"][0]["plain"].lower()

    d = c.get("/api/decisions").json()
    assert d[0]["outcome"] == "WAIT" and d[0]["failed"] == ["low_vol_score"] and d[0]["failed_plain"]
    assert any(ch["name"] == "low_vol_score" and not ch["passed"] for ch in d[0]["checks"])

    opp = c.get("/api/opportunities").json()["pairs"][0]
    assert opp["asset"] == "BTC" and opp["long"] == "L" and opp["short"] == "S" and opp["executable"] is True
    assert opp["mean_apr"] == pytest.approx(262.8, rel=1e-3)  # 0.03 %/h x 8760 h
    assert opp["positive_ratio"] == 1.0


def test_trade_lifecycle_appears_in_the_api():
    rig = Rig()
    rig.scan()
    c, _ = make(rig)
    t = c.get("/api/trades").json()
    assert len(t) == 1 and t[0]["state"] == "ENTERING" and t[0]["asset"] == "BTC"
    assert t[0]["entry_slices"] == "0/5"

    asyncio.run(rig.engine.run_once())  # first slice
    rig.clock.advance(minutes=75)
    asyncio.run(rig.engine.run_once())
    d = c.get("/api/trades/arb0001").json()
    assert d["state"] == "ENTERING" and d["entry_plan"]["slices"] == 5
    assert len([s for s in d["slices"] if s["phase"] == "entry"]) >= 2
    assert len(d["orders"]) >= 4 and all(o["price"] for o in d["orders"])
    assert len(d["equity"]) >= 2 and d["equity"][0]["long_ratio"] is not None
    assert d["decision"]["outcome"] == "ENTER"
    assert d["timeline"][-1]["detail"]["to"] == "ENTERING"

    rig.kill.flatten("op", "test")  # close it and check the P&L section fills in
    for _ in range(120):
        rig.clock.advance(seconds=2)
        asyncio.run(rig.engine.run_once())
        if rig.repo.get_trade("arb0001").state == "CLOSED":
            break
    d = c.get("/api/trades/arb0001").json()
    assert d["state"] == "CLOSED" and d["pnl"]["total_realized_pnl"] is not None
    assert d["exit"]["mode"] == "emergency" and d["risk_events"][0]["action"] == "EMERGENCY_EXIT"
    o = c.get("/api/overview").json()
    assert o["closed_trades"] == 1 and o["open_trades"] == 0 and o["closed_pnl_usd"] != "0"
    kinds = {a["kind"] for a in c.get("/api/alerts").json()}
    assert {"entry_started", "emergency_exit_10pct", "trade_closed", "final_pnl"} <= kinds


def test_unknown_trade_is_404_and_limits_are_clamped():
    c, _ = make(Rig())
    assert c.get("/api/trades/nope").status_code == 404
    assert c.get("/api/decisions?limit=100000").status_code == 200
    assert c.get("/api/alerts?limit=-5").status_code == 200


def test_entry_explanation_keeps_shadow_override_and_frozen_funding():
    rig = Rig(params_kw={"shadow_paper_enabled": True, "min_expected_net_edge_bps": 5000})
    rig.scan()
    c, rt = make(rig)
    d = c.get("/api/trades/arb0001").json()
    assert d["shadow"] is True
    waived = next(ch for ch in d["decision"]["checks"] if ch["name"] == "expected_net_edge")
    assert waived["passed"] is False and waived["blocking"] == "info"
    snapshot = d["entry_snapshot"]
    assert snapshot["min_expected_net_edge_bps"] == 5000
    assert snapshot["low_vol_max_percentile"] == 45
    assert snapshot["mean_funding_apr_pct"] == pytest.approx(262.8)
    assert snapshot["funding"][1]["interval_hours"] == 8
    assert snapshot["funding"][1]["rate_pct"] == pytest.approx(0.24)
    rig.src["S"].rate_per_hour = D("0.0009")
    rt.params = rt.params.model_copy(update={"min_expected_net_edge_bps": D(10)})
    rig.scanner.last_selection = {}
    assert c.get("/api/trades/arb0001").json()["entry_snapshot"] == snapshot
    rig.repo.update_trade("arb0001", context={})
    missing = c.get("/api/trades/arb0001").json()
    assert missing["decision"] is None
    assert missing["entry_snapshot"]["min_expected_net_edge_bps"] is None
    assert all(f["rate_pct"] is None for f in missing["entry_snapshot"]["funding"])


def test_14_day_average_uses_saved_history_and_original_trade_direction():
    from app.core.clock import ManualClock
    from app.persistence.models import ZeroDeltaObservation
    from tests.execution.test_scanner import FakeZD
    from tests.providers.helpers import T0, response, row, trace

    now = T0 + timedelta(hours=337)
    payload = response([row("L", rate=0), row("S", rate=0.24)], [
        trace("L", list(range(337)), 87.6),
        trace("S", list(range(337)), [-438.0] * 168 + [350.4] * 169),
    ])
    rig = Rig(zd=FakeZD(ManualClock(now), payload), now=now)
    rig.scan()
    c, _ = make(rig)
    snapshot = c.get("/api/trades/arb0001").json()["entry_snapshot"]
    avg = snapshot["average_funding_14d"]
    assert avg["available"] is True and avg["reason"] is None
    assert avg["start"] == T0.isoformat()
    assert avg["end"] == (T0 + timedelta(days=14)).isoformat()
    assert [leg["samples"] for leg in avg["legs"]] == [337, 337]
    assert avg["legs"][0]["mean_apr_pct"] == pytest.approx(87.6)
    assert avg["legs"][1]["mean_apr_pct"] == pytest.approx((-438 * 168 + 350.4 * 169) / 337)
    expected_net = (-525.6 * 168 + 262.8 * 169) / 337
    assert avg["net_apr_pct"] == pytest.approx(expected_net)
    assert avg["net_rate_per_hour_pct"] == pytest.approx(expected_net / 8760)
    assert avg["net_apr_pct"] < 0 < snapshot["mean_funding_apr_pct"]  # do not flip the original legs
    rig.clock.advance(days=21)
    assert c.get("/api/trades/arb0001").json()["entry_snapshot"]["average_funding_14d"] == avg
    # A missing internal hour cannot be disguised as complete history by its endpoints.
    with rig.repo.session() as session:
        session.query(ZeroDeltaObservation).filter_by(trade_id="arb0001", venue_name="S",
                                                      timestamp=T0 + timedelta(hours=150)).delete()
    gap = c.get("/api/trades/arb0001").json()["entry_snapshot"]["average_funding_14d"]
    assert gap["available"] is False and gap["net_apr_pct"] is None
    assert gap["legs"][1]["samples"] == 336 and gap["legs"][1]["mean_apr_pct"] is None


def test_14_day_average_does_not_label_seven_days_as_fourteen():
    rig = Rig()
    rig.scan()
    c, _ = make(rig)
    avg = c.get("/api/trades/arb0001").json()["entry_snapshot"]["average_funding_14d"]
    assert avg["available"] is False and avg["net_apr_pct"] is None
    assert [leg["samples"] for leg in avg["legs"]] == [169, 169]
    assert all(leg["mean_apr_pct"] is None for leg in avg["legs"])


def test_14_day_average_deduplicates_identical_points_but_rejects_conflicting_rates():
    from app.api.dashboard import _funding_average_14d
    from tests.providers.helpers import T0

    end = T0 + timedelta(days=14)
    trade = SimpleNamespace(long_venue="L", short_venue="S", context={"zero_delta": {"window_end": end.isoformat()}})
    points = [SimpleNamespace(venue_name=venue, timestamp=T0 + timedelta(hours=i),
                              normalized_funding_rate_per_hour=D(rate))
              for venue, rate in (("L", "-0.01"), ("S", "0.02")) for i in range(337)]
    points.append(points[0])
    points.append(SimpleNamespace(venue_name="S", timestamp=end + timedelta(hours=1),
                                  normalized_funding_rate_per_hour=D(99)))
    avg = _funding_average_14d(trade, points)
    assert avg["available"] and avg["net_rate_per_hour_pct"] == pytest.approx(0.03)
    assert avg["legs"][0]["samples"] == 337
    points.append(SimpleNamespace(venue_name="L", timestamp=T0, normalized_funding_rate_per_hour=D(99)))
    conflicting = _funding_average_14d(trade, points)
    assert not conflicting["available"] and conflicting["net_apr_pct"] is None


def test_dashboard_endpoints_are_read_only():
    c, rt = make(Rig())
    for path in ("/api/overview", "/api/decisions", "/api/opportunities", "/api/alerts", "/api/trades"):
        assert c.post(path).status_code == 405 and c.delete(path).status_code == 405
    assert rt.kill.entries_allowed  # nothing changed


# ---- performance view ------------------------------------------------------------------------

def run_to_hedged(rig, steps=7):
    for _ in range(steps):
        rig.clock.advance(minutes=75)
        asyncio.run(rig.engine.run_once())


def close_trade(rig):
    rig.kill.flatten("op", "test")
    for _ in range(150):
        rig.clock.advance(seconds=2)
        asyncio.run(rig.engine.run_once())
        if rig.repo.get_trade("arb0001").state == "CLOSED":
            return
    raise AssertionError("trade did not close")


def test_performance_before_any_trade_is_empty_not_broken():
    c, _ = make(Rig())
    p = c.get("/api/performance").json()
    s = p["summary"]
    assert s["closed_trades"] == 0 and s["open_trades"] == 0 and s["win_rate"] is None
    assert s["net_pnl_total"] == 0 and p["cumulative"] == [] and p["per_trade"] == []


def test_open_trade_profit_matches_the_account_equity_change():
    """Two independent calculations must agree: the fill/funding ledger and the change in account equity."""
    rig = Rig()
    rig.scan()
    c, _ = make(rig)
    run_to_hedged(rig)
    rig.src["S"].rate_per_hour = D("0.0003")
    for _ in range(10):
        rig.clock.advance(minutes=60)
        asyncio.run(rig.engine.run_once())
    rig.src["S"].mid, rig.src["L"].mid = D(101), D("101.4")  # the venues drift apart a little
    rig.clock.advance(minutes=1)
    asyncio.run(rig.engine.run_once())

    d = c.get("/api/trades/arb0001").json()
    assert d["state"] == "HEDGED_OPEN" and d["money"]["live"] is True
    assert d["money"]["net"] == pytest.approx(d["pnl_series"][-1]["pnl"], abs=1e-6)
    m = d["money"]
    assert m["net"] == pytest.approx(m["funding"] + m["price"] - m["fees"], abs=1e-9)
    assert m["funding"] > 0 and m["fees"] > 0
    perf = c.get("/api/performance").json()
    assert perf["summary"]["unrealized_pnl_open"] == pytest.approx(m["net"], abs=1e-9)
    assert perf["cumulative"][-1]["live"] is True  # the curve ends at "now" for an open trade


def test_closed_trade_profit_matches_equity_and_the_summary_adds_up():
    rig = Rig()
    rig.scan()
    c, _ = make(rig)
    run_to_hedged(rig)
    rig.src["S"].mid, rig.src["L"].mid = D(101), D("101.4")
    close_trade(rig)

    d = c.get("/api/trades/arb0001").json()
    assert d["money"]["live"] is False
    assert d["money"]["net"] == pytest.approx(d["pnl_series"][-1]["pnl"], abs=1e-6)  # final point is after the last order
    s = c.get("/api/performance").json()["summary"]
    assert s["closed_trades"] == 1 and s["open_trades"] == 0 and s["unrealized_pnl_open"] is None
    assert s["net_pnl_closed"] == pytest.approx(d["money"]["net"])
    assert s["net_pnl_total"] == pytest.approx(s["funding_total"] + s["price_pnl_total"] - s["fees_total"], abs=1e-6)
    assert s["win_rate"] in (0.0, 1.0) and s["best_trade"]["id"] == "arb0001"
    p = c.get("/api/performance").json()
    assert p["cumulative"][-1]["pnl"] == pytest.approx(s["net_pnl_closed"])
    assert p["per_trade"][0]["id"] == "arb0001" and p["per_trade"][0]["live"] is False


def test_timing_says_how_long_entry_hold_and_exit_took():
    rig = Rig()
    rig.scan()
    c, _ = make(rig)
    t = c.get("/api/trades").json()[0]["timing"]
    assert t["entry_done"] is False and t["hold_seconds"] is None  # still opening
    run_to_hedged(rig)
    t = c.get("/api/trades").json()[0]["timing"]
    assert t["entry_done"] is True
    # 5 slices spaced 72 minutes apart: the last one is due 4 x 72 = 288 min after the first
    assert 280 * 60 <= t["entry_seconds"] <= 400 * 60  # the test steps every 75 min, so it is noticed at the next step
    assert t["hold_seconds"] is not None and t["exit_seconds"] is None
    close_trade(rig)
    row = c.get("/api/trades").json()[0]
    t = row["timing"]
    assert t["exit_seconds"] is not None and t["exit_seconds"] < 300  # emergency exit: about a minute or two
    assert t["total_seconds"] == pytest.approx(t["entry_seconds"] + t["hold_seconds"] + t["exit_seconds"], abs=1)
    assert row["return_pct"] is not None and row["annualized_return_pct"] is not None


def test_trade_detail_reports_entry_and_exit_prices_per_leg():
    rig = Rig()
    rig.scan()
    c, _ = make(rig)
    run_to_hedged(rig)
    close_trade(rig)
    pr = c.get("/api/trades/arb0001").json()["prices"]
    assert pr["long_entry"]["avg_price"] == pytest.approx(100.01, abs=0.05)  # bought at the ask
    assert pr["short_entry"]["avg_price"] == pytest.approx(99.99, abs=0.05)  # sold at the bid
    assert pr["long_exit"]["quantity"] == pytest.approx(pr["long_entry"]["quantity"], rel=1e-6)  # fully closed


# ---- the plan: selection and simulation -------------------------------------------------------

def test_selection_endpoint_shows_picks_exchanges_and_simulation():
    from tests.execution.test_selection_flow import make_rig
    rig = make_rig(selection_count=3)
    rig.scan()
    c, _ = make(rig)
    sel = c.get("/api/selection").json()
    assert sel["count"] == 3 and sel["pairs_considered"] == 6
    top = sel["overall"][0]
    assert (top["long"], top["short"], top["tradable"]) == ("A", "X", False) and top["why_not"]
    assert sel["tradable"][0]["short"] == "C" and sel["tradable"][0]["simulation"]["net"] is not None
    assert len(sel["tradable"][0]["simulation"]["curve"]) > 50
    assert TOKEN not in c.get("/api/selection").text


def test_selection_survives_a_restart_from_the_database():
    from tests.execution.test_selection_flow import make_rig
    rig = make_rig(selection_count=2)
    rig.scan()
    c, rt = make(rig)
    rt.scanner.last_selection = {}  # a fresh process has nothing in memory
    sel = c.get("/api/selection").json()
    assert sel["from_database"] is True and len(sel["overall"]) == 2 and len(sel["tradable"]) == 2
    assert sel["overall"][0]["short"] == "X" and sel["overall"][0]["simulation"]["hours"] == 168


def test_selection_is_empty_before_the_first_scan_and_the_page_has_the_plan_section():
    c, _ = make(Rig())
    assert c.get("/api/selection").json() == {}
    html = c.get("/dashboard").text
    assert "identify, select, simulate" in html and "/api/selection" in html and "renderPlan" in html
