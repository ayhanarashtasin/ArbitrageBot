"""
Save every venue's funding-APR history for one ticker (wide CSV: timestamp x venue).

Usage:
    export ZERO_DELTA_SESSION_COOKIE=...        (PowerShell: $env:ZERO_DELTA_SESSION_COOKIE="...")
    python tools/zerodelta_research/capture_all_venues.py --coin BTC

Outputs (in data/zerodelta/):
    <coin>_all_hourly_apr.csv   ~14 days hourly, one column per venue
    <coin>_all_daily_apr.csv    ~90 days daily, one column per venue
    <coin>_venues_snapshot.csv  current venue table (rate, interval, volume, OI, class)
    <coin>_coverage.csv         per-venue coverage check for the hourly series

Requires ZERO_DELTA_SESSION_COOKIE. There is no fallback and the cookie is never printed.
Reuses the reference client in Capture_zero_Delta/ without modifying it.
"""

import argparse
import csv
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Capture_zero_Delta"))
from zero_delta_fetcher import ZeroDeltaFetcher, decode_plotly_bdata  # noqa: E402

HERE = ROOT / "data" / "zerodelta"
HERE.mkdir(parents=True, exist_ok=True)
MIN_HOURS = 168  # 7 complete days (project.md section 9)
SKIP_NAMES = ("best combo",)  # Zero Delta's own suggestion trace, not a venue


def collect_series(fig):
    hourly, daily = {}, {}
    for t in fig.get("data", []):
        if t.get("mode") != "lines":
            continue
        name = (t.get("name") or "").strip()
        if any(s in name.lower() for s in SKIP_NAMES):
            continue
        x = t.get("x", [])
        y = decode_plotly_bdata(t.get("y"))
        if len(x) == 0 or len(y) == 0:
            continue
        if len(x) != len(y):
            print(f"  skip {name}: x/y length mismatch ({len(x)} vs {len(y)})")
            continue
        if name.lower().endswith("(hourly)"):
            hourly[name[: -len("(hourly)")].strip()] = dict(zip(x, y.tolist()))
        else:
            daily[name] = dict(zip(x, y.tolist()))
    return hourly, daily


def write_wide(path, series):
    venues = sorted(series)
    stamps = sorted({ts for s in series.values() for ts in s})
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["timestamp"] + venues)
        for ts in stamps:
            w.writerow([ts] + [series[v].get(ts, "") for v in venues])  # blank = missing, never filled
    return venues, stamps


def coverage_rows(series):
    rows = []
    for venue, s in sorted(series.items()):
        ts = sorted(datetime.fromisoformat(k[:19]) for k in s)
        hours = (ts[-1] - ts[0]).total_seconds() / 3600
        gaps = sum(1 for a, b in zip(ts, ts[1:]) if (b - a).total_seconds() != 3600)
        rows.append({
            "venue": venue,
            "first_timestamp": ts[0].isoformat(),
            "last_timestamp": ts[-1].isoformat(),
            "observation_count": len(ts),
            "coverage_hours": round(hours, 1),
            "gap_count": gaps,
            "history_ok": hours >= MIN_HOURS and gaps == 0,
        })
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coin", required=True)
    args = ap.parse_args()
    coin = args.coin.upper()

    cookie = os.environ.get("ZERO_DELTA_SESSION_COOKIE")
    if not cookie:
        sys.exit("ZERO_DELTA_SESSION_COOKIE is not set.")

    fetcher = ZeroDeltaFetcher(cookie)
    # The reference client discards all but one pair, so capture the raw response as it goes by.
    seen = {}
    real_post = fetcher.session.post

    def spy(*a, **k):
        r = real_post(*a, **k)
        seen["resp"] = r
        return r

    fetcher.session.post = spy
    fetcher.fetch_v1_data(coin=coin)
    response = seen["resp"].json().get("response", {})
    figure = response.get("apr-graph", {}).get("figure", {})
    venue_rows = response.get("store-latest-hourly", {}).get("data", [])

    hourly, daily = collect_series(figure)
    if not hourly:
        print("No hourly series returned; nothing saved.")
        sys.exit(1)

    hv, hs = write_wide(HERE / f"{coin.lower()}_all_hourly_apr.csv", hourly)
    dv, ds = write_wide(HERE / f"{coin.lower()}_all_daily_apr.csv", daily)
    print(f"hourly: {len(hv)} venues x {len(hs)} timestamps")
    print(f"daily : {len(dv)} venues x {len(ds)} timestamps")

    cov = coverage_rows(hourly)
    with open(HERE / f"{coin.lower()}_coverage.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(cov[0]))
        w.writeheader()
        w.writerows(cov)
    ok = sum(r["history_ok"] for r in cov)
    print(f"history_ok (>= {MIN_HOURS}h, no gaps): {ok}/{len(cov)} venues")
    for r in cov:
        if not r["history_ok"]:
            print(f"  NOT OK: {r['venue']}  hours={r['coverage_hours']} gaps={r['gap_count']}")

    rows = venue_rows
    if rows:
        with open(HERE / f"{coin.lower()}_venues_snapshot.csv", "w", newline="", encoding="utf-8") as f:
            cols = ["Exchange", "Symbol", "Funding Rate", "Interval", "APR (%)",
                    "24hr Volume (M)", "Open Interest (M)"]
            w = csv.writer(f)
            w.writerow(cols + ["Class", "Volume > $5M"])
            for r in rows:
                vol = float(r.get("24hr Volume (M)") or 0.0)
                w.writerow([r.get(c, "") for c in cols]
                           + [fetcher.classify_venue(r.get("Exchange", "")), vol > 5.0])
        print(f"snapshot: {len(rows)} venues")


if __name__ == "__main__":
    main()
