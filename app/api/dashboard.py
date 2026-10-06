"""Read-only dashboard data (no secrets, no actions). Every function returns plain JSON-safe values.

The page in app/api/static/dashboard.html polls these endpoints. Numbers are shown as the bot stores them;
edges are annualised percent (APR) because that is how people compare funding.
"""

from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.bootstrap import Runtime
from app.domain.trade import TERMINAL, TradeState
from app.persistence.models import as_utc

STARTED_AT = datetime.now(UTC)

# What each rejection reason means in plain words. Unknown reasons fall back to their own name.
PLAIN = {
    "expected_net_edge": "Funding gap too small after fees and slippage",
    "low_vol_score": "Market too volatile right now (waiting for calm)",
    "volatility_score_available": "Not enough price history to judge volatility",
    "negative_spread_within_8h_median_funding": "Price difference exceeds 8 hours of 7-day median funding",
    "net_funding_positive_now": "Current funding difference is not positive",
    "median_funding_positive": "7-day median funding difference is not positive",
    # Keep labels for decisions recorded before the 8-hour median rule.
    "negative_spread_within_2h_funding": "Price difference exceeds the old 2-hour funding limit",
    "net_funding_positive_over_2h": "Funding difference is not positive",
    "long_book_fresh": "Long venue order book too old", "short_book_fresh": "Short venue order book too old",
    "long_book_depth_sufficient": "Long venue book too thin", "short_book_depth_sufficient": "Short venue book too thin",
    "long_depth_multiplier": "Long venue liquidity too low", "short_depth_multiplier": "Short venue liquidity too low",
    "long_slippage": "Long venue slippage too high", "short_slippage": "Short venue slippage too high",
    "long_funding_rate_matches": "Zero Delta and long venue disagree on funding",
    "short_funding_rate_matches": "Zero Delta and short venue disagree on funding",
    "long_funding_interval_matches": "Zero Delta and long venue disagree on funding interval",
    "short_funding_interval_matches": "Zero Delta and short venue disagree on funding interval",
    "zero_delta_data_fresh": "Zero Delta data too old",
    "earnings_blackout": "Stock inside earnings blackout or earnings date unavailable",
    "legs_can_be_sized_equally": "No common coin quantity fits both venues within the child cap",
}


def _f(x: Any) -> float | None:
    return None if x is None else float(x)


def _iso(dt) -> str | None:
    dt = as_utc(dt)
    return None if dt is None else dt.isoformat()


