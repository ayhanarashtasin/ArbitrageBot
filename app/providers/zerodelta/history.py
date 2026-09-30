"""Coverage validation for hourly funding history (project.md section 9).

No forward-fill, no interpolation, gaps are counted and never treated as observations.
"""

from datetime import datetime, timedelta
from decimal import Decimal

from .models import HistoryCoverage

STEP = timedelta(hours=1)

Point = tuple[datetime, Decimal]


def analyze_series(
    raw: list[Point],
    *,
    primary_days: int,
    min_days: int,
    max_missing_intervals: int,
) -> tuple[HistoryCoverage, list[Point]]:
    """Validate one venue's series and return (coverage, points inside the trailing window).

    The window is the trailing `primary_days` ending at the series' newest timestamp. Coverage is
    the span of points inside that window and must reach `min_days` (168 hours for 7 days).
    """
    out_of_order = sum(1 for a, b in zip(raw, raw[1:]) if b[0] < a[0])
    ordered = sorted(raw, key=lambda p: p[0])  # stable: first occurrence of a duplicate wins
    unique: list[Point] = []
    seen: set[datetime] = set()
    for point in ordered:
        if point[0] not in seen:
            seen.add(point[0])
            unique.append(point)
    duplicates = len(ordered) - len(unique)

    if not unique:
        return (
            HistoryCoverage(
                requested_start=None, requested_end=None, first_timestamp=None,
                last_timestamp=None, coverage_seconds=0, observation_count=0,
                expected_observation_count_if_known=None, missing_intervals=0,
                duplicate_count=duplicates, out_of_order_count=out_of_order,
                status="DATA_INSUFFICIENT", reasons=("NO_OBSERVATIONS",),
            ),
            [],
        )

    end = unique[-1][0]
    start = end - timedelta(days=primary_days)
    window = [p for p in unique if p[0] >= start]
    first = window[0][0]
    coverage_seconds = int((end - first).total_seconds())
    missing = sum(
        max(0, round((b[0] - a[0]) / STEP) - 1) for a, b in zip(window, window[1:])
    )

    reasons: list[str] = []
    if coverage_seconds < min_days * 86400:
        reasons.append("COVERAGE_BELOW_MIN")
    if missing > max_missing_intervals:
        reasons.append("GAPS_EXCEED_LIMIT")

    coverage = HistoryCoverage(
        requested_start=start,
        requested_end=end,
        first_timestamp=first,
        last_timestamp=end,
        coverage_seconds=coverage_seconds,
        observation_count=len(window),
        expected_observation_count_if_known=coverage_seconds // 3600 + 1,
        missing_intervals=missing,
        duplicate_count=duplicates,
        out_of_order_count=out_of_order,
        status="DATA_INSUFFICIENT" if reasons else "OK",
        reasons=tuple(reasons),
    )
    return coverage, window
