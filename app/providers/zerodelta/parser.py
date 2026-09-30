"""Parse a raw Dash response into typed snapshots and per-venue hourly series.

Strategy code never sees this raw JSON. Problems are collected as ParseIssue, not swallowed.
Timestamps in the chart are naive; they are treated as UTC (unverified, see project notes).
"""

import base64
import math
import struct
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .exceptions import ZeroDeltaParseError
from .models import ParseIssue, ZeroDeltaMarketSnapshot
from .normalizer import normalize_per_hour
from .venues import VenueRegistry

MILLION = Decimal(1_000_000)
REQUIRED_ROW_FIELDS = ("Exchange", "Symbol", "Funding Rate", "Interval", "24hr Volume (M)")
SKIPPED_TRACE_MARKERS = ("best combo",)  # Zero Delta's own suggestion trace, not a venue

Point = tuple[datetime, Decimal]


@dataclass
class ParsedAsset:
    snapshots: list[ZeroDeltaMarketSnapshot] = field(default_factory=list)
    series: dict[str, list[Point]] = field(default_factory=dict)  # keyed by trace venue name
    issues: list[ParseIssue] = field(default_factory=list)


def decode_bdata(y: Any) -> list[float | None]:
    """Decode Plotly's binary float64 array ({"dtype": "f8", "bdata": <base64>}) or a plain list.

    NaN becomes None so a gap stays a gap.
    """
    if isinstance(y, dict) and "bdata" in y:
        dtype = y.get("dtype", "f8")
        if dtype != "f8":
            raise ZeroDeltaParseError(f"Unsupported Plotly dtype {dtype!r}")
        try:
            raw = base64.b64decode(y["bdata"], validate=True)
        except (ValueError, TypeError) as exc:
            raise ZeroDeltaParseError("Invalid base64 in Plotly bdata") from exc
        if len(raw) % 8:
            raise ZeroDeltaParseError("Plotly bdata length is not a multiple of 8 bytes")
        values = struct.unpack(f"<{len(raw) // 8}d", raw)
    elif isinstance(y, list):
        values = y
    else:
        return []
    return [None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v) for v in values]


def parse_timestamp(text: str) -> datetime:
    """'2026-09-15T01:00:00.000000000' (nanoseconds, no zone) -> aware UTC datetime."""
    base, _, frac = text.partition(".")
    micro = (frac[:6]).ljust(6, "0") if frac else "000000"
    return datetime.fromisoformat(f"{base}.{micro}").replace(tzinfo=UTC)


def _dec(value: Any) -> Decimal:
    if value is None or isinstance(value, bool) or value == "":
        raise InvalidOperation
    return Decimal(str(value))


def parse_dash_response(
    payload: Any, asset: str, received_at: datetime, registry: VenueRegistry
) -> ParsedAsset:
    if not isinstance(payload, dict) or not isinstance(payload.get("response"), dict):
        raise ZeroDeltaParseError("Response has no 'response' object")
    response = payload["response"]
    try:
        rows = response["store-latest-hourly"]["data"]
        traces = response["apr-graph"]["figure"]["data"]
    except (KeyError, TypeError) as exc:
        raise ZeroDeltaParseError("Response lacks the venue table or the APR chart") from exc
    if not isinstance(rows, list) or not isinstance(traces, list):
        raise ZeroDeltaParseError("Venue table or chart data is not a list")

    out = ParsedAsset()
    seen_venues: set[str] = set()

    for row in rows:
        if not isinstance(row, dict):
            out.issues.append(ParseIssue(code="INVALID_VALUE", message="venue row is not an object"))
            continue
        name = str(row.get("Exchange") or "").strip()
        missing = [f for f in REQUIRED_ROW_FIELDS if row.get(f) in (None, "")]
        if missing:
            out.issues.append(ParseIssue(
                code="MISSING_FIELD", venue=name or None,
                message=f"venue row skipped, missing {', '.join(missing)}",
            ))
            continue
        try:
            rate = _dec(row["Funding Rate"])
            interval = _dec(row["Interval"])
            volume_m = _dec(row["24hr Volume (M)"])
            apr = _dec(row["APR (%)"]) if row.get("APR (%)") not in (None, "") else None
            oi_m = _dec(row["Open Interest (M)"]) if row.get("Open Interest (M)") not in (None, "") else None
        except InvalidOperation:
            out.issues.append(ParseIssue(
                code="INVALID_VALUE", venue=name, message="venue row skipped, non-numeric value"
            ))
            continue
        if interval <= 0:
            out.issues.append(ParseIssue(
                code="INVALID_VALUE", venue=name, message="venue row skipped, interval must be positive"
            ))
            continue
        if name in seen_venues:
            out.issues.append(ParseIssue(
                code="INVALID_VALUE", venue=name, message="duplicate venue row ignored"
            ))
            continue
        seen_venues.add(name)
        symbol = str(row["Symbol"])
        out.snapshots.append(ZeroDeltaMarketSnapshot(
            received_at=received_at,
            asset=asset.upper(),
            venue_name=name,
            venue_type=registry.classify(name),
            market_identifier=symbol,
            display_symbol=symbol,
            funding_rate_raw=rate,
            funding_interval_hours=interval,
            normalized_funding_rate_per_hour=normalize_per_hour(rate, interval),
            funding_apr=apr,
            avg_24h_volume_usd=volume_m * MILLION,
            open_interest_usd=None if oi_m is None else oi_m * MILLION,
        ))

    for trace in traces:
        if not isinstance(trace, dict) or trace.get("mode") != "lines":
            continue
        name = str(trace.get("name") or "").strip()
        if any(m in name.lower() for m in SKIPPED_TRACE_MARKERS):
            continue
        if not name.lower().endswith("(hourly)"):
            continue  # daily series are supplementary and not used in Phase 1
        venue = name[: -len("(hourly)")].strip()
        x = trace.get("x") or []
        y = decode_bdata(trace.get("y"))
        if len(x) != len(y):
            out.issues.append(ParseIssue(
                code="LENGTH_MISMATCH", venue=venue,
                message=f"series skipped, {len(x)} timestamps vs {len(y)} values",
            ))
            continue
        points: list[Point] = []
        nulls = 0
        try:
            for ts, val in zip(x, y):
                if val is None:
                    nulls += 1
                    continue
                points.append((parse_timestamp(str(ts)), Decimal(repr(val))))
        except ValueError:
            out.issues.append(ParseIssue(
                code="INVALID_VALUE", venue=venue, message="series skipped, unreadable timestamp"
            ))
            continue
        if nulls:
            out.issues.append(ParseIssue(
                code="NULL_VALUES", venue=venue, message=f"{nulls} empty points left as gaps"
            ))
        out.series[venue] = points

    return out