def overview(rt: Runtime) -> dict[str, Any]:
    now = rt.clock.now()
    decisions = rt.repo.recent_decisions(200)
    blockers: Counter = Counter()
    outcomes: Counter = Counter()
    latest_by_asset: dict[str, datetime] = {}
    for d in decisions:
        outcomes[d.outcome] += 1
        for c in d.checks:
            if not c.get("passed", True) and c.get("blocking") != "info":
                blockers[c["name"]] += 1
        latest_by_asset.setdefault(d.asset, as_utc(d.recorded_at))

    scans = {}
    for asset, r in rt.scanner.last_report.items():
        # with hundreds of coins only those that matter get a card: something tradable, checked, opened or failing
        if not (r.executable_pairs or r.verified or r.opened or not r.zero_delta_ok):
            continue
        scans[asset] = {
            "scanned_at": r.scanned_at, "zero_delta_ok": r.zero_delta_ok, "markets": r.total_markets,
            "eligible_markets": r.zero_delta_eligible_markets, "pairs": r.pairs_found,
            "executable_pairs": r.executable_pairs, "verified": r.verified, "outcomes": r.outcomes,
            "opened": r.opened, "note": r.note,
            "market_validation": r.market_validation,
        }
    trades = rt.repo.all_trades()
    open_trades = [t for t in trades if TradeState(t.state) not in TERMINAL]
    closed = [t for t in trades if TradeState(t.state) is TradeState.CLOSED]
    pnl_total = sum((Decimal(t.pnl["total_realized_pnl"]) for t in closed if t.pnl), Decimal(0))
    return {
        "now": now.isoformat(),
        "uptime_seconds": (now - STARTED_AT).total_seconds() if now >= STARTED_AT else 0,
        "mode": rt.params.run_mode,
        "live_trading_enabled": rt.params.run_mode == "live",  # the bot refuses to start in live mode unless gated
        "kill_switch": {"pause_new_entries": rt.kill.pause_new_entries, "emergency_flatten": rt.kill.emergency_flatten},
        "blocked_reason": rt.engine.blocked(),
        "health": rt.health.snapshot(),
        "database_ok": rt.repo.db_ok, "database_backlog": rt.repo.backlog_size,
        "runtime_metrics": {"loops": dict(getattr(rt.runner, "loop_metrics", {})),
                            "public_http": {a.name: a._http.diagnostics() for a in rt.adapters.values()
                                            if hasattr(a, "_http")},
                            "zero_delta_db": rt.zd._postgres.diagnostics()
                                if rt.zd and getattr(rt.zd, "_postgres", None) and hasattr(rt.zd._postgres, "diagnostics")
                                else {}},
        "scan_interval_seconds": rt.params.scan_interval_seconds,
        "data_source": "postgres" if rt.scanner.database_source else "dash",
        "discovery_refresh_minute": rt.scanner.zd_settings.refresh_minute,
        "excluded_venues": sorted(rt.scanner.excluded_venues),
        "discovery": dict(rt.scanner.discovery),
        "assets": rt.settings.scan_assets,
        "scans": dict(sorted(scans.items(), key=lambda kv: (-kv[1]["verified"], -kv[1]["executable_pairs"]))[:24]),
        "sweep": {**rt.scanner.sweep, "progress": rt.scanner.progress},
        "top_blockers": [{"check": k, "plain": PLAIN.get(k, k), "count": v} for k, v in blockers.most_common(8)],
        "decision_outcomes": dict(outcomes),
        "open_trades": len(open_trades), "closed_trades": len(closed),
        "closed_pnl_usd": str(pnl_total),
        "limits": {"max_concurrent_trades": rt.params.max_concurrent_trades,
                   "target_notional_per_leg_usd": str(rt.params.target_notional_per_leg_usd),
                   "leverage": str(rt.params.target_leverage)},
        "locked": {"max_leverage": str(rt.params.max_allowed_leverage),
                    "twap_minutes": rt.params.twap_entry_duration_minutes,
                    "child_usd": str(rt.params.twap_max_child_notional_usd),
                    "low_vol_percentile": str(rt.params.low_vol_max_percentile),
                    "spread_horizon_hours": rt.params.funding_screening_horizon_minutes // 60,
                    "controlled_exit_ratio": str(rt.params.controlled_exit_equity_ratio),
                   "emergency_exit_ratio": str(rt.params.emergency_exit_equity_ratio)},
        "paper_defaults_in_use": sorted(k for k, v in rt.params.provenance.items() if v == "paper_default"),
        "explicit_parameters": sorted(k for k, v in rt.params.provenance.items() if v == "explicit"),
    }


def decisions(rt: Runtime, limit: int = 60) -> list[dict[str, Any]]:
    out = []
    for d in rt.repo.recent_decisions(limit):
        failed = [c["name"] for c in d.checks if not c.get("passed", True) and c.get("blocking") != "info"]
        out.append({
            "at": _iso(d.recorded_at), "asset": d.asset, "long": d.long_venue, "short": d.short_venue,
            "outcome": d.outcome, "failed": failed, "failed_plain": [PLAIN.get(n, n) for n in failed],
            "expected_net_edge_bps": d.metrics.get("expected_net_edge_bps"),
            "low_vol_score": d.metrics.get("low_vol_score"),
            "entry_spread_pct": d.metrics.get("entry_spread_pct"),
            "net_funding_per_hour": d.metrics.get("net_funding_per_hour"),
            "breakeven_hours": d.metrics.get("breakeven_hours"),
            "expected_hold_hours": d.metrics.get("expected_hold_hours"),
            "round_trip_cost_bps": d.metrics.get("round_trip_cost_bps"),
            "income_assumption": d.metrics.get("income_assumption"),
            "trade_id": d.trade_id,
            "checks": [{"name": c["name"], "passed": c["passed"], "value": c["value"], "limit": c["limit"],
                        "blocking": c.get("blocking", "reject")}
                       for c in d.checks],
        })
    return out


