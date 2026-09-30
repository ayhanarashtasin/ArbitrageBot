import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.core.config import ZeroDeltaSettings
from app.providers.zerodelta import ZeroDeltaProvider
from app.providers.zerodelta.client import ZeroDeltaClient
from app.strategy.analytics import compute_edge_stats
from app.strategy.instruments import ContractKind, parse_market_identifier
from app.strategy.pairs import generate_pairs
from tests.providers.helpers import hours, response, row, trace

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "zerodelta"
NOW = datetime(2026, 9, 29, tzinfo=UTC)
D = Decimal
LIN = "BTC/USDT:USDT"
TYPES = {"Alpha": "CEX", "Beta": "DEX", "Gamma": "HIP3"}


def settings(types=None, **kw):
    return ZeroDeltaSettings(_env_file=None, venue_types=types or TYPES, **kw)


def analysis_for(st, rows, traces, asset="BTC"):
    return ZeroDeltaProvider(st).analyze_response(response(rows, traces), asset, received_at=NOW)


def build(venues, asset="BTC", types=None, **kw):
    """venues: {name: (apr value or 169-point list, market symbol)}"""
    st = settings(types, **kw)
    rows = [row(v, symbol=sym, rate=0.01) for v, (_, sym) in venues.items()]
    traces = [trace(v, hours(168), apr) for v, (apr, _) in venues.items()]
    return generate_pairs(analysis_for(st, rows, traces, asset), st)


# ---- instruments -----------------------------------------------------------------------------

@pytest.mark.parametrize("ident,kind", [
    ("BTC/USDT:USDT", ContractKind.LINEAR), ("BTC/USD:USD", ContractKind.LINEAR),
    ("BTC/USD:BTC", ContractKind.INVERSE), ("BTC/USDC:XYZ", ContractKind.UNKNOWN),
])
def test_contract_kind(ident, kind):
    assert parse_market_identifier(ident).kind is kind


@pytest.mark.parametrize("bad", ["", "BTC", "BTC/USDT", "BTCUSDT:USDT", "/USDT:USDT"])
def test_unrecognised_identifier(bad):
    assert parse_market_identifier(bad) is None


# ---- statistics (hand-checked) ---------------------------------------------------------------

def test_edge_stats_known_values():
    s = compute_edge_stats([D(x) for x in (1, 2, -1, 3, 3, 0, -2)])
    assert s.mean == D(6) / 7
    assert s.median == D(1)
    assert (s.minimum, s.maximum, s.latest) == (D(-2), D(3), D(-2))
    assert s.positive_edge_ratio == D(4) / 7
    assert s.direction_consistency_ratio == D(4) / 7
    assert s.sign_flip_count == 3  # + to -, - to +, + to - (the zero is skipped)
    assert s.longest_positive_edge_streak == 2


def test_edge_stats_constant_series():
    s = compute_edge_stats([D(2)] * 10)
    assert s.stddev == 0 and s.sign_flip_count == 0 and s.longest_positive_edge_streak == 10
    assert s.positive_edge_ratio == 1


def test_edge_stats_are_decimal():
    s = compute_edge_stats([D("0.1"), D("0.2")])
    assert isinstance(s.mean, Decimal) and isinstance(s.stddev, Decimal)
    assert s.mean == D("0.15")


def test_edge_stats_empty_rejected():
    with pytest.raises(ValueError):
        compute_edge_stats([])


# ---- pair generation -------------------------------------------------------------------------

def test_orientation_shorts_the_higher_funding_venue():
    pairs = build({"Alpha": (8.76, LIN), "Beta": (26.28, LIN)})
    assert len(pairs) == 1
    p = pairs[0]
    assert (p.long_leg.venue_name, p.short_leg.venue_name) == ("Alpha", "Beta")
    assert p.stats.mean == D(2) / 1000  # (26.28 - 8.76) / 8760 = 0.002 %/h
    assert p.stats.positive_edge_ratio == 1 and p.stats.sign_flip_count == 0


def test_orientation_reverses_with_the_data():
    p = build({"Alpha": (26.28, LIN), "Beta": (8.76, LIN)})[0]
    assert (p.long_leg.venue_name, p.short_leg.venue_name) == ("Beta", "Alpha")
    assert p.stats.mean > 0


def test_type_combinations():
    pairs = build({n: (10.0 + i, LIN) for i, n in enumerate(["Alpha", "Beta", "Gamma"])})
    assert sorted(p.pair_type for p in pairs) == ["CEX<->DEX", "CEX<->HIP3", "DEX<->HIP3"]
    same = build({"Alpha": (1.0, LIN), "Other": (2.0, LIN)}, types={"Alpha": "CEX", "Other": "CEX"})
    assert same[0].pair_type == "CEX<->CEX"
    dd = build({"Beta": (1.0, LIN), "Other": (2.0, LIN)}, types={"Beta": "DEX", "Other": "DEX"})
    assert dd[0].pair_type == "DEX<->DEX"
    hh = build({"Gamma": (1.0, LIN), "Other": (2.0, LIN)}, types={"Gamma": "HIP3", "Other": "HIP3"})
    assert hh[0].pair_type == "HIP3<->HIP3"


