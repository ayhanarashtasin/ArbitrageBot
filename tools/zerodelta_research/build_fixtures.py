"""Rebuild tests/fixtures/zerodelta/<coin>_dash_response.json from the captured research CSVs.

The fixtures have the raw Dash response shape (venue table + Plotly traces with base64 float64
data) so the real parser is exercised. They contain market data only, no cookie or account data.

    python tools/zerodelta_research/build_fixtures.py
"""

import base64
import csv
import json
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data" / "zerodelta"
OUT = ROOT / "tests" / "fixtures" / "zerodelta"


def build(coin: str) -> dict:
    with open(DATA / f"{coin}_venues_snapshot.csv", newline="", encoding="utf-8") as f:
        snapshot = list(csv.DictReader(f))
    rows = [
        {
            "Exchange": r["Exchange"], "Symbol": r["Symbol"],
            "Funding Rate": float(r["Funding Rate"]), "Interval": int(float(r["Interval"])),
            "APR (%)": float(r["APR (%)"]), "24hr Volume (M)": float(r["24hr Volume (M)"]),
            "Open Interest (M)": float(r["Open Interest (M)"]) if r["Open Interest (M)"] else None,
        }
        for r in snapshot
    ]
    with open(DATA / f"{coin}_all_hourly_apr.csv", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        venues = next(reader)[1:]
        table = list(reader)
    traces = []
    for i, venue in enumerate(venues):
        xs, ys = [], []
        for row in table:
            if row[1 + i] != "":
                xs.append(row[0])
                ys.append(float(row[1 + i]))
        bdata = base64.b64encode(struct.pack(f"<{len(ys)}d", *ys)).decode()
        traces.append({
            "mode": "lines", "name": f"{venue} (hourly)", "x": xs,
            "y": {"dtype": "f8", "bdata": bdata},
        })
    # Zero Delta's own suggestion trace; the parser must ignore it.
    traces.append({**traces[0], "name": "Best combo APR (Hourly)"})
    return {"response": {
        "apr-graph": {"figure": {"data": traces}},
        "store-latest-hourly": {"data": rows},
        "store-best-combo": {"data": {}},
    }}


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for coin in ("btc", "eth", "sol"):
        path = OUT / f"{coin}_dash_response.json"
        path.write_text(json.dumps(build(coin), separators=(",", ":")), encoding="utf-8")
        print(path.name, path.stat().st_size // 1024, "KB")