def opportunities(rt: Runtime) -> dict[str, Any]:
    """The best pairs across every coin scanned, by 7-day average funding (display order, not a score)."""
    return {"pairs": rt.scanner.last_top_pairs, "source": "postgres" if rt.scanner.database_source else "dash",
            "scope": rt.scanner.discovery.get("scope", "all scanned assets")}


def selection(rt: Runtime) -> dict[str, Any]:
    """Diagnostic replay, out-of-sample research and actual forward trial status, explicitly distinguished."""
    import copy
    out = copy.deepcopy(rt.scanner.last_selection or rt.repo.latest_selection())
    if not out:
        return {}
    trials = rt.repo.all_trades()
    made_at = datetime.fromisoformat(out["at"]) if out.get("at") else None
    for kind in ("overall", "tradable"):
        for row in out.get(kind, []):
            evidence = row.pop("funding_evidence", None)
            if evidence is not None:
                row["evidence_samples"] = {v: len(points) for v, points in evidence.items()}
            matches = [t for t in trials if (t.asset, t.long_venue, t.short_venue) ==
                       (row["asset"], row["long"], row["short"])]
            if matches:
                t = matches[-1]
                trial = {"status": t.state, "trade_id": t.id, "shadow": bool((t.context or {}).get("shadow")),
                         "net": _pnl_numbers(rt, t)["net"]}
                if t.closed_at is not None and made_at is not None and as_utc(t.closed_at) < made_at:
                    # Live 2026-10-02: US was picked again a day after its trial closed and the plan showed that old
                    # trial as the new pick's result. A trial that ended before this selection is only history.
                    row["forward"] = {**(row.get("forward") or {}), "previous_trial": trial}
                    continue
                active = rt.engine.active.get(t.id)
                row["forward"] = {**trial, "entry_wait": active.gate_wait if active else None}
    out["track_record"] = rt.repo.selection_track_record()
    return out


def alerts(rt: Runtime, limit: int = 40) -> list[dict[str, Any]]:
    return [{"at": _iso(a.recorded_at), "kind": a.kind, "severity": a.severity, "message": a.message,
             "trade_id": a.trade_id, "delivered": a.delivered} for a in rt.repo.recent_alerts(limit)]


def _timing(rt: Runtime, t) -> dict[str, Any]:
    """When each phase started, taken from the recorded state transitions (works for live trades too)."""
    at: dict[str, datetime] = {}
    for a in rt.repo.audit_for(t.id):
        if a.kind != "state_transition":
            continue
        to, when = a.data.get("to"), as_utc(a.recorded_at)
        if to == "ENTERING":
            at.setdefault("entering", when)
        elif to == "HEDGED_OPEN":
            at.setdefault("hedged", when)
        elif to in ("EXIT_PENDING", "CONTROLLED_EXIT", "EMERGENCY_EXIT", "EXITING"):
            at.setdefault("exit_start", when)
        elif to == "CLOSED":
            at["closed"] = when
    now = rt.clock.now()
    start = at.get("entering") or as_utc(t.opened_at)
    hedged, exit_start, closed = at.get("hedged"), at.get("exit_start"), at.get("closed")

    def secs(a, b):
        return None if a is None or b is None else max(0.0, (b - a).total_seconds())

    return {
        "entry_started_at": _iso(start), "hedged_at": _iso(hedged), "exit_started_at": _iso(exit_start),
        "closed_at": _iso(closed),
        "entry_seconds": secs(start, hedged or exit_start or closed or now),
        "entry_done": hedged is not None,
        "hold_seconds": secs(hedged, exit_start or closed or now),
        "exit_seconds": secs(exit_start, closed or now),
        "total_seconds": secs(start, closed or now),
    }


