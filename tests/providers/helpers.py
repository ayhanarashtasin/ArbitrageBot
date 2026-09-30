"""Builders for synthetic Dash responses used by the provider tests."""

import base64
import struct
from datetime import UTC, datetime, timedelta
from typing import Any

T0 = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)


def ts(hours: float, base: datetime = T0) -> str:
    """Timestamp string in Zero Delta's naive nanosecond format."""
    return (base + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S.000000000")


def bdata(values: list[float]) -> dict[str, str]:
    return {"dtype": "f8", "bdata": base64.b64encode(struct.pack(f"<{len(values)}d", *values)).decode()}


def trace(venue: str, hours: list[float], apr: float | list[float] = 10.0) -> dict[str, Any]:
    ys = apr if isinstance(apr, list) else [apr] * len(hours)
    return {"mode": "lines", "name": f"{venue} (hourly)", "x": [ts(h) for h in hours], "y": bdata(ys)}


def row(venue: str, *, volume_m: float = 50.0, interval: int = 8, rate: float = 0.01,
        symbol: str = "BTC/USDT:USDT") -> dict[str, Any]:
    return {
        "Exchange": venue, "Symbol": symbol, "Funding Rate": rate, "Interval": interval,
        "APR (%)": 10.95, "24hr Volume (M)": volume_m, "Open Interest (M)": 5.0,
    }


def response(rows: list[dict], traces: list[dict]) -> dict[str, Any]:
    return {"response": {
        "apr-graph": {"figure": {"data": traces}},
        "store-latest-hourly": {"data": rows},
        "store-best-combo": {"data": {}},
    }}


def hours(n_hours: int) -> list[float]:
    """Hourly points covering exactly n_hours (n_hours + 1 observations)."""
    return [float(h) for h in range(n_hours + 1)]
