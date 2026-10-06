"""Independent cashflow audit: catches bookkeeping errors rather than trusting reported P&L."""

from copy import deepcopy

import httpx

from app.tools import observe
from app.tools.observe import audit_trade


def trade():
    return {"id": "paper1", "asset": "BTC", "shadow": True,
            "orders": [
                {"id": "entry-L", "leg": "long", "side": "BUY", "qty": 2, "price": 100, "fee_usd": .1},
                {"id": "entry-S", "leg": "short", "side": "SELL", "qty": 2, "price": 100, "fee_usd": .1},
                {"id": "exit-L", "leg": "long", "side": "SELL", "qty": 2, "price": 101, "fee_usd": .1},
                {"id": "exit-S", "leg": "short", "side": "BUY", "qty": 2, "price": 101, "fee_usd": .1}],
            "funding": [{"venue": "S", "at": "settlement1", "payment_usd": .6}],
            "money": {"funding": .6, "fees": .4, "net": .2}}


def test_closed_ledger_reconciles_and_detects_a_wrong_total():
    t = trade()
    assert audit_trade(t)["status"] == "ok"
    t["money"]["net"] = 1.2
    result = audit_trade(t)
    assert result["status"] == "failed" and result["issues"][0].startswith("net:")


def test_duplicate_funding_is_detected_even_if_the_reported_total_includes_it():
    t = trade()
    t["funding"].append(deepcopy(t["funding"][0]))
    t["money"].update(funding=1.2, net=.8)
    assert "duplicate_funding_payments" in audit_trade(t)["issues"]


def test_open_legs_need_prices_before_their_total_can_be_audited():
    t = trade()
    t["orders"] = t["orders"][:2]
    t["money"].update(fees=.2, net=.4)
    assert audit_trade(t)["status"] == "partial"
    t["valuation_marks"] = {"long": {"price": 102}, "short": {"price": 102}}
    assert audit_trade(t)["status"] == "ok"
    t["valuation_marks"]["short"]["price"] = 103
    assert audit_trade(t)["status"] == "failed"


def test_long_observation_audits_new_failures_and_reports_only_period_profit(monkeypatch, capsys):
    import json

    ticks = [0.0]
    monkeypatch.setattr(observe.time, "monotonic", lambda: ticks[0])
    monkeypatch.setattr(observe.time, "sleep", lambda seconds: ticks.__setitem__(0, ticks[0] + seconds))
    sample_count = [0]

    def sample(client):
        sample_count[0] += 1
        return {"event": "sample", "health": {"ok": True},
                "performance": {"net_pnl_total": 100 + sample_count[0], "fees_total": sample_count[0]}}

    monkeypatch.setattr(observe, "sample", sample)

    def request(req):
        assert req.method == "GET"
        if req.url.path == "/api/trades":
            return httpx.Response(200, json=[{"id": "paper1"}])
        detail = trade()
        if sample_count[0] >= 1:
            detail["money"]["net"] = 99
        return httpx.Response(200, json=detail)

    result = observe.main(["--hours", "0.001", "--interval-seconds", "2", "--audit",
                           "--audit-interval-seconds", "2"], transport=httpx.MockTransport(request))
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    final = records[-1]
    assert result == 1 and final["audit_failed"]
    assert final["audits"] >= 3 and records[0]["trades"][0]["status"] == "ok"
    assert final["performance_change"] == {"net_pnl_total": 2, "fees_total": 2}