def _pnl_numbers(rt: Runtime, t) -> dict[str, Any]:
    """Final numbers for a closed trade, mark-to-market numbers (before closing costs) for an open one."""
    active = rt.engine.active.get(t.id)
    if t.pnl:
        p = t.pnl
        return {"live": False, "net": float(p["total_realized_pnl"]), "funding": float(p["funding_pnl"]),
                "price": float(p["hedge_trading_pnl"]), "fees": float(p["fees"]),
                "slippage": float(p["slippage_estimate"]), "status": "final"}
    if active is not None:
        p, how = rt.engine.live_valuation(active)
        if how["status"] == "unavailable":
            # An unvalued open leg would read as a loss of its whole notional (-$99 on a $0.13 reality): no number.
            return {"live": True, "net": None, "funding": float(p.funding_net), "price": None,
                    "fees": float(p.fees), "slippage": float(p.slippage_estimate), "status": "unavailable",
                    "note": f"no recent price for the {' and '.join(how['unpriced_legs'])} leg (venue unreachable)"}
        out = {"live": True, "net": float(p.total), "funding": float(p.funding_net), "price": float(p.hedge_trading),
               "fees": float(p.fees), "slippage": float(p.slippage_estimate), "status": how["status"]}
        if how["stale_legs_seconds"]:
            out["stale_legs_seconds"] = how["stale_legs_seconds"]
            out["note"] = "valued with last-known prices: " + ", ".join(
                f"{leg} {secs:.0f} s old" for leg, secs in how["stale_legs_seconds"].items())
        return out
    return {"live": True, "net": None, "funding": None, "price": None, "fees": None, "slippage": None,
            "status": "unavailable", "note": "trade not loaded in the engine"}


def trades(rt: Runtime) -> list[dict[str, Any]]:
    live = {s["id"]: s for s in rt.engine.summary()}
    out = []
    for t in reversed(rt.repo.all_trades()):
        timing = _timing(rt, t)
        num = _pnl_numbers(rt, t)
        target = float(t.target_notional_per_leg)
        ret = None if num["net"] is None or target <= 0 else num["net"] / target * 100
        hours = (timing["total_seconds"] or 0) / 3600
        row = {
            "id": t.id, "asset": t.asset, "state": t.state, "long": t.long_venue, "short": t.short_venue,
            "shadow": bool((t.context or {}).get("shadow")),
            "opened_at": _iso(t.opened_at), "closed_at": _iso(t.closed_at),
            "target_per_leg_usd": str(t.target_notional_per_leg), "leverage": str(t.leverage),
            "exit_reason": t.exit_reason, "max_delta_imbalance_usd": str(t.max_delta_imbalance_usd),
            "pnl": t.pnl, "timing": timing, "money": num, "return_pct": ret,
            "annualized_return_pct": None if ret is None or hours < 1 / 60 else ret / (hours / 8760),
        }
        row.update({k: v for k, v in live.get(t.id, {}).items() if k not in row})
        out.append(row)
    return out


