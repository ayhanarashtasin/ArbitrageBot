import base64
import json
import struct
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.config import ZeroDeltaSettings
from app.providers.zerodelta import VenueRegistry, VenueType, ZeroDeltaParseError, ZeroDeltaProvider
from app.providers.zerodelta.history import analyze_series
from app.providers.zerodelta.normalizer import apr_to_per_hour, normalize_per_hour
from app.providers.zerodelta.parser import decode_bdata, parse_dash_response, parse_timestamp

from .helpers import T0, bdata, hours, response, row, trace

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "zerodelta"
NOW = datetime(2026, 9, 29, tzinfo=UTC)


def provider(**kw) -> ZeroDeltaProvider:
    return ZeroDeltaProvider(ZeroDeltaSettings(_env_file=None, **kw))


def analyze(rows, traces, **kw):
    return provider(**kw).analyze_response(response(rows, traces), "BTC", received_at=NOW)


def market(analysis, venue):
    return next(m for m in analysis.markets if m.snapshot.venue_name == venue)


# ---- response structure ----------------------------------------------------------------------

def test_valid_response_parses_snapshot_and_history():
    a = analyze([row("OKX", interval=8, rate=0.08, volume_m=12.5)], [trace("OKX", hours(168), 10.95)])
    m = market(a, "OKX")
    s = m.snapshot
    assert s.venue_type is VenueType.CEX
    assert s.funding_rate_raw == Decimal("0.08")
    assert s.funding_interval_hours == Decimal(8)
    assert s.normalized_funding_rate_per_hour == Decimal("0.01")
    assert s.avg_24h_volume_usd == Decimal(12_500_000)
    assert m.eligible and a.issues == ()
    assert len(a.histories["OKX"]) == 169
    obs = a.histories["OKX"][0]
    assert obs.normalized_funding_rate_per_hour == Decimal("10.95") / 8760
    assert obs.timestamp.tzinfo is UTC


def test_missing_row_field_skips_row_and_reports_issue():
    bad = row("Gate")
    del bad["Funding Rate"]
    a = analyze([bad, row("OKX")], [trace("OKX", hours(168))])
    assert [m.snapshot.venue_name for m in a.markets] == ["OKX"]
    assert any(i.code == "MISSING_FIELD" and i.venue == "Gate" for i in a.issues)


def test_unknown_extra_fields_are_ignored():
    r = row("OKX")
    r["Brand New Column"] = "x"
    payload = response([r], [trace("OKX", hours(168))])
    payload["response"]["something-new"] = {"data": 1}
    a = provider().analyze_response(payload, "BTC", received_at=NOW)
    assert market(a, "OKX").eligible


@pytest.mark.parametrize("payload", [None, [], "x", {}, {"response": None}, {"response": {}},
                                     {"response": {"store-latest-hourly": {"data": []}}}])
def test_malformed_structure_raises(payload):
    with pytest.raises(ZeroDeltaParseError):
        parse_dash_response(payload, "BTC", NOW, VenueRegistry())


def test_non_numeric_value_skips_row():
    r = row("OKX")
    r["Funding Rate"] = "abc"
    a = analyze([r], [trace("OKX", hours(168))])
    assert a.markets == () and a.issues[0].code == "INVALID_VALUE"


def test_zero_interval_rejected():
    a = analyze([row("OKX", interval=0)], [trace("OKX", hours(168))])
    assert a.markets == ()


# ---- binary history decoding -----------------------------------------------------------------

def test_bdata_roundtrip():
    assert decode_bdata(bdata([1.5, -2.25, 0.0])) == [1.5, -2.25, 0.0]


def test_bdata_plain_list_and_nan_becomes_gap():
    raw = base64.b64encode(struct.pack("<3d", 1.0, float("nan"), 3.0)).decode()
    assert decode_bdata({"dtype": "f8", "bdata": raw}) == [1.0, None, 3.0]
    assert decode_bdata([1, None, 3]) == [1.0, None, 3.0]


