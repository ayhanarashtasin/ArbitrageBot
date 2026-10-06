"""Live low-volatility filter (project.md section 22, LOCKED).

    low_vol_score = max(underlying_rv_percentile, basis_volatility_percentile)
    entry allowed only if low_vol_score <= 60; otherwise WAIT (never a permanent rejection)

Definitions chosen here, because project.md gives only the names:
  underlying RV     rolling population stdev of 1-minute log returns of the underlying's price
                    (mean of the two venues' closes) over RV_WINDOW_MINUTES
  basis             (close_long_venue - close_short_venue) / mean of the two closes, per minute
  basis volatility  rolling population stdev of the one-minute CHANGE in basis over
                    BASIS_VOL_WINDOW_MINUTES
  percentile        percent of rolling values in the lookback that are STRICTLY BELOW the current one
                    (0..100). Ties do not count against the current value, so a perfectly flat series
                    scores 0 instead of being blocked.
Fewer than VOL_MIN_OBSERVATIONS rolling values means the score is unavailable and the entry waits.
"""

import statistics
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.domain.market import Candle

D = Decimal


@dataclass(frozen=True)
class VolScore:
    available: bool
    underlying_rv_percentile: Decimal | None
    basis_vol_percentile: Decimal | None
    low_vol_score: Decimal | None
    rv_observations: int
    basis_observations: int
    reason: str | None = None


def rolling_pstdev(values: list[Decimal], window: int) -> list[Decimal]:
    """Population stdev over every full window (oldest first). Empty if there are too few values."""
    if window < 2 or len(values) < window:
        return []
    return [statistics.pstdev(values[i - window: i]) for i in range(window, len(values) + 1)]


def percentile_of_latest(series: list[Decimal]) -> Decimal | None:
    """Percent of values in `series` strictly below the last value."""
    if not series:
        return None
    latest = series[-1]
    return D(100) * D(sum(1 for v in series if v < latest)) / D(len(series))


def log_returns(prices: list[Decimal]) -> list[Decimal]:
    return [(b / a).ln() for a, b in zip(prices, prices[1:]) if a > 0 and b > 0]


def align_closes(a: list[Candle], b: list[Candle]) -> list[tuple[datetime, Decimal, Decimal]]:
    """Closes present in both series, matched on candle open time, oldest first."""
    by_time = {c.open_time: c.close for c in b}
    return [(c.open_time, c.close, by_time[c.open_time]) for c in a if c.open_time in by_time]


def low_vol_score(
    long_candles: list[Candle],
    short_candles: list[Candle],
    *,
    rv_window: int,
    basis_window: int,
    min_observations: int,
) -> VolScore:
    aligned = align_closes(long_candles, short_candles)
    if len(aligned) < max(rv_window, basis_window) + 2:
        return VolScore(False, None, None, None, 0, 0, "not enough aligned 1-minute candles")

    mid = [(x + y) / 2 for _, x, y in aligned]
    rv = rolling_pstdev(log_returns(mid), rv_window)

    basis = [(x - y) / ((x + y) / 2) for _, x, y in aligned]
    basis_changes = [b - a for a, b in zip(basis, basis[1:])]
    bv = rolling_pstdev(basis_changes, basis_window)

    if len(rv) < min_observations or len(bv) < min_observations:
        return VolScore(False, None, None, None, len(rv), len(bv),
                        f"needs {min_observations} rolling observations, have rv={len(rv)} basis={len(bv)}")

    rv_pct, bv_pct = percentile_of_latest(rv), percentile_of_latest(bv)
    return VolScore(True, rv_pct, bv_pct, max(rv_pct, bv_pct), len(rv), len(bv))


# The smallest per-call 1-minute limit among the adapters: Bybit accepts at most 999 (its endpoint returns 1000 rows
# and one is kept for the still-forming candle). 1000 used to be requested here, which Bybit refuses with a
# ValueError, so no pair with a Bybit leg could ever be verified. 999 still leaves ~934 rolling values (>= 500).
MAX_1M_CANDLES = 999


def candles_needed(params) -> int:
    """1-minute candles to request per leg: the percentile lookback plus one rolling window, capped per call."""
    need = params.vol_percentile_lookback_days * 1440 + max(params.rv_window_minutes,
                                                            params.basis_vol_window_minutes) + 5
    return min(need, MAX_1M_CANDLES)


def entry_allowed(score: VolScore, max_percentile: Decimal) -> bool:
    """LOCKED boundary: a score of exactly 60 passes; anything higher waits."""
    return score.available and score.low_vol_score is not None and score.low_vol_score <= max_percentile