def performance(rt: Runtime) -> dict[str, Any]:
    rows = list(reversed(trades(rt)))  # oldest first
    closed = [r for r in rows if r["state"] == "CLOSED" and r["money"]["net"] is not None]
    finished = ("CLOSED", "REJECTED", "DATA_INSUFFICIENT", "EXCHANGE_MISMATCH")
    open_ = [r for r in rows if r["state"] not in finished]
    open_live = [r for r in open_ if r["money"]["net"] is not None]

    def total(key, group):
        return sum(r["money"][key] for r in group)

    def avg(key):
        vals = [r["timing"][key] for r in rows if r["timing"][key] is not None]
        return sum(vals) / len(vals) if vals else None

    wins = [r for r in closed if r["money"]["net"] > 0]
    net_closed, unrealized = total("net", closed), total("net", open_live)
    cumulative, running = [], 0.0
    for r in sorted(closed, key=lambda r: r["closed_at"] or ""):
        running += r["money"]["net"]
        cumulative.append({"at": r["closed_at"], "pnl": running, "trade": r["id"]})
    if open_live:
        cumulative.append({"at": rt.clock.now().isoformat(), "pnl": running + unrealized, "trade": "open", "live": True})
    funding, running_f = [], 0.0
    for f in rt.repo.all_funding_payments():
        if f.actual_payment is not None:
            running_f += float(f.actual_payment)
            funding.append({"at": _iso(f.timestamp), "total": running_f})
    exposure = 0.0
    for active in rt.engine.active.values():
        if active.last_obs and active.last_obs.delta:
            exposure += float(active.last_obs.delta.long_usd + active.last_obs.delta.short_usd)
    notional_closed = sum(float(r["target_per_leg_usd"]) for r in closed)
    best = max(closed, key=lambda r: r["money"]["net"], default=None)
    worst = min(closed, key=lambda r: r["money"]["net"], default=None)
    both = closed + open_live
    groups = {}
    for name, is_shadow in (("shadow_research", True), ("profit_gated", False)):
        valued = [r for r in both if r["shadow"] == is_shadow]
        done = [r for r in closed if r["shadow"] == is_shadow]
        groups[name] = {"trades": sum(r["shadow"] == is_shadow for r in rows), "closed_trades": len(done),
            "net_pnl": total("net", valued), "funding": total("funding", valued), "fees": total("fees", valued),
            "price_pnl": total("price", valued),
            "win_rate": sum(r["money"]["net"] > 0 for r in done) / len(done) if done else None}
    return {
        "groups": groups,
        "summary": {
            "closed_trades": len(closed), "open_trades": len(open_), "wins": len(wins),
            "shadow_trades": sum(r["shadow"] for r in rows),
            "shadow_net_pnl": sum(r["money"]["net"] for r in both if r["shadow"]),
            "standard_net_pnl": sum(r["money"]["net"] for r in both if not r["shadow"]),
            "losses": len(closed) - len(wins), "win_rate": len(wins) / len(closed) if closed else None,
            "net_pnl_closed": net_closed, "unrealized_pnl_open": unrealized if open_live else None,
            "net_pnl_total": net_closed + unrealized,
            "funding_total": total("funding", both), "fees_total": total("fees", both),
            "price_pnl_total": total("price", both),
            "avg_entry_seconds": avg("entry_seconds"),
            "avg_hold_seconds": avg("hold_seconds"), "avg_exit_seconds": avg("exit_seconds"),
            "exposure_open_usd": exposure,
            "return_on_notional_pct": None if not notional_closed else net_closed / notional_closed * 100,
            "best_trade": None if best is None else {"id": best["id"], "net": best["money"]["net"]},
            "worst_trade": None if worst is None else {"id": worst["id"], "net": worst["money"]["net"]},
        },
        "cumulative": cumulative,
        "funding_cumulative": funding,
        "per_trade": [{"id": r["id"], "asset": r["asset"], "live": r["money"]["live"], "state": r["state"],
                       "funding": r["money"]["funding"], "price": r["money"]["price"], "fees": r["money"]["fees"],
                       "net": r["money"]["net"]} for r in rows if r["money"]["net"] is not None],
    }


def _avg_price(orders, leg: str, exit_: bool) -> dict[str, Any] | None:
    picked = [o for o in orders if o.leg == leg and bool(o.reduce_only) == exit_ and o.filled_quantity > 0
              and o.average_fill_price]
    qty = sum((o.filled_quantity for o in picked), Decimal(0))
    if not picked or qty == 0:
        return None
    notional = sum((o.filled_quantity * o.average_fill_price for o in picked), Decimal(0))
    return {"avg_price": float(notional / qty), "quantity": float(qty), "notional_usd": float(notional)}