@pytest.mark.parametrize("bad", [{"dtype": "i4", "bdata": "AAAA"}, {"dtype": "f8", "bdata": "!!!"},
                                 {"dtype": "f8", "bdata": base64.b64encode(b"123").decode()}])
def test_bdata_invalid_raises(bad):
    with pytest.raises(ZeroDeltaParseError):
        decode_bdata(bad)


def test_null_points_stay_gaps_and_are_reported():
    t = trace("OKX", hours(168))
    ys = [10.0] * 169
    ys[50] = float("nan")
    t["y"] = bdata(ys)
    a = analyze([row("OKX")], [t])
    assert any(i.code == "NULL_VALUES" for i in a.issues)
    assert market(a, "OKX").coverage.missing_intervals == 1
    assert not market(a, "OKX").history_ok


def test_length_mismatch_reported():
    t = trace("OKX", hours(168))
    t["y"] = bdata([1.0, 2.0])
    a = analyze([row("OKX")], [t])
    assert any(i.code == "LENGTH_MISMATCH" for i in a.issues)
    assert market(a, "OKX").coverage is None


def test_timestamp_nanoseconds_are_utc():
    assert parse_timestamp("2026-09-15T01:00:00.000000000") == datetime(2026, 9, 15, 1, tzinfo=UTC)


# ---- history coverage ------------------------------------------------------------------------

def series(hs):
    return [(T0 + timedelta(hours=h), Decimal(1)) for h in hs]


def check(hs, **kw):
    return analyze_series(series(hs), primary_days=7, min_days=7, max_missing_intervals=0, **kw)[0]


@pytest.mark.parametrize("span,ok", [(24, False), (72, False), (167, False), (168, True), (336, True), (720, True)])
def test_seven_day_rule(span, ok):
    c = check(hours(span))
    assert (c.status == "OK") is ok
    assert c.coverage_seconds == min(span, 168) * 3600
    if not ok:
        assert "COVERAGE_BELOW_MIN" in c.reasons


def test_longer_history_is_trimmed_to_primary_window():
    _, window = analyze_series(series(hours(336)), primary_days=7, min_days=7, max_missing_intervals=0)
    assert len(window) == 169


def test_duplicate_timestamps_counted_first_wins():
    pts = series(hours(168)) + [(T0, Decimal(99))]
    c, window = analyze_series(pts, primary_days=7, min_days=7, max_missing_intervals=0)
    assert c.duplicate_count == 1 and c.observation_count == 169
    assert window[0][1] == Decimal(1)


def test_out_of_order_is_counted_and_sorted():
    pts = series(hours(168))
    pts[10], pts[11] = pts[11], pts[10]
    c, window = analyze_series(pts, primary_days=7, min_days=7, max_missing_intervals=0)
    assert c.out_of_order_count == 1 and c.status == "OK"
    assert [p[0] for p in window] == sorted(p[0] for p in window)


def test_missing_interval_fails_by_default_and_passes_when_allowed():
    hs = [h for h in hours(168) if h != 100]
    assert check(hs).missing_intervals == 1 and check(hs).status == "DATA_INSUFFICIENT"
    assert "GAPS_EXCEED_LIMIT" in check(hs).reasons
    c, _ = analyze_series(series(hs), primary_days=7, min_days=7, max_missing_intervals=1)
    assert c.status == "OK"


def test_gaps_are_never_filled():
    hs = [h for h in hours(168) if h not in (50, 51)]
    c, window = analyze_series(series(hs), primary_days=7, min_days=7, max_missing_intervals=5)
    assert c.missing_intervals == 2 and len(window) == 167


def test_empty_series_is_insufficient():
    c, w = analyze_series([], primary_days=7, min_days=7, max_missing_intervals=0)
    assert c.status == "DATA_INSUFFICIENT" and w == []


def test_venue_row_without_series_is_insufficient():
    a = analyze([row("OKX")], [])
    m = market(a, "OKX")
    assert m.coverage is None and not m.history_ok and "NO_HISTORY" in m.reasons


