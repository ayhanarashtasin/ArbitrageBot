"""
7-day funding-edge analytics for every venue pair of one ticker (project.md sections 13-14).

Usage:
    python tools/zerodelta_research/analyze_pairs.py --coin BTC [--top 15] [--include-gap-venues]

Inputs and output live in data/zerodelta/ (inputs from capture_all_venues.py):
    <coin>_all_hourly_apr.csv, <coin>_coverage.csv, <coin>_venues_snapshot.csv
Output:
    <coin>_pair_analytics.csv

Definitions
    window   : trailing 168 hourly timestamps ending at the newest timestamp
    edge     : APR(short venue) - APR(long venue), in APR %; short = venue with the higher 7d mean
    per hour : APR / 8760 (the series are annualized APR, so this is a straight unit conversion)
    A pair needs all 168 timestamps present on both venues, otherwise DATA_INSUFFICIENT.
    Only venues with volume > $5M (strict) and history_ok are paired, unless --include-gap-venues.

All values are Decimal (project.md section 44); nothing is converted to float.

Not computed (Zero Delta chart has no price/basis/score series): basis metrics, provider score/rank.
The table is sorted by 7d mean edge for readability only. No ranking formula is defined in project.md.
"""

import argparse
import csv
import statistics
from decimal import Decimal
from datetime import datetime, timedelta
from itertools import combinations
from pathlib import Path

HERE = Path(__file__).resolve().parents[2] / "data" / "zerodelta"
WINDOW_HOURS = 168
HOURS_PER_YEAR = Decimal(8760)


def load_hourly(path):
    with open(path, newline="", encoding="utf-8") as f:
        r = csv.reader(f)
        header = next(r)
        venues = header[1:]
        stamps, cols = [], {v: [] for v in venues}
        for row in r:
            stamps.append(datetime.fromisoformat(row[0][:19]))
            for v, cell in zip(venues, row[1:]):
                cols[v].append(Decimal(cell) if cell != "" else None)
    return stamps, cols


def sign(x):
    return (x > 0) - (x < 0)


ZERO = Decimal(0)


def pair_metrics(edges):
    """edges: list of APR-% values in time order, oriented so the mean is >= 0."""
    n = len(edges)
    pos = sum(1 for e in edges if e > 0)
    neg = sum(1 for e in edges if e < 0)
    signs = [sign(e) for e in edges if e != 0]
    flips = sum(1 for a, b in zip(signs, signs[1:]) if a != b)
    best = run = 0
    for e in edges:
        run = run + 1 if e > 0 else 0
        best = max(best, run)
    return {
        "current_edge_apr": edges[-1],
        "mean_edge_apr": statistics.mean(edges),
        "median_edge_apr": statistics.median(edges),
        "min_edge_apr": min(edges),
        "max_edge_apr": max(edges),
        "stddev_edge_apr": statistics.pstdev(edges),
        "positive_edge_ratio": Decimal(pos) / n,
        "direction_consistency_ratio": Decimal(max(pos, neg)) / n,
        "sign_flip_count": flips,
        "longest_positive_streak_h": best,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coin", required=True)
    ap.add_argument("--top", type=int, default=15, help="rows to print (CSV always has all)")
    ap.add_argument("--include-gap-venues", action="store_true")
    args = ap.parse_args()
    coin = args.coin.lower()

    stamps, cols = load_hourly(HERE / f"{coin}_all_hourly_apr.csv")
    end = stamps[-1]
    idx = [i for i, t in enumerate(stamps) if end - timedelta(hours=WINDOW_HOURS) < t <= end]

    with open(HERE / f"{coin}_coverage.csv", newline="", encoding="utf-8") as f:
        history_ok = {r["venue"]: r["history_ok"] == "True" for r in csv.DictReader(f)}
    with open(HERE / f"{coin}_venues_snapshot.csv", newline="", encoding="utf-8") as f:
        snap = {r["Exchange"].strip(): r for r in csv.DictReader(f)}

    venues, excluded = [], []
    for v in sorted(cols):
        s = snap.get(v)
        if s is None:
            excluded.append((v, "not in venue snapshot"))
        elif s["Volume > $5M"] != "True":
            excluded.append((v, f"volume {s['24hr Volume (M)']}M is not > $5M"))
        elif not history_ok.get(v, False) and not args.include_gap_venues:
            excluded.append((v, "history gap or under 168h"))
        else:
            venues.append(v)

    rows = []
    for a, b in combinations(venues, 2):
        pts = [(cols[a][i], cols[b][i]) for i in idx]
        if len(pts) < WINDOW_HOURS or any(x is None or y is None for x, y in pts):
            rows.append({"long_venue": a, "short_venue": b, "status": "DATA_INSUFFICIENT"})
            continue
        diff = [y - x for x, y in pts]  # b short, a long
        if statistics.mean(diff) >= 0:
            long_v, short_v, edges = a, b, diff
        else:
            long_v, short_v, edges = b, a, [-d for d in diff]
        m = pair_metrics(edges)
        row = {
            "long_venue": long_v, "short_venue": short_v, "status": "OK",
            "long_class": snap[long_v]["Class"], "short_class": snap[short_v]["Class"],
            "long_market": snap[long_v]["Symbol"], "short_market": snap[short_v]["Symbol"],
            "long_interval_h": snap[long_v]["Interval"], "short_interval_h": snap[short_v]["Interval"],
            "observation_count": len(edges), "coverage_hours": WINDOW_HOURS - 1,
            "window_end": end.isoformat(),
            "compatibility": "UNVERIFIED_SAME_TICKER",
        }
        row.update(m)
        row["mean_edge_pct_per_hour"] = m["mean_edge_apr"] / HOURS_PER_YEAR
        row["current_edge_pct_per_hour"] = m["current_edge_apr"] / HOURS_PER_YEAR
        rows.append(row)

    ok = sorted((r for r in rows if r["status"] == "OK"), key=lambda r: -r["mean_edge_apr"])
    bad = [r for r in rows if r["status"] != "OK"]
    fields = list(ok[0]) if ok else ["long_venue", "short_venue", "status"]
    with open(HERE / f"{coin}_pair_analytics.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(ok + bad)

    print(f"{coin.upper()}: window ends {end}, {len(venues)} venues eligible, "
          f"{len(ok)} pairs OK, {len(bad)} DATA_INSUFFICIENT")
    for v, why in excluded:
        print(f"  excluded {v}: {why}")
    print(f"\nTop {args.top} by 7d mean edge (display order only)")
    print(f"{'long':22} {'short':22} {'cls':>9} {'now':>7} {'mean':>7} {'med':>7} {'std':>6} "
          f"{'pos%':>5} {'cons%':>5} {'flips':>5} {'streak':>6}")
    for r in ok[:args.top]:
        print(f"{r['long_venue'][:22]:22} {r['short_venue'][:22]:22} "
              f"{r['long_class'][:4] + '/' + r['short_class'][:4]:>9} "
              f"{r['current_edge_apr']:7.1f} {r['mean_edge_apr']:7.1f} {r['median_edge_apr']:7.1f} "
              f"{r['stddev_edge_apr']:6.1f} {r['positive_edge_ratio'] * 100:5.0f} "
              f"{r['direction_consistency_ratio'] * 100:5.0f} {r['sign_flip_count']:5d} "
              f"{r['longest_positive_streak_h']:6d}")


if __name__ == "__main__":
    main()
