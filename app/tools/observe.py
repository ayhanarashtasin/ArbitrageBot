"""Read-only paper monitoring and ledger audit through the running API; never builds a trading runtime.

    python -m app.tools.observe --audit
    python -m app.tools.observe --hours 24 --interval-seconds 60 --output logs/paper-observation.jsonl --audit
"""

import argparse
import json
import math
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx

from app.accounting.pnl import FundingRecord, OrderRecord, compute_pnl

D = Decimal


def audit_trade(detail: dict) -> dict:
    """Rebuild P&L from saved fills/payments, independent of the reported totals. Slippage is not charged twice."""
    orders = [OrderRecord(o["leg"], o["side"], D(str(o["qty"])), D(str(o["price"])), D(str(o["fee_usd"])))
              for o in detail["orders"] if o["qty"] and o["price"] is not None]
    funding = [FundingRecord("", D(str(f["payment_usd"])), D(str(f["payment_usd"])))
               for f in detail["funding"] if f["payment_usd"] is not None]
    marks = {role: D(str(value["price"])) for role, value in detail.get("valuation_marks", {}).items()}
    pnl = compute_pnl(orders, funding, marks)
    issues = []
    for label, keys in (("duplicate_order_ids", [o["id"] for o in detail["orders"]]),
                        ("duplicate_funding_payments", [(f["venue"], f["at"]) for f in detail["funding"]])):
        if len(keys) != len(set(keys)):
            issues.append(label)
    money = detail["money"]
    for label, computed in (("fees", pnl.fees), ("funding", pnl.funding_net), ("net", pnl.total)):
        if label == "net" and not pnl.complete:
            continue
        reported = money.get(label)
        if reported is not None and abs(computed - D(str(reported))) > D("0.00001"):
            issues.append(f"{label}: ledger={computed} reported={reported}")
    return {"id": detail["id"], "asset": detail["asset"], "shadow": detail["shadow"],
            "status": "failed" if issues else "ok" if pnl.complete and money.get("net") is not None else "partial",
            "issues": issues, "ledger_net": str(pnl.total) if pnl.complete else None,
            "funding": str(pnl.funding_net), "fees": str(pnl.fees),
            "economics": detail.get("economics", {})}


def get(client: httpx.Client, path: str):
    response = client.get(path)
    response.raise_for_status()
    return response.json()


def sample(client: httpx.Client) -> dict:
    health = get(client, "/health")
    overview = get(client, "/api/overview")
    performance = get(client, "/api/performance")
    trades = get(client, "/state")["active_trades"]
    discovery = overview.get("discovery", {})
    failures = [{"asset": row["asset"], "venue": market["venue"], "reasons": market["reasons"],
                 "missing_intervals": market.get("coverage", {}).get("missing_intervals")}
                for row in discovery.get("screening", []) for market in row.get("validation", [])
                if not market.get("eligible", True)]
    return {"at": datetime.now(UTC).isoformat(), "event": "sample", "health": health,
            "uptime_seconds": overview["uptime_seconds"], "database_backlog": overview["database_backlog"],
            "metrics": overview.get("runtime_metrics", {}),
            "scan": {k: overview.get("sweep", {}).get(k) for k in
                      ("started_at", "finished_at", "duration_seconds", "partial", "market_query_timeouts", "aborted")},
            "discovery": {k: discovery.get(k) for k in
                          ("source", "as_of", "summary_rows", "validated_pairs", "summary_refresh_verified")},
            "invalid_markets": failures,
            "performance": performance["summary"], "groups": performance.get("groups", {}), "trades": trades}


def performance_change(first, last) -> dict:
    """Change during observation, not the cumulative lifetime P&L labelled as a daily return."""
    keys = ("closed_trades", "net_pnl_closed", "net_pnl_total", "funding_total", "fees_total", "price_pnl_total")
    return {k: last[k] - first[k] for k in keys if first.get(k) is not None and last.get(k) is not None}


def main(argv=None, *, transport=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--hours", type=float, default=0, help="0 takes one sample")
    parser.add_argument("--interval-seconds", type=float, default=60)
    parser.add_argument("--output", type=Path, help="append JSON lines to an existing directory")
    parser.add_argument("--audit", action="store_true", help="audit all saved trades at start, periodically and at completion")
    parser.add_argument("--audit-interval-seconds", type=float, default=3600)
    args = parser.parse_args(argv)
    if (not all(math.isfinite(n) for n in (args.hours, args.interval_seconds, args.audit_interval_seconds))
            or args.hours < 0 or min(args.interval_seconds, args.audit_interval_seconds) <= 0):
        parser.error("hours must be finite and nonnegative; sample/audit intervals must be finite and positive")
    if args.output and not args.output.parent.is_dir():
        parser.error("the output directory must already exist")

    def emit(record):
        line = json.dumps(record, ensure_ascii=False, default=str)
        if args.output:
            with args.output.open("a", encoding="utf-8") as file:
                file.write(line + "\n")
        else:
            print(line, flush=True)

    count, unhealthy, errors, audit_failed, audits = 0, 0, 0, False, 0
    started_at = datetime.now(UTC).isoformat()
    first_performance, last_performance = None, None
    next_audit = 0.0
    deadline = time.monotonic() + args.hours * 3600
    with httpx.Client(base_url=args.base_url, timeout=30, transport=transport) as client:
        def audit():
            nonlocal audit_failed, errors, audits, next_audit
            try:
                results = [audit_trade(get(client, f"/api/trades/{t['id']}")) for t in get(client, "/api/trades")]
                audit_failed |= any(r["status"] == "failed" for r in results)
                audits += 1
                emit({"at": datetime.now(UTC).isoformat(), "event": "audit", "trades": results})
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                errors += 1
                emit({"at": datetime.now(UTC).isoformat(), "event": "audit_error", "error": type(exc).__name__})
            next_audit = time.monotonic() + args.audit_interval_seconds

        while True:
            if args.audit and time.monotonic() >= next_audit:
                audit()
            try:
                record = sample(client)
                count += 1
                unhealthy += not record["health"]["ok"]
                last_performance = record["performance"]
                if first_performance is None:
                    first_performance = last_performance
                emit(record)
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                errors += 1
                emit({"at": datetime.now(UTC).isoformat(), "event": "sample_error", "error": type(exc).__name__})
            remaining = deadline - time.monotonic()
            if args.hours == 0 or remaining <= 0:
                break
            time.sleep(min(args.interval_seconds, remaining))
        if args.audit and args.hours > 0:
            audit()
    emit({"at": datetime.now(UTC).isoformat(), "event": "complete", "samples": count,
          "started_at": started_at, "unhealthy_samples": unhealthy, "errors": errors,
          "audits": audits, "audit_failed": audit_failed,
          "performance_change": performance_change(first_performance, last_performance)
              if first_performance is not None else None})
    return 1 if errors or unhealthy or audit_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