def _funding_average_14d(t, observations) -> dict[str, Any]:
    """Mean of the saved hourly percent rates; keep the trade's original long/short direction."""
    end = ((t.context or {}).get("zero_delta") or {}).get("window_end")
    out = {"available": False, "start": None, "end": None, "expected_samples_per_leg": 337,
           "legs": [], "net_rate_per_hour_pct": None, "net_apr_pct": None,
           "reason": "The entry-time funding history window was not recorded."}
    if not end:
        return out
    try:
        end = as_utc(datetime.fromisoformat(str(end)))
    except ValueError:
        return out
    start = end - timedelta(days=14)
    out.update(start=_iso(start), end=_iso(end))
    expected = {start + timedelta(hours=i) for i in range(337)}
    means, incomplete = {}, []
    for role in ("long", "short"):
        venue = getattr(t, role + "_venue")
        series, conflicting = {}, False
        for obs in observations:
            at = as_utc(obs.timestamp)
            if obs.venue_name != venue or not start <= at <= end:
                continue
            rate = obs.normalized_funding_rate_per_hour  # percent/hour, not a fraction or settlement rate
            if at in series and series[at] != rate:
                conflicting = True
            series[at] = rate
        complete = series.keys() == expected and not conflicting
        mean = sum(series.values(), Decimal(0)) / len(series) if complete else None
        means[role] = mean
        out["legs"].append({"leg": role, "venue": venue, "samples": len(series),
                            "mean_rate_per_hour_pct": _f(mean),
                            "mean_apr_pct": None if mean is None else float(mean * 8760)})
        if not complete:
            incomplete.append(venue)
    if incomplete:
        out["reason"] = "Missing or inconsistent hourly history for " + ", ".join(incomplete) + "."
        return out
    net = means["short"] - means["long"]
    out.update(available=True, reason=None, net_rate_per_hour_pct=float(net), net_apr_pct=float(net * 8760))
    return out


def _entry_snapshot(t, observations=()) -> dict[str, Any]:
    """Frozen discovery rates and thresholds, never today's selection or configuration."""
    ctx = t.context or {}
    pair, params = ctx.get("zero_delta") or {}, ctx.get("params") or {}
    metrics = (ctx.get("decision") or {}).get("metrics") or {}
    breakeven = metrics.get("breakeven_hours")
    funding = []
    for role in ("long", "short"):
        leg = pair.get(role + "_leg") or {}
        rate, interval = leg.get("current_funding_per_hour"), leg.get("funding_interval_hours")
        funding.append({
            "leg": role, "venue": leg.get("venue_name") or getattr(t, role + "_venue"),
            "interval_hours": _f(interval),
            "rate_pct": None if rate is None or interval is None else float(Decimal(str(rate)) * Decimal(str(interval))),
        })
    mean = (pair.get("stats") or {}).get("mean")
    return {
        "funding": funding, "funding_as_of": pair.get("window_end"),
        "mean_funding_apr_pct": None if mean is None else float(Decimal(str(mean)) * 8760),
        "average_funding_14d": _funding_average_14d(t, observations),
        "selection_metric": params.get("selection_metric"),
        "min_expected_net_edge_bps": _f(params.get("min_expected_net_edge_bps")),
        "low_vol_max_percentile": _f(params.get("low_vol_max_percentile")),
        "expected_hold_hours": _f(params.get("expected_hold_hours")),
        "breakeven_hours": _f(breakeven) if breakeven not in (None, "unreachable") else None,
        "twap_entry_duration_minutes": params.get("twap_entry_duration_minutes"),
    }


def _exposure_hours(orders, context, until) -> dict[str, float | None]:
    """Integrate filled base quantity over real fill times, as hours at the originally planned full quantity."""
    quantities = (context.get("entry_plan") or {}).get("slice_qty") or []
    target = sum((Decimal(q) for q in quantities), Decimal(0))
    if target <= 0:
        return {"long": None, "short": None}
    out = {}
    for leg in ("long", "short"):
        hours = sum(((Decimal(1) if o.side == "BUY" else Decimal(-1)) * o.filled_quantity
            * Decimal(str(max(0, (until - as_utc(o.updated_at)).total_seconds()))) / 3600
            for o in orders if o.leg == leg), Decimal(0))
        out[leg] = float(abs(hours) / target)
    return out