def test_unknown_type_pairs_are_kept_and_labelled():
    pairs = build({"Alpha": (1.0, LIN), "Mystery": (2.0, LIN)})
    assert pairs[0].pair_type == "CEX<->UNKNOWN"
    assert pairs[0].long_leg.venue_type_verified or pairs[0].short_leg.venue_type_verified


def test_inverse_and_other_underlying_are_excluded():
    pairs = build({"Alpha": (1.0, LIN), "Beta": (2.0, "BTC/USD:BTC"), "Gamma": (3.0, "ETH/USDT:USDT")})
    assert pairs == []


def test_no_self_pairs_and_full_combination_count():
    pairs = build({"Alpha": (1.0, LIN), "Beta": (2.0, LIN), "Gamma": (3.0, LIN)})
    assert len(pairs) == 3
    assert all(p.long_leg.venue_name != p.short_leg.venue_name for p in pairs)


def test_ineligible_markets_are_not_paired():
    st = settings()
    rows = [row("Alpha", symbol=LIN), row("Beta", symbol=LIN, volume_m=5.0)]
    traces = [trace("Alpha", hours(168)), trace("Beta", hours(168))]
    assert generate_pairs(analysis_for(st, rows, traces), st) == []


def test_settlement_mismatch_flag_and_multiplier_unverified():
    p = build({"Alpha": (1.0, "BTC/USDT:USDT"), "Beta": (2.0, "BTC/USDC:USDC")})[0]
    assert p.settlement_mismatch and not p.contract_multiplier_verified
    assert not build({"Alpha": (1.0, LIN), "Beta": (2.0, LIN)})[0].settlement_mismatch


def test_unavailable_metrics_stay_none():
    p = build({"Alpha": (1.0, LIN), "Beta": (2.0, LIN)})[0]
    assert p.current_basis is None and p.mean_basis_7d is None and p.provider_score is None


def test_sign_flips_on_alternating_series():
    up = [30.0, -30.0] * 84 + [30.0]
    p = build({"Alpha": (up, LIN), "Beta": ([0.0] * 169, LIN)})[0]
    assert p.stats.sign_flip_count == 168
    assert p.stats.longest_positive_edge_streak == 1


def test_current_edge_comes_from_the_current_venue_table():
    st = settings()
    rows = [row("Alpha", symbol=LIN, rate=0.01, interval=1), row("Beta", symbol=LIN, rate=0.04, interval=8)]
    traces = [trace("Alpha", hours(168), 5.0), trace("Beta", hours(168), 9.0)]
    p = generate_pairs(analysis_for(st, rows, traces), st)[0]
    # Beta is short (higher history): 0.04 %/8h = 0.005 %/h. Alpha is long: 0.01 %/1h.
    assert p.current_funding_edge == D("0.005") - D("0.01")


def test_venue_with_gap_is_excluded_before_pairing():
    st = settings()
    rows = [row("Alpha", symbol=LIN), row("Beta", symbol=LIN)]
    gappy = trace("Beta", [h for h in hours(168) if h not in (60, 61)], [12.0] * 167)
    a = analysis_for(st, rows, [trace("Alpha", hours(168), 10.0), gappy])
    assert not next(m for m in a.markets if m.snapshot.venue_name == "Beta").history_ok
    assert generate_pairs(a, st) == []


def test_gap_allowed_when_configured_pairs_on_shared_hours():
    st = settings(max_missing_intervals=1)
    rows = [row("Alpha", symbol=LIN), row("Beta", symbol=LIN)]
    one_gap = trace("Beta", [h for h in hours(168) if h != 60], [12.0] * 168)
    p = generate_pairs(analysis_for(st, rows, [trace("Alpha", hours(168), 10.0), one_gap]), st)[0]
    assert p.status == "OK" and p.coverage.missing_intervals == 1 and p.coverage.observation_count == 168


# ---- real fixtures ---------------------------------------------------------------------------

def load(coin):
    return json.loads((FIXTURES / f"{coin}_dash_response.json").read_text("utf-8"))


@pytest.mark.parametrize("coin,venues", [("btc", 26), ("eth", 24), ("sol", 23)])
def test_real_fixture_pair_counts(coin, venues):
    st = ZeroDeltaSettings(_env_file=None)
    a = ZeroDeltaProvider(st).analyze_response(load(coin), coin, received_at=NOW)
    pairs = generate_pairs(a, st)
    assert len(pairs) == venues * (venues - 1) // 2
    assert all(p.status == "OK" and p.coverage.coverage_seconds == 168 * 3600 for p in pairs)
    assert all(p.stats.mean >= 0 for p in pairs)
    assert not any(p.contract_multiplier_verified for p in pairs)


def test_provider_get_candidate_pairs_end_to_end():
    body = load("sol")
    st = ZeroDeltaSettings(_env_file=None, session_cookie="x", user_id=1)
    client = ZeroDeltaClient(st, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)))
    pairs = asyncio.run(ZeroDeltaProvider(st, client=client).get_candidate_pairs("SOL"))
    assert len(pairs) == 253
