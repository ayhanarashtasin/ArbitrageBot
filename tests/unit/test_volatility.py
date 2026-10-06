import math
from datetime import timedelta
from decimal import Decimal

import pytest

from app.market_data.volatility import (
    VolScore,
    entry_allowed,
    log_returns,
    low_vol_score,
    percentile_of_latest,
    rolling_pstdev,
)

from .helpers import candles

D = Decimal


def test_percentile_definition():
    assert percentile_of_latest([D(1), D(2), D(3), D(2)]) == D(25)  # only the 1 is strictly below 2
    assert percentile_of_latest([D(5), D(1)]) == D(0)
    assert percentile_of_latest([D(1), D(9)]) == D(50)
    assert percentile_of_latest([D(4)] * 10) == D(0)  # ties do not count against the latest value
    assert percentile_of_latest([]) is None


def test_rolling_pstdev():
    assert rolling_pstdev([D(1), D(2), D(3), D(4)], 2) == [D("0.5")] * 3
    assert rolling_pstdev([D(1)], 2) == []


def test_log_returns():
    r = log_returns([D(100), D(110)])
    assert abs(float(r[0]) - math.log(1.1)) < 1e-12


def wavy(n, amp_early, amp_late, switch):
    """Price series whose swings change size at index `switch`."""
    p, out = 100.0, []
    for i in range(n):
        amp = amp_early if i < switch else amp_late
        p += amp if i % 2 == 0 else -amp
        out.append(round(p, 6))
    return out


def test_quiet_now_after_wild_history_scores_low():
    c = candles(wavy(400, amp_early=2.0, amp_late=0.01, switch=300))
    s = low_vol_score(c, c, rv_window=20, basis_window=20, min_observations=100)
    assert s.available
    assert s.underlying_rv_percentile < 45
    assert entry_allowed(s, D(60))


def test_wild_now_after_quiet_history_scores_high():
    c = candles(wavy(400, amp_early=0.01, amp_late=2.0, switch=380))
    s = low_vol_score(c, c, rv_window=10, basis_window=10, min_observations=100)
    assert s.available and s.underlying_rv_percentile > 90
    assert not entry_allowed(s, D(60))


def test_basis_volatility_drives_the_score_when_underlying_is_calm():
    base = [100.0 + 0.001 * (i % 2) for i in range(400)]
    other = [b + (0.0 if i < 380 else (1.0 if i % 2 else -1.0)) for i, b in enumerate(base)]
    s = low_vol_score(candles(base), candles(other), rv_window=10, basis_window=10, min_observations=100)
    assert s.available and s.basis_vol_percentile > 90
    assert s.low_vol_score == max(s.underlying_rv_percentile, s.basis_vol_percentile)


def test_too_few_observations_waits():
    c = candles([100 + i * 0.01 for i in range(60)])
    s = low_vol_score(c, c, rv_window=20, basis_window=20, min_observations=500)
    assert not s.available and not entry_allowed(s, D(60))


@pytest.mark.parametrize("score,ok", [("20", True), ("40", True), ("60", True), ("60.0001", False), ("61", False)])
def test_locked_60_boundary(score, ok):
    s = VolScore(True, D(score), D(1), D(score), 1000, 1000)
    assert entry_allowed(s, D(60)) is ok


def test_unavailable_score_never_allows_entry():
    assert not entry_allowed(VolScore(False, None, None, None, 0, 0, "x"), D(60))


def test_unaligned_candles_are_matched_on_time_only():
    a = candles([100 + i for i in range(50)])
    b = candles([100 + i for i in range(50)], start=a[0].open_time + timedelta(minutes=25))
    s = low_vol_score(a, b, rv_window=5, basis_window=5, min_observations=10)
    assert s.rv_observations <= 25


def test_the_candle_request_fits_every_adapters_per_call_limit():
    """A request above a venue's cap raises ValueError before any network call: Bybit legs were never verifiable."""
    from app.core.trading_config import TradingSettings
    from app.exchanges import binance, blofin, bybit, gate, hyperliquid, mexc, okx
    from app.market_data.volatility import candles_needed
    limits = {"bybit": bybit._MAX_KLINES, "binance": binance._MAX_KLINES - 1, "hyperliquid": hyperliquid._MAX_CANDLES - 1,
              "okx": okx._MAX_CANDLES, "gate": gate._MAX_CANDLES, "mexc": mexc._MAX_KLINES, "blofin": blofin._MAX_CANDLES}
    need = candles_needed(TradingSettings(_env_file=None).resolve())
    assert all(need <= cap for cap in limits.values()), {k: v for k, v in limits.items() if need > v}