def test_series_without_venue_row_is_reported():
    a = analyze([row("OKX")], [trace("OKX", hours(168)), trace("Mystery", hours(168))])
    assert any(i.code == "SYMBOL_MISMATCH" and i.venue == "Mystery" for i in a.issues)
    assert "Mystery" not in a.histories


def test_best_combo_trace_is_ignored():
    a = analyze([row("OKX")], [trace("OKX", hours(168)), {**trace("x", hours(168)), "name": "Best combo APR (Hourly)"}])
    assert a.issues == ()


# ---- volume filter (strictly greater than $5,000,000) ----------------------------------------

@pytest.mark.parametrize("vol_m,ok", [(4.99999999, False), (5.0, False), (5.00000001, True)])
def test_volume_boundary(vol_m, ok):
    a = analyze([row("OKX", volume_m=vol_m)], [trace("OKX", hours(168))])
    assert market(a, "OKX").volume_ok is ok
    assert market(a, "OKX").eligible is ok


def test_locked_values_cannot_be_lowered():
    with pytest.raises(ValueError):
        ZeroDeltaSettings(_env_file=None, min_avg_24h_volume_usd=Decimal(4_000_000))
    with pytest.raises(ValueError):
        ZeroDeltaSettings(_env_file=None, min_lookback_days=6)


# ---- venue types -----------------------------------------------------------------------------

def test_venue_types_and_overrides():
    reg = VenueRegistry({"Arcus": "DEX", "Some HIP Market": "HIP-3"})
    assert reg.classify("Binance USDT") is VenueType.CEX
    assert reg.classify("dYdX") is VenueType.DEX
    assert reg.classify("Arcus") is VenueType.DEX
    assert reg.classify("Some HIP Market") is VenueType.HIP3
    assert reg.classify("Totally New Venue") is VenueType.UNKNOWN


def test_unknown_venue_type_is_flagged_but_not_a_locked_filter():
    a = analyze([row("Totally New Venue")], [trace("Totally New Venue", hours(168))])
    m = market(a, "Totally New Venue")
    assert not m.venue_type_known and "VENUE_TYPE_UNKNOWN" in m.reasons and m.eligible


# ---- normalization ---------------------------------------------------------------------------

@pytest.mark.parametrize("rate,interval,expected", [
    ("0.01", 1, "0.01"), ("0.04", 4, "0.01"), ("0.08", 8, "0.01"),
    ("-0.08", 8, "-0.01"), ("0", 8, "0"),
])
def test_funding_normalization(rate, interval, expected):
    assert normalize_per_hour(Decimal(rate), Decimal(interval)) == Decimal(expected)


def test_normalization_rejects_bad_interval():
    with pytest.raises(ValueError):
        normalize_per_hour(Decimal(1), Decimal(0))


def test_apr_to_per_hour():
    assert apr_to_per_hour(Decimal(8760)) == Decimal(1)


def test_days_beyond_hourly_window_rejected():
    with pytest.raises(ValueError):
        provider().analyze_response(response([row("OKX")], [trace("OKX", hours(168))]), "BTC", days=15)


# ---- BTC / ETH / SOL fixtures (raw Dash shape, built from real captures) ---------------------

@pytest.mark.parametrize("coin,eligible,total", [("btc", 26, 32), ("eth", 24, 32), ("sol", 23, 28)])
def test_real_fixtures(coin, eligible, total):
    payload = json.loads((FIXTURES / f"{coin}_dash_response.json").read_text("utf-8"))
    a = provider().analyze_response(payload, coin, received_at=NOW)
    assert len(a.markets) == total
    assert sum(m.eligible for m in a.markets) == eligible
    assert a.issues == ()
    bybit = market(a, "Bybit USDT")
    assert not bybit.history_ok and bybit.coverage.missing_intervals == 1
    for m in a.markets:
        if m.eligible:
            assert m.coverage.coverage_seconds == 168 * 3600