def trade_detail(rt: Runtime, trade_id: str) -> dict[str, Any]:
    t = rt.repo.get_trade(trade_id)
    eq = rt.repo.equity_series(trade_id)
    audit = rt.repo.audit_for(trade_id)
    ctx = t.context or {}
    funding = rt.repo.funding_payments(trade_id)
    orders = rt.repo.orders_for(trade_id)
    active = rt.engine.active.get(trade_id)
    base = (t.baseline_long_equity or 0) + (t.baseline_short_equity or 0)
    pnl_series = [{"at": _iso(e.recorded_at), "pnl": float(e.long_equity + e.short_equity - base)}
                  for e in eq if e.long_equity is not None and e.short_equity is not None and base]
    cum, running = [], 0.0
    for f in funding:
        if f.actual_payment is not None:
            running += float(f.actual_payment)
            cum.append({"at": _iso(f.timestamp), "total": running})
    return {
        "id": t.id, "asset": t.asset, "state": t.state, "long": t.long_venue, "short": t.short_venue,
        "shadow": bool(ctx.get("shadow")),
        "opened_at": _iso(t.opened_at), "closed_at": _iso(t.closed_at), "exit_reason": t.exit_reason,
        "target_per_leg_usd": float(t.target_notional_per_leg), "leverage": float(t.leverage),
        "baseline_long_equity": _f(t.baseline_long_equity), "baseline_short_equity": _f(t.baseline_short_equity),
        "pnl": t.pnl, "money": _pnl_numbers(rt, t), "timing": _timing(rt, t), "pnl_series": pnl_series,
        "valuation_marks": {role: {"price": float(price), "at": _iso(at)}
                            for role, (at, price) in active.last_marks.items()} if active else {},
        "economics": {"forecast": (ctx.get("decision") or {}).get("metrics") or {},
                      "equivalent_full_size_hours": _exposure_hours(orders, ctx, as_utc(t.closed_at) or rt.clock.now()),
                      "group": "shadow_research" if ctx.get("shadow") else "profit_gated"},
        "funding_cumulative": cum,
        "prices": {"long_entry": _avg_price(orders, "long", False), "short_entry": _avg_price(orders, "short", False),
                   "long_exit": _avg_price(orders, "long", True), "short_exit": _avg_price(orders, "short", True)},
        "decision": ctx.get("decision"),
        "entry_snapshot": _entry_snapshot(t, rt.repo.funding_observations(trade_id)),
        "entry_plan": ctx.get("entry_plan"), "entry_stopped": ctx.get("entry_stopped"), "exit": ctx.get("exit"),
        "equity": [{"at": _iso(e.recorded_at), "long_ratio": _f(e.long_ratio), "short_ratio": _f(e.short_ratio),
                    "net_delta_usd": _f(e.net_delta_usd)} for e in eq],
        "slices": [{"phase": s.phase, "index": s.slice_index, "status": s.status,
                    "planned_usd": _f(s.planned_notional), "submit_gap_ms": s.submission_time_difference_ms,
                    "at": _iso(s.long_submit_time)} for s in rt.repo.all_slices(trade_id)],
        "orders": [{"id": o.client_order_id, "leg": o.leg, "side": o.side, "qty": _f(o.filled_quantity),
                    "price": _f(o.average_fill_price), "fee_usd": _f(o.fee_usd), "status": o.status,
                    "reduce_only": o.reduce_only, "at": _iso(o.updated_at)} for o in orders],
        "funding": [{"at": _iso(f.timestamp), "venue": f.venue, "rate": _f(f.rate), "payment_usd": _f(f.actual_payment)}
                    for f in funding],
        "timeline": [{"at": _iso(a.recorded_at), "kind": a.kind, "detail": a.data} for a in audit
                     if a.kind in ("state_transition", "heal_flatten", "trade_created")],
        "risk_events": [{"at": _iso(r.recorded_at), "action": r.action, "priority": r.priority, "reasons": r.reasons}
                        for r in rt.repo.risk_events(trade_id)],
    }
