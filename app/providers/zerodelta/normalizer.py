"""Funding normalization (project.md section 12).

Sign convention: positive funding = LONG pays SHORT, negative = SHORT pays LONG.
Units: percent. `normalize_per_hour(0.08, 8)` is 0.01 (%/h).
"""

from decimal import Decimal

HOURS_PER_YEAR = Decimal(8760)


def normalize_per_hour(rate_pct_for_interval: Decimal, interval_hours: Decimal) -> Decimal:
    if interval_hours <= 0:
        raise ValueError("funding interval must be positive")
    return rate_pct_for_interval / interval_hours


def apr_to_per_hour(apr_pct: Decimal) -> Decimal:
    """The history chart is annualized APR (percent); per-hour percent is APR / 8760."""
    return apr_pct / HOURS_PER_YEAR
